import json
import os
import time

import pytest
import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE

BUCKET = "test-bucket"
BASE_URL = f"{GCS_BASE}/{BUCKET}"


def make_client(cache_dir=None):
    return GCSClient(requests.Session(), BUCKET, cache_dir=cache_dir)


@responses.activate
def test_fetch_object_success():
    responses.add(responses.GET, f"{BASE_URL}/some/path.json",
                  body='{"key": "value"}', status=200)
    client = make_client()
    result = client.fetch_object("some/path.json")
    assert result == '{"key": "value"}'


@responses.activate
def test_fetch_object_404():
    responses.add(responses.GET, f"{BASE_URL}/missing.json", status=404)
    client = make_client()
    assert client.fetch_object("missing.json") is None


@responses.activate
def test_fetch_object_500():
    responses.add(responses.GET, f"{BASE_URL}/error.json", status=500)
    client = make_client()
    with pytest.raises(requests.HTTPError):
        client.fetch_object("error.json")


@responses.activate
def test_list_prefixes_single_page():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/100/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=xml_body, status=200)
    client = make_client()
    result = client.list_prefixes("pr-logs/pull/org_repo/")
    assert result == ["pr-logs/pull/org_repo/100/", "pr-logs/pull/org_repo/200/"]


@responses.activate
def test_list_prefixes_pagination():
    page1 = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>true</IsTruncated>
        <NextMarker>pr-logs/pull/org_repo/100/</NextMarker>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/100/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    page2 = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=page1, status=200)
    responses.add(responses.GET, f"{BASE_URL}/", body=page2, status=200)
    client = make_client()
    result = client.list_prefixes("pr-logs/pull/org_repo/")
    assert len(result) == 2
    assert "pr-logs/pull/org_repo/100/" in result
    assert "pr-logs/pull/org_repo/200/" in result


@responses.activate
def test_list_prs():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>base/100/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>base/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=xml_body, status=200)
    client = make_client()
    result = client.list_prs("base")
    assert result == ["100", "200"]


# --- Cache tests ---

@responses.activate
def test_cache_fetch_object_hit(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='{"cached": true}', status=200)
    client = make_client(cache_dir=str(tmp_path))
    # First call fetches from GCS and caches
    assert client.fetch_object("data.json") == '{"cached": true}'
    assert len(responses.calls) == 1
    # Second call serves from cache
    assert client.fetch_object("data.json") == '{"cached": true}'
    assert len(responses.calls) == 1  # no new HTTP call


@responses.activate
def test_cache_fetch_object_miss(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/missing.json", status=404)
    client = make_client(cache_dir=str(tmp_path))
    assert client.fetch_object("missing.json") is None
    assert len(responses.calls) == 1
    # Second call returns None from cache without HTTP
    assert client.fetch_object("missing.json") is None
    assert len(responses.calls) == 1


@responses.activate
def test_cache_fetch_binary_hit(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.tar",
                  body=b'\x00\x01\x02\x03', status=200)
    client = make_client(cache_dir=str(tmp_path))
    assert client.fetch_binary("data.tar") == b'\x00\x01\x02\x03'
    assert len(responses.calls) == 1
    assert client.fetch_binary("data.tar") == b'\x00\x01\x02\x03'
    assert len(responses.calls) == 1


@responses.activate
def test_cache_head_uses_fetch_entry(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/exists.json",
                  body='content', status=200)
    client = make_client(cache_dir=str(tmp_path))
    # Fetch populates cache
    client.fetch_object("exists.json")
    # Head reads from cache without HTTP
    assert client.head_object("exists.json") is True
    assert len(responses.calls) == 1  # only the original fetch


@responses.activate
def test_cache_head_miss_cached(tmp_path):
    responses.add(responses.HEAD, f"{BASE_URL}/gone.tar", status=404)
    client = make_client(cache_dir=str(tmp_path))
    assert client.head_object("gone.tar") is False
    assert len(responses.calls) == 1
    # Cached miss
    assert client.head_object("gone.tar") is False
    assert len(responses.calls) == 1


@responses.activate
def test_cache_disabled_no_caching():
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='content', status=200)
    client = make_client(cache_dir=None)
    client.fetch_object("data.json")
    client.fetch_object("data.json")
    assert len(responses.calls) == 2  # no caching, both hit GCS


@responses.activate
def test_cache_persists_across_clients(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='original', status=200)
    client1 = make_client(cache_dir=str(tmp_path))
    client1.fetch_object("data.json")
    assert len(responses.calls) == 1
    # New client, same cache dir
    client2 = make_client(cache_dir=str(tmp_path))
    assert client2.fetch_object("data.json") == 'original'
    assert len(responses.calls) == 1  # served from disk


# --- cleanup_aged_builds tests ---

