"""Tests for ArtifactCache and CachedGCSClient."""
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scraper.cache import ArtifactCache, CachedGCSClient


@pytest.fixture
def cache(tmp_path):
    return ArtifactCache(str(tmp_path / "cache"))


@pytest.fixture
def cache_dir(tmp_path):
    return str(tmp_path / "cache")


# ------------------------------------------------------------------
# ArtifactCache: artifact files
# ------------------------------------------------------------------


def test_put_get_roundtrip(cache):
    cache.put("some/path.json", b'{"key": "value"}')
    assert cache.get("some/path.json") == b'{"key": "value"}'


def test_get_uncached_returns_none(cache):
    assert cache.get("nonexistent/file.json") is None


def test_file_path_returns_path_when_cached(cache):
    cache.put("a/b.json", b"data")
    fp = cache.file_path("a/b.json")
    assert fp is not None
    assert fp.exists()


def test_file_path_returns_none_when_not_cached(cache):
    assert cache.file_path("missing.json") is None


def test_put_stream(cache):
    chunks = [b"hello ", b"world"]
    path = cache.put_stream("streamed/file.bin", iter(chunks))
    assert path.exists()
    assert path.read_bytes() == b"hello world"
    assert cache.get("streamed/file.bin") == b"hello world"


# ------------------------------------------------------------------
# ArtifactCache: miss tracking
# ------------------------------------------------------------------


def test_miss_tracking(cache):
    assert not cache.is_miss("missing/artifact.json")
    cache.record_miss("missing/artifact.json")
    assert cache.is_miss("missing/artifact.json")


def test_miss_does_not_affect_other_paths(cache):
    cache.record_miss("a.json")
    assert not cache.is_miss("b.json")


def test_record_miss_idempotent(cache):
    cache.record_miss("path.json")
    cache.record_miss("path.json")  # should not raise
    assert cache.is_miss("path.json")


# ------------------------------------------------------------------
# ArtifactCache: staging
# ------------------------------------------------------------------


def test_stage_unstage(cache):
    chunks = [b"tar content part 1", b"tar content part 2"]
    path1 = cache.stage("some/prometheus.tar", iter(chunks))
    assert path1.exists()
    assert path1.read_bytes() == b"tar content part 1tar content part 2"
    assert str(path1).startswith(str(cache._staging))

    # Second stage of same GCS path gets a different file
    chunks2 = [b"other content"]
    path2 = cache.stage("some/prometheus.tar", iter(chunks2))
    assert path2 != path1

    cache.unstage(path1)
    assert not path1.exists()
    assert path2.exists()  # other staging unaffected

    cache.unstage(path2)
    assert not path2.exists()


def test_staging_cleanup_at_init(cache_dir):
    # Create first cache, stage a file
    cache1 = ArtifactCache(cache_dir)
    staged = cache1.stage("test/file.tar", iter([b"data"]))
    assert staged.exists()

    # Create second cache (simulating restart) -- staging should be wiped
    cache2 = ArtifactCache(cache_dir)
    assert not staged.exists()
    assert cache2._staging.exists()  # directory recreated


# ------------------------------------------------------------------
# ArtifactCache: processed output
# ------------------------------------------------------------------


def test_processed_output_version_match(cache):
    cache.put_processed("path/prometheus.tar", "1.5", "metric_line_1\nmetric_line_2\n")
    result = cache.get_processed("path/prometheus.tar", "1.5")
    assert result == "metric_line_1\nmetric_line_2\n"


def test_processed_output_version_mismatch(cache):
    cache.put_processed("path/prometheus.tar", "1.5", "data")
    assert cache.get_processed("path/prometheus.tar", "1.6") is None


def test_processed_output_empty_content(cache):
    # Empty metrics are valid (build with no relevant metrics)
    cache.put_processed("path/prometheus.tar", "1.0", "")
    result = cache.get_processed("path/prometheus.tar", "1.0")
    assert result == ""


def test_processed_output_missing(cache):
    assert cache.get_processed("nonexistent", "1.0") is None


# ------------------------------------------------------------------
# ArtifactCache: build metadata and cleanup
# ------------------------------------------------------------------


