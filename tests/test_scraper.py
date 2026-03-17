import json
import time

import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE
from scraper.sinks import VictoriaMetricsSink
from scraper.metrics import MetricsPipeline
from scraper.scraper import Scraper

BUCKET = "test-platform-results"
BASE_URL = f"{GCS_BASE}/{BUCKET}"
VM_URL = "http://vm:8428"
VL_URL = "http://vl:9428"


def make_gcs_listing_xml(prefixes):
    items = "".join(
        f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        {items}
    </ListBucketResult>"""


def _mock_known_pipeline(pipeline_name, pipeline_version, build_ids):
    """Mock the VM label values API for a specific pipeline+version."""
    responses.add(
        responses.GET, f"{VM_URL}/api/v1/label/build_id/values",
        json={"status": "success", "data": list(build_ids)}, status=200,
    )


def _mock_gcs_listing(base_path, prs_jobs_builds):
    """Mock GCS listing for PRs/jobs/builds. prs_jobs_builds is [(pr, job, [builds])]."""
    pr_prefixes = list({f"{base_path}/{pr}/" for pr, _, _ in prs_jobs_builds})
    responses.add(responses.GET, f"{BASE_URL}/",
                  body=make_gcs_listing_xml(pr_prefixes), status=200)
    for pr, job, builds in prs_jobs_builds:
        responses.add(responses.GET, f"{BASE_URL}/",
                      body=make_gcs_listing_xml([f"{base_path}/{pr}/{job}/"]), status=200)
        responses.add(responses.GET, f"{BASE_URL}/",
                      body=make_gcs_listing_xml([f"{base_path}/{pr}/{job}/{b}/" for b in builds]), status=200)


@responses.activate
def test_scrape_discovers_and_ingests(metrics_json):
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    # Mock per-pipeline known build_ids query (one per pipeline)
    _mock_known_pipeline("metrics", MetricsPipeline.version, [])

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)
    # Mock VM push (metrics + sentinel)
    responses.add(responses.POST, f"{VM_URL}/api/v1/import/prometheus", status=204)

    session = requests.Session()
    gcs = GCSClient(session, BUCKET)
    vm_sink = VictoriaMetricsSink(session, VM_URL)
    pipelines = [MetricsPipeline(vm_sink)]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) > 0
    # Verify per-pipeline sentinel was pushed
    sentinel_calls = [c for c in post_calls if "ci_pipeline_scraped" in (c.request.body or "")]
    assert len(sentinel_calls) == 1
    assert 'pipeline="metrics"' in sentinel_calls[0].request.body


@responses.activate
def test_scrape_skips_known_builds():
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_known_pipeline("metrics", MetricsPipeline.version, ["12345"])
    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    session = requests.Session()
    gcs = GCSClient(session, BUCKET)
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    # No started.json fetch — build was skipped
    get_calls = [c for c in responses.calls if "started.json" in c.request.url]
    assert len(get_calls) == 0


@responses.activate
def test_scrape_dry_run(metrics_json):
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_known_pipeline("metrics", MetricsPipeline.version, [])
    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)

    session = requests.Session()
    gcs = GCSClient(session, BUCKET)
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=True)

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) == 0


@responses.activate
def test_scrape_skips_out_of_range_builds():
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_known_pipeline("metrics", MetricsPipeline.version, [])
    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": 1000000}, status=200)

    session = requests.Session()
    gcs = GCSClient(session, BUCKET)
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    metrics_calls = [c for c in responses.calls if "ci-operator-metrics" in c.request.url]
    assert len(metrics_calls) == 0


@responses.activate
def test_scrape_reprocesses_pipeline_on_version_mismatch(metrics_json):
    """When a pipeline's version changes, only that pipeline reprocesses."""
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    # The scraper queries with current version — return empty to simulate version mismatch.
    # Build 12345 exists at old version but not at current, so it must be reprocessed.
    _mock_known_pipeline("metrics", MetricsPipeline.version, [])
    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)
    responses.add(responses.POST, f"{VM_URL}/api/v1/import/prometheus", status=204)

    session = requests.Session()
    gcs = GCSClient(session, BUCKET)
    vm_sink = VictoriaMetricsSink(session, VM_URL)
    pipelines = [MetricsPipeline(vm_sink)]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    # Build should be reprocessed since no builds match current version
    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) > 0
    sentinel_calls = [c for c in post_calls if "ci_pipeline_scraped" in (c.request.body or "")]
    assert len(sentinel_calls) == 1
