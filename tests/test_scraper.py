import json
import time

import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE
from scraper.cache import ArtifactCache, CachedGCSClient
from scraper.state import ScrapeState
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
def test_scrape_discovers_and_ingests(metrics_json, tmp_path):
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-step-graph.json",
                  status=404)
    responses.add(responses.POST, f"{VM_URL}/api/v1/import/prometheus", status=204)

    session = requests.Session()
    gcs = CachedGCSClient(GCSClient(session, BUCKET))
    state = ScrapeState(str(tmp_path / "state.db"))
    vm_sink = VictoriaMetricsSink(session, VM_URL)
    pipelines = [MetricsPipeline(vm_sink)]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1, state=state)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) > 0

    # Build should be marked done in state
    assert not state.should_process("12345", "metrics", MetricsPipeline.version)


@responses.activate
def test_scrape_skips_known_builds(tmp_path):
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    # Pre-mark build as done in state
    state = ScrapeState(str(tmp_path / "state.db"))
    state.mark_done("12345", "metrics", MetricsPipeline.version)

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    session = requests.Session()
    gcs = CachedGCSClient(GCSClient(session, BUCKET))
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1, state=state)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    # No started.json fetch — build was skipped
    get_calls = [c for c in responses.calls if "started.json" in c.request.url]
    assert len(get_calls) == 0


@responses.activate
def test_scrape_dry_run(metrics_json):
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-step-graph.json",
                  status=404)

    session = requests.Session()
    gcs = CachedGCSClient(GCSClient(session, BUCKET))
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=True)

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) == 0


@responses.activate
def test_scrape_skips_out_of_range_builds():
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": 1000000}, status=200)

    session = requests.Session()
    gcs = CachedGCSClient(GCSClient(session, BUCKET))
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    metrics_calls = [c for c in responses.calls if "ci-operator-metrics" in c.request.url]
    assert len(metrics_calls) == 0


@responses.activate
def test_scrape_reprocesses_pipeline_on_version_mismatch(metrics_json, tmp_path):
    """When a pipeline's version changes, builds processed at old version are reprocessed."""
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())

    # Mark build as done at OLD version
    state = ScrapeState(str(tmp_path / "state.db"))
    state.mark_done("12345", "metrics", "old-version")

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": now}, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-metrics.json",
                  json=metrics_json, status=200)
    responses.add(responses.GET,
                  f"{BASE_URL}/{base_path}/100/job-e2e/12345/artifacts/ci-operator-step-graph.json",
                  status=404)
    responses.add(responses.POST, f"{VM_URL}/api/v1/import/prometheus", status=204)

    session = requests.Session()
    gcs = CachedGCSClient(GCSClient(session, BUCKET))
    vm_sink = VictoriaMetricsSink(session, VM_URL)
    pipelines = [MetricsPipeline(vm_sink)]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1, state=state)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    # Build should be reprocessed and marked done at current version
    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) > 0
    assert not state.should_process("12345", "metrics", MetricsPipeline.version)


@responses.activate
def test_out_of_range_build_registered_in_cache(tmp_path):
    """Out-of-range builds are registered in cache so cleanup can find them."""
    base_path = "pr-logs/pull/org_repo"
    now = int(time.time())
    old_ts = 1000000  # far in the past

    _mock_gcs_listing(base_path, [("100", "job-e2e", ["12345"])])

    responses.add(responses.GET, f"{BASE_URL}/{base_path}/100/job-e2e/12345/started.json",
                  json={"timestamp": old_ts}, status=200)

    session = requests.Session()
    cache = ArtifactCache(str(tmp_path / "cache"))
    gcs = CachedGCSClient(GCSClient(session, BUCKET), cache)
    pipelines = [MetricsPipeline(VictoriaMetricsSink(session, VM_URL))]
    scraper = Scraper(gcs, session, VM_URL, VL_URL, pipelines, workers=1)

    scraper.scrape(base_path, since=now - 3600, until=now + 3600, dry_run=False)

    # Build was out of range so nothing should be ingested
    metrics_calls = [c for c in responses.calls if "ci-operator-metrics" in c.request.url]
    assert len(metrics_calls) == 0

    # But the build MUST be registered in the cache for cleanup
    with cache._db_lock:
        row = cache._db.execute(
            "SELECT started_ts FROM builds WHERE prefix = ?",
            (f"{base_path}/100/job-e2e/12345",),
        ).fetchone()
    assert row is not None
    assert row[0] == old_ts