def test_cleanup_deletes_expired_builds(cache):
    # Register a build and write some artifacts into it
    prefix = "pr-logs/pull/org_repo/1/job-a/100"
    cache.register_build(prefix, 1000)
    cache.put(f"{prefix}/artifacts/file.json", b"data")
    cache.record_miss(f"{prefix}/artifacts/missing.json")

    # Cleanup with cutoff after the build's timestamp
    cache.cleanup(2000)

    # Build directory and associated misses should be gone
    assert not (cache._dir / prefix).exists()
    assert not cache.is_miss(f"{prefix}/artifacts/missing.json")


def test_cleanup_preserves_recent_builds(cache):
    prefix = "pr-logs/pull/org_repo/2/job-b/200"
    cache.register_build(prefix, 5000)
    cache.put(f"{prefix}/artifacts/file.json", b"data")

    cache.cleanup(3000)  # cutoff before build's timestamp

    assert cache.get(f"{prefix}/artifacts/file.json") == b"data"


def test_cleanup_handles_missing_directory(cache):
    # Register a build but don't create any files
    cache.register_build("pr-logs/pull/org/1/job/100", 500)
    # Should not raise
    cache.cleanup(1000)


# ------------------------------------------------------------------
# ArtifactCache: atomic write correctness
# ------------------------------------------------------------------


def test_atomic_write_produces_complete_file(cache):
    """Verify the file only appears at its final path after write completes."""
    gcs_path = "test/atomic.json"
    data = b"complete data"
    cache.put(gcs_path, data)
    assert cache.get(gcs_path) == data
    # No tmp files should linger
    parent = cache._artifact_path(gcs_path).parent
    tmp_files = [f for f in parent.iterdir() if f.name.startswith("tmp")]
    assert tmp_files == []


# ------------------------------------------------------------------
# ArtifactCache: concurrency
# ------------------------------------------------------------------


def test_concurrent_miss_recording(cache):
    errors = []

    def record(path):
        try:
            cache.record_miss(path)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=record, args=(f"path/{i}.json",))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for i in range(20):
        assert cache.is_miss(f"path/{i}.json")


def test_concurrent_miss_same_path(cache):
    """Multiple threads recording the same miss path concurrently."""
    errors = []

    def record():
        try:
            cache.record_miss("shared/path.json")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=record) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert cache.is_miss("shared/path.json")


# ------------------------------------------------------------------
# ArtifactCache: SQLite error degradation
# ------------------------------------------------------------------