def _make_build_dir(tmp_path, pr, job, build_id, timestamp, extra_files=None):
    """Create a build directory with started.json and optional extra files."""
    build_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / pr / job / build_id
    build_dir.mkdir(parents=True)
    started = build_dir / "started.json"
    started.write_text(json.dumps({"timestamp": timestamp}))
    if extra_files:
        for rel_path, content in extra_files.items():
            f = build_dir / rel_path
            f.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                f.write_bytes(content)
            else:
                f.write_text(content)
    return build_dir


def test_cleanup_deletes_old_builds(tmp_path):
    """Builds older than cutoff are deleted entirely."""
    now = int(time.time())
    old_build = _make_build_dir(tmp_path, "1", "job-a", "100", now - 200 * 86400,
                                extra_files={
                                    "artifacts/step/gather-extra/artifacts/metrics/prometheus.tar.metrics": "metrics",
                                    "artifacts/step/gather-extra/artifacts/events.json": "events",
                                    "artifacts/step/ci-operator.log": "log data",
                                    "artifacts/step/gather-extra/artifacts/metrics/prometheus.tar.miss": "",
                                })
    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert not old_build.exists()


def test_cleanup_keeps_recent_builds(tmp_path):
    """Builds within the retention window survive."""
    now = int(time.time())
    recent_build = _make_build_dir(tmp_path, "1", "job-a", "200", now - 30 * 86400,
                                   extra_files={"artifacts/ci-operator.log": "log"})
    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert recent_build.exists()
    assert (recent_build / "started.json").exists()
    assert (recent_build / "artifacts" / "ci-operator.log").exists()


def test_cleanup_deletes_old_orphaned_tmp_files(tmp_path):
    """Orphaned tmp files older than 1 hour are deleted."""
    now = int(time.time())
    # Recent build to keep the directory alive
    _make_build_dir(tmp_path, "1", "job-a", "300", now - 10 * 86400)
    # Orphaned tmp file in a subdirectory
    step_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / "1" / "job-a" / "300" / "artifacts" / "step"
    step_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = step_dir / "tmpabcdef123"
    tmp_file.write_bytes(b"orphaned atomic write data" * 1000)
    # Set mtime to 2 hours ago
    old_time = time.time() - 7200
    os.utime(tmp_file, (old_time, old_time))

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert not tmp_file.exists()


def test_cleanup_skips_fresh_tmp_files(tmp_path):
    """Tmp files younger than 1 hour are kept (could be active atomic writes)."""
    now = int(time.time())
    _make_build_dir(tmp_path, "1", "job-a", "400", now - 10 * 86400)
    step_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / "1" / "job-a" / "400" / "artifacts"
    step_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = step_dir / "tmpfresh999"
    tmp_file.write_bytes(b"in-progress write")
    # mtime is now (default) -- should survive

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert tmp_file.exists()


def test_cleanup_removes_empty_parent_dirs(tmp_path):
    """Empty PR/job directories are removed after build deletion."""
    now = int(time.time())
    old_build = _make_build_dir(tmp_path, "99", "job-x", "500", now - 200 * 86400)
    pr_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / "99"

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert not old_build.exists()
    assert not pr_dir.exists(), "empty PR directory should be removed"


def test_cleanup_keeps_nonempty_parent_dirs(tmp_path):
    """Parent directories with surviving builds are kept."""
    now = int(time.time())
    # Two builds under the same PR: one old, one recent
    _make_build_dir(tmp_path, "1", "job-a", "600", now - 200 * 86400)
    recent = _make_build_dir(tmp_path, "1", "job-a", "700", now - 10 * 86400)
    pr_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / "1"

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert pr_dir.exists(), "PR directory should be kept (has surviving build)"
    assert recent.exists()


def test_cleanup_no_cache_noop():
    """cleanup_aged_builds is a no-op when cache is disabled."""
    client = make_client(cache_dir=None)
    client.cleanup_aged_builds(0)  # should not raise


def test_cleanup_idempotent(tmp_path):
    """Running cleanup twice produces zero deletions on second run."""
    now = int(time.time())
    _make_build_dir(tmp_path, "1", "job-a", "800", now - 200 * 86400)

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    # Second run -- nothing left to delete
    client.cleanup_aged_builds(now - 90 * 86400)  # should not raise


def test_cleanup_corrupt_started_json(tmp_path):
    """Builds with corrupt started.json are treated as aged out."""
    now = int(time.time())
    build_dir = tmp_path / "pr-logs" / "pull" / "org_repo" / "1" / "job-a" / "900"
    build_dir.mkdir(parents=True)
    (build_dir / "started.json").write_text("not valid json{{{")

    client = make_client(cache_dir=str(tmp_path))
    client.cleanup_aged_builds(now - 90 * 86400)
    assert not build_dir.exists(), "corrupt started.json should be treated as timestamp 0"