def test_sqlite_error_degrades_gracefully(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Write garbage to cache.db to corrupt it
    (cache_dir / "cache.db").write_text("not a database")
    cache = ArtifactCache(str(cache_dir))

    # Should degrade gracefully -- no crash
    assert not cache._db_ok
    assert not cache.is_miss("anything")
    cache.record_miss("anything")  # should not raise
    cache.register_build("prefix", 1000)  # should not raise

    # Artifact operations (filesystem-only) should still work
    cache.put("test.json", b"data")
    assert cache.get("test.json") == b"data"


# ------------------------------------------------------------------
# CachedGCSClient
# ------------------------------------------------------------------


def _make_mock_gcs():
    """Create a mock GCSClient with common methods."""
    gcs = MagicMock()
    gcs.list_prefixes.return_value = ["a/", "b/"]
    gcs.list_prs.return_value = ["1", "2"]
    gcs.list_jobs.return_value = ["job-a"]
    gcs.list_builds.return_value = ["100"]
    return gcs


def test_cached_gcs_client_cache_hit(cache):
    gcs = _make_mock_gcs()
    client = CachedGCSClient(gcs, cache)

    # Pre-populate cache
    cache.put("some/file.json", b'{"cached": true}')

    result = client.fetch_object("some/file.json")
    assert result == '{"cached": true}'
    # GCS should NOT have been called
    gcs.fetch_text.assert_not_called()


def test_cached_gcs_client_cache_miss_fetches(cache):
    gcs = _make_mock_gcs()
    gcs.fetch_text.return_value = '{"from": "gcs"}'
    client = CachedGCSClient(gcs, cache)

    result = client.fetch_object("uncached/file.json")
    assert result == '{"from": "gcs"}'
    gcs.fetch_text.assert_called_once_with("uncached/file.json")
    # Should now be cached
    assert cache.get("uncached/file.json") == b'{"from": "gcs"}'


def test_cached_gcs_client_404_records_miss(cache):
    gcs = _make_mock_gcs()
    gcs.fetch_text.return_value = None
    client = CachedGCSClient(gcs, cache)

    result = client.fetch_object("missing.json")
    assert result is None
    assert cache.is_miss("missing.json")

    # Second call should not hit GCS
    gcs.fetch_text.reset_mock()
    result2 = client.fetch_object("missing.json")
    assert result2 is None
    gcs.fetch_text.assert_not_called()


def test_cached_gcs_client_no_cache_mode():
    gcs = _make_mock_gcs()
    gcs.fetch_text.return_value = '{"data": 1}'
    client = CachedGCSClient(gcs, cache=None)

    result = client.fetch_object("file.json")
    assert result == '{"data": 1}'
    gcs.fetch_text.assert_called_once()

    # ensure_cached returns None without cache
    assert client.ensure_cached("file.json") is None
    assert client.ensure_staged("file.json") is None


def test_cached_gcs_client_head_uses_cache(cache):
    gcs = _make_mock_gcs()
    client = CachedGCSClient(gcs, cache)

    cache.put("exists.json", b"content")
    assert client.head_object("exists.json") is True
    gcs.head.assert_not_called()


def test_cached_gcs_client_head_records_miss(cache):
    gcs = _make_mock_gcs()
    gcs.head.return_value = False
    client = CachedGCSClient(gcs, cache)

    assert client.head_object("missing.json") is False
    assert cache.is_miss("missing.json")


def test_cached_gcs_client_fetch_binary(cache):
    gcs = _make_mock_gcs()
    gcs.fetch_bytes.return_value = b"\x89PNG..."
    client = CachedGCSClient(gcs, cache)

    result = client.fetch_binary("image.png")
    assert result == b"\x89PNG..."
    assert cache.get("image.png") == b"\x89PNG..."


def test_ensure_cached_streams_to_cache(cache):
    gcs = _make_mock_gcs()
    gcs.stream.return_value = iter([b"chunk1", b"chunk2"])
    client = CachedGCSClient(gcs, cache)

    path = client.ensure_cached("large/file.bin")
    assert path is not None
    assert path.read_bytes() == b"chunk1chunk2"
    assert str(path).startswith(str(cache._dir))
    # Not in staging
    assert ".staging" not in str(path)


def test_ensure_staged_streams_to_staging(cache):
    gcs = _make_mock_gcs()
    gcs.stream.return_value = iter([b"tar-data"])
    client = CachedGCSClient(gcs, cache)

    path = client.ensure_staged("build/prometheus.tar")
    assert path is not None
    assert path.read_bytes() == b"tar-data"
    assert ".staging" in str(path)


def test_ensure_staged_404_records_miss(cache):
    gcs = _make_mock_gcs()
    gcs.stream.return_value = None
    client = CachedGCSClient(gcs, cache)

    assert client.ensure_staged("missing.tar") is None
    assert cache.is_miss("missing.tar")


def test_cached_gcs_client_delegates_listing(cache):
    gcs = _make_mock_gcs()
    client = CachedGCSClient(gcs, cache)

    assert client.list_prs("base") == ["1", "2"]
    gcs.list_prs.assert_called_once_with("base")


# ------------------------------------------------------------------
# ArtifactCache: cleanup robustness
# ------------------------------------------------------------------


def _write_started_json(cache, prefix, timestamp):
    """Write a started.json with the given timestamp into a build dir."""
    data = json.dumps({"timestamp": timestamp}).encode()
    cache.put(f"{prefix}/started.json", data)


def test_cleanup_removes_orphan_dirs(cache):
    """Dirs on disk with started.json but not registered in DB are cleaned."""
    prefix = "pr-logs/pull/org_repo/1/job-a/100"
    _write_started_json(cache, prefix, 1000)
    cache.put(f"{prefix}/artifacts/file.json", b"data")
    # Do NOT register in DB — simulates the pre-fix orphan scenario
    assert (cache._dir / prefix).exists()

    cache.cleanup(2000)

    assert not (cache._dir / prefix).exists()


def test_cleanup_preserves_recent_orphan_dirs(cache):
    """Orphan dirs with recent timestamps are not deleted."""
    prefix = "pr-logs/pull/org_repo/2/job-b/200"
    _write_started_json(cache, prefix, 5000)
    cache.put(f"{prefix}/artifacts/file.json", b"data")

    cache.cleanup(3000)

    assert (cache._dir / prefix / "artifacts" / "file.json").exists()


def test_cleanup_rmtree_failure_keeps_build_in_db(cache):
    """If rmtree fails, the build stays in DB for retry next cycle."""
    prefix = "pr-logs/pull/org_repo/1/job-a/100"
    cache.register_build(prefix, 1000)
    _write_started_json(cache, prefix, 1000)
    cache.put(f"{prefix}/artifacts/file.json", b"data")

    with patch("scraper.cache.shutil.rmtree", side_effect=OSError("disk error")):
        cache.cleanup(2000)

    # Build should still be in DB
    with cache._db_lock:
        row = cache._db.execute(
            "SELECT 1 FROM builds WHERE prefix = ?", (prefix,),
        ).fetchone()
    assert row is not None
    # Files should still be on disk
    assert (cache._dir / prefix / "artifacts" / "file.json").exists()

    # Next cleanup (without mock) should succeed
    cache.cleanup(2000)
    assert not (cache._dir / prefix).exists()
    with cache._db_lock:
        row = cache._db.execute(
            "SELECT 1 FROM builds WHERE prefix = ?", (prefix,),
        ).fetchone()
    assert row is None


def test_cleanup_removes_orphan_misses(cache):
    """Misses for builds not in DB and not on disk are cleaned."""
    # Record misses for a build that doesn't exist anywhere
    cache.record_miss("pr-logs/pull/org_repo/99/job-x/999/artifacts/missing.json")
    cache.record_miss("pr-logs/pull/org_repo/99/job-x/999/artifacts/other.json")
    assert cache.is_miss("pr-logs/pull/org_repo/99/job-x/999/artifacts/missing.json")

    cache.cleanup(2000)

    assert not cache.is_miss("pr-logs/pull/org_repo/99/job-x/999/artifacts/missing.json")
    assert not cache.is_miss("pr-logs/pull/org_repo/99/job-x/999/artifacts/other.json")


def test_cleanup_preserves_misses_for_existing_dirs(cache):
    """Misses for unregistered builds whose dirs still exist are preserved."""
    prefix = "pr-logs/pull/org_repo/3/job-c/300"
    _write_started_json(cache, prefix, 5000)  # recent — won't be deleted
    cache.record_miss(f"{prefix}/artifacts/missing.json")

    cache.cleanup(3000)

    # Dir still exists (recent timestamp), so misses should be preserved
    assert cache.is_miss(f"{prefix}/artifacts/missing.json")


def test_cleanup_walk_skips_staging(cache):
    """Walk cleanup does not delete staged files."""
    staged = cache.stage("some/prometheus.tar", iter([b"tar data"]))
    assert staged.exists()

    cache.cleanup(999999999)  # far-future cutoff

    assert staged.exists()


def test_cleanup_walk_cleans_db_entries_for_orphans(cache):
    """Walk cleanup removes misses and stale DB entries for orphan dirs."""
    prefix = "pr-logs/pull/org_repo/4/job-d/400"
    _write_started_json(cache, prefix, 1000)
    cache.record_miss(f"{prefix}/artifacts/missing.json")
    # Don't register in DB — orphan

    cache.cleanup(2000)

    # Dir should be gone
    assert not (cache._dir / prefix).exists()
    # Misses for the orphan should be cleaned
    assert not cache.is_miss(f"{prefix}/artifacts/missing.json")
