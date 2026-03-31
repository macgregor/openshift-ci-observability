from unittest.mock import MagicMock, patch

from scraper.test_cluster_metrics import (
    discover_test_steps,
    parse_promtool_line,
    parse_exposition_line,
    extract_test_cluster_metrics,
    extract_health_metrics,
    _build_node_role_map,
    _filter_overlapping,
    _read_cached_metrics,
    _write_cached_metrics,
    _OUTPUT_NAMES,
    _HEALTH_OUTPUT_NAMES,
    _HEALTH_METRICS_FILE,
    _OVERLAPPING_OUTPUT_NAMES,
    TestClusterMetricsPipeline,
)
from scraper.cache import ArtifactCache, CachedGCSClient
from scraper.state import ScrapeState


SAMPLE_LABELS = {
    "org": "opendatahub-io",
    "repo": "opendatahub-operator",
    "branch": "main",
    "job_name": "pull-ci-opendatahub-io-opendatahub-operator-main-opendatahub-operator-e2e",
    "pr_number": "3260",
    "pr_sha": "d26c6146301d",
    "author": "dev",
    "build_id": "2031880686163464192",
}

BUCKET = "test-platform-results"
VM_URL = "http://vm:8428"


def _make_pipeline(tmp_path=None):
    """Create a TestClusterMetricsPipeline with mock dependencies."""
    sink = MagicMock()
    gcs = MagicMock(spec=CachedGCSClient)
    cache = MagicMock(spec=ArtifactCache)
    cache.get_processed.return_value = None
    state = MagicMock(spec=ScrapeState)
    pipeline = TestClusterMetricsPipeline(sink, gcs, cache, state)
    return pipeline, sink, gcs, cache, state


def test_discover_test_steps_filters_infra():
    ctx = MagicMock()
    ctx.list_artifact_dirs.return_value = [
        "build-logs", "build-resources", "my-e2e-step", "release",
    ]
    assert discover_test_steps(ctx) == ["my-e2e-step"]


def test_discover_test_steps_multiple():
    ctx = MagicMock()
    ctx.list_artifact_dirs.return_value = ["step-a", "step-b"]
    assert discover_test_steps(ctx) == ["step-a", "step-b"]


def test_discover_test_steps_all_infra():
    ctx = MagicMock()
    ctx.list_artifact_dirs.return_value = ["build-logs", "build-resources", "release"]
    assert discover_test_steps(ctx) == []


def test_parse_promtool_line_basic():
    line = '{__name__="cluster:cpu_usage_cores:sum", prometheus="openshift-monitoring/k8s"} 3.14 1710000000000'
    result = parse_promtool_line(line)
    assert result is not None
    name, labels, value, ts = result
    assert name == "cluster:cpu_usage_cores:sum"
    assert labels == {"prometheus": "openshift-monitoring/k8s"}
    assert value == 3.14
    assert ts == 1710000000


def test_parse_promtool_line_multiple_labels():
    line = '{__name__="machine_cpu_cores", instance="10.0.1.5:9100", node="ip-10-0-1-5"} 16 1710000030000'
    result = parse_promtool_line(line)
    name, labels, value, ts = result
    assert name == "machine_cpu_cores"
    assert labels["instance"] == "10.0.1.5:9100"
    assert labels["node"] == "ip-10-0-1-5"
    assert value == 16.0
    assert ts == 1710000030


def test_parse_promtool_line_invalid():
    assert parse_promtool_line("") is None
    assert parse_promtool_line("garbage") is None
    assert parse_promtool_line("# comment") is None


def test_parse_promtool_line_no_name():
    line = '{foo="bar"} 1.0 1710000000000'
    assert parse_promtool_line(line) is None


def test_metric_name_conversion():
    assert _OUTPUT_NAMES["cluster:cpu_usage_cores:sum"] == "ci_test_cluster_cluster_cpu_usage_cores_sum"
    assert _OUTPUT_NAMES["cluster:capacity_cpu_cores:sum"] == "ci_test_cluster_cluster_capacity_cpu_cores_sum"
    assert _OUTPUT_NAMES["cluster:memory_usage_bytes:sum"] == "ci_test_cluster_cluster_memory_usage_bytes_sum"
    assert _OUTPUT_NAMES["instance:node_memory_utilisation:ratio"] == "ci_test_cluster_instance_node_memory_utilisation_ratio"
    assert _OUTPUT_NAMES["node_memory_MemTotal_bytes"] == "ci_test_cluster_node_memory_memtotal_bytes"
    assert _OUTPUT_NAMES["machine_cpu_cores"] == "ci_test_cluster_machine_cpu_cores"
    assert _OUTPUT_NAMES["kube_pod_container_resource_requests"] == "ci_test_cluster_kube_pod_container_resource_requests"
    assert _OUTPUT_NAMES["kube_pod_container_resource_limits"] == "ci_test_cluster_kube_pod_container_resource_limits"
    assert _OUTPUT_NAMES["container_memory_working_set_bytes"] == "ci_test_cluster_container_memory_working_set_bytes"


def test_extract_test_cluster_metrics():
    lines = [
        '{__name__="cluster:cpu_usage_cores:sum", prometheus="openshift-monitoring/k8s"} 3.14 1710000000000',
        '{__name__="machine_cpu_cores", instance="10.0.1.5:9100"} 16 1710000030000',
        '{__name__="unrelated_metric", foo="bar"} 99 1710000000000',
    ]
    job_labels = {"build_id": "123", "pr_number": "42"}
    metrics = extract_test_cluster_metrics(lines, job_labels)
    assert len(metrics) == 2
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in metrics[0]
    assert 'build_id="123"' in metrics[0]
    assert 'metrics_source="tsdb"' in metrics[0]
    assert "3.14" in metrics[0]
    assert "ci_test_cluster_machine_cpu_cores" in metrics[1]
    assert 'pr_number="42"' in metrics[1]
    assert 'metrics_source="tsdb"' in metrics[1]


def test_extract_pod_resource_metrics():
    lines = [
        '{__name__="kube_pod_container_resource_requests", container="operator", namespace="redhat-ods-operator", node="ip-10-0-1-2.ec2.internal", pod="opendatahub-operator-0", resource="cpu", unit="core"} 0.5 1710000000000',
        '{__name__="kube_pod_container_resource_requests", container="operator", namespace="redhat-ods-operator", node="ip-10-0-1-2.ec2.internal", pod="opendatahub-operator-0", resource="memory", unit="byte"} 536870912 1710000000000',
        '{__name__="kube_pod_container_resource_limits", container="operator", namespace="redhat-ods-operator", node="ip-10-0-1-2.ec2.internal", pod="opendatahub-operator-0", resource="cpu", unit="core"} 1.0 1710000000000',
        '{__name__="container_memory_working_set_bytes", container="operator", namespace="redhat-ods-operator", pod="opendatahub-operator-0", id="/kubepods/pod123/ctr456"} 268435456 1710000000000',
    ]
    job_labels = {"build_id": "99", "repo": "opendatahub-io/opendatahub-operator"}
    metrics = extract_test_cluster_metrics(lines, job_labels)
    assert len(metrics) == 4
    # Verify metric names
    names = [m.split("{")[0] for m in metrics]
    assert names[0] == "ci_test_cluster_kube_pod_container_resource_requests"
    assert names[1] == "ci_test_cluster_kube_pod_container_resource_requests"
    assert names[2] == "ci_test_cluster_kube_pod_container_resource_limits"
    assert names[3] == "ci_test_cluster_container_memory_working_set_bytes"
    # Verify pod/namespace labels are preserved from prometheus
    assert 'pod="opendatahub-operator-0"' in metrics[0]
    assert 'namespace="redhat-ods-operator"' in metrics[0]
    assert 'resource="cpu"' in metrics[0]
    # Verify job labels are merged
    assert 'build_id="99"' in metrics[0]


def test_extract_ignores_unmatched_metrics():
    lines = [
        '{__name__="some_other_metric"} 1.0 1710000000000',
    ]
    assert extract_test_cluster_metrics(lines, {"build_id": "1"}) == []


def test_extract_includes_test_step_label():
    lines = [
        '{__name__="cluster:cpu_usage_cores:sum"} 3.14 1710000000000',
    ]
    job_labels = {"build_id": "123", "test_step": "my-e2e-step"}
    metrics = extract_test_cluster_metrics(lines, job_labels)
    assert len(metrics) == 1
    assert 'test_step="my-e2e-step"' in metrics[0]


def test_build_node_role_map():
    parsed = [
        ("kube_node_role", {"node": "ip-10-0-1-1.ec2.internal", "role": "master"}, 1.0, 1710000000),
        ("kube_node_role", {"node": "ip-10-0-1-1.ec2.internal", "role": "control-plane"}, 1.0, 1710000000),
        ("kube_node_role", {"node": "ip-10-0-1-2.ec2.internal", "role": "worker"}, 1.0, 1710000000),
        ("kube_node_role", {"node": "ip-10-0-1-2.ec2.internal", "role": "worker"}, 1.0, 1710000030),
        ("machine_cpu_cores", {"instance": "10.0.1.1:10250", "node": "ip-10-0-1-1.ec2.internal"}, 4.0, 1710000000),
    ]
    role_map = _build_node_role_map(parsed)
    assert role_map == {
        "ip-10-0-1-1.ec2.internal": "master",
        "ip-10-0-1-2.ec2.internal": "worker",
    }


def test_extract_enriches_per_node_with_role():
    lines = [
        # Role mapping source (OCP nodes have both master and control-plane roles)
        '{__name__="kube_node_role", node="ip-10-0-1-1.ec2.internal", role="master"} 1 1710000000000',
        '{__name__="kube_node_role", node="ip-10-0-1-1.ec2.internal", role="control-plane"} 1 1710000000000',
        '{__name__="kube_node_role", node="ip-10-0-1-2.ec2.internal", role="worker"} 1 1710000000000',
        # Per-node metric with "node" label (machine_cpu_cores)
        '{__name__="machine_cpu_cores", instance="10.0.1.1:10250", node="ip-10-0-1-1.ec2.internal"} 4 1710000000000',
        '{__name__="machine_cpu_cores", instance="10.0.1.2:10250", node="ip-10-0-1-2.ec2.internal"} 8 1710000000000',
        # Per-node metric with "instance" label in hostname format (memory ratio)
        '{__name__="instance:node_memory_utilisation:ratio", instance="ip-10-0-1-1.ec2.internal"} 0.65 1710000000000',
        '{__name__="instance:node_memory_utilisation:ratio", instance="ip-10-0-1-2.ec2.internal"} 0.15 1710000000000',
        # Cluster-wide metric should NOT get role label
        '{__name__="cluster:cpu_usage_cores:sum"} 5.0 1710000000000',
    ]
    metrics = extract_test_cluster_metrics(lines, {"build_id": "1"})
    # kube_node_role should not be emitted
    assert not any("kube_node_role" in m for m in metrics)
    # machine_cpu_cores should get role from "node" label
    cpu_master = [m for m in metrics if "machine_cpu_cores" in m and "4.0" in m][0]
    assert 'role="master"' in cpu_master
    cpu_worker = [m for m in metrics if "machine_cpu_cores" in m and "8.0" in m][0]
    assert 'role="worker"' in cpu_worker
    # memory ratio should get role from "instance" label
    mem_master = [m for m in metrics if "node_memory_utilisation" in m and "0.65" in m][0]
    assert 'role="master"' in mem_master
    mem_worker = [m for m in metrics if "node_memory_utilisation" in m and "0.15" in m][0]
    assert 'role="worker"' in mem_worker
    # cluster-wide should NOT have role
    cpu_cluster = [m for m in metrics if "cpu_usage_cores_sum" in m][0]
    assert 'role=' not in cpu_cluster


def test_extract_no_role_without_kube_node_role():
    """Per-node metrics work without kube_node_role (no role label added)."""
    lines = [
        '{__name__="machine_cpu_cores", instance="10.0.1.1:10250", node="ip-10-0-1-1.ec2.internal"} 4 1710000000000',
    ]
    metrics = extract_test_cluster_metrics(lines, {"build_id": "1"})
    assert len(metrics) == 1
    assert 'role=' not in metrics[0]


def test_process_skips_without_cluster_claim():
    """Pipeline skips entirely when no clusterClaim.json exists, but still marks done."""
    pipeline, sink, gcs, cache, state = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = None
    assert pipeline.process(ctx) == 0
    ctx.list_artifact_dirs.assert_not_called()
    sink.push.assert_not_called()
    state.mark_done.assert_called_once()


def test_process_skips_without_cache():
    """Pipeline skips when disk cache is disabled, but still marks done."""
    sink = MagicMock()
    gcs = MagicMock(spec=CachedGCSClient)
    state = MagicMock(spec=ScrapeState)
    pipeline = TestClusterMetricsPipeline(sink, gcs, None, state)
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    assert pipeline.process(ctx) == 0
    ctx.fetch_artifact.assert_not_called()
    state.mark_done.assert_called_once()


def test_process_no_test_steps():
    """Pipeline returns 0 when no test step directories exist, but still marks done."""
    pipeline, sink, gcs, cache, state = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(
        ["build-logs", "build-resources", "release"])
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()
    state.mark_done.assert_called_once()


def test_process_no_prometheus_tar():
    """Pipeline returns 0 when step exists but prometheus.tar doesn't."""
    pipeline, sink, gcs, cache, state = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-step"])
    gcs.ensure_staged.return_value = None  # prometheus.tar not found
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()


@patch("scraper.test_cluster_metrics._run_promtool")
def test_process_with_promtool(mock_promtool, tmp_path):
    """Pipeline submits promtool work to async pool; drain() completes it."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum", prometheus="k8s"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)

    # Create a minimal valid tar on disk (in staging)
    import io
    import tarfile
    tar_file = cache._staging / "test-tar"
    tar_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(tar_file), mode="w") as tf:
        data = b"fake tsdb data"
        info = tarfile.TarInfo(name="wal/00000001")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = tar_file
    ctx.artifact_gcs_path.return_value = "pr-logs/pull/org_repo/42/job/123/artifacts/my-e2e-step/gather-extra/artifacts/metrics/prometheus.tar"

    count = pipeline.process(ctx)
    assert count == 0  # Returns immediately; work is async
    pipeline.drain()  # Wait for the promtool pool to finish
    # Health metrics (none found since fetch returns "{}") + tar metrics
    assert sink.push.call_count >= 1
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in pushed[0]
    assert 'test_step="my-e2e-step"' in pushed[0]
    # Verify .metrics cache was written
    gcs_path = ctx.artifact_gcs_path.return_value
    cached = _read_cached_metrics(cache, gcs_path, pipeline.version)
    assert cached is not None


# --- .metrics cache tests ---

def test_cache_hit(tmp_path):
    """Pipeline serves metrics from .metrics cache without promtool."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    cached_content = 'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123",metrics_source="tsdb"} 4.2 1710000000\n'
    cache.get_processed.return_value = cached_content

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    sink.push.assert_called_once()
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in pushed[0]
    # Should NOT try to download the tar
    gcs.ensure_staged.assert_not_called()


def test_cache_stale_version(tmp_path):
    """Pipeline reprocesses when .metrics version doesn't match."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    # get_processed returns None for version mismatch
    cache.get_processed.return_value = None
    gcs.ensure_staged.return_value = None  # tar not available

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    # Stale cache was rejected, tried to stage tar (returned None)
    gcs.ensure_staged.assert_called_once()
    sink.push.assert_not_called()


def test_cache_empty_metrics():
    """Pipeline handles empty .metrics cache (skipped tar) gracefully."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    # Empty content = tar was skipped (oversized, etc.)
    cache.get_processed.return_value = ""

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    # No push for empty metrics
    sink.push.assert_not_called()
    gcs.ensure_staged.assert_not_called()


# --- Unit tests for cache read/write helpers ---

def test_read_cached_metrics_hit(tmp_path):
    """get_processed returns content when version matches."""
    cache = ArtifactCache(str(tmp_path / "cache"))
    gcs_path = "some/path/prometheus.tar"
    version = "1.1"

    _write_cached_metrics(cache, gcs_path, version, ["metric1 1.0", "metric2 2.0"])
    result = _read_cached_metrics(cache, gcs_path, version)
    assert result == ["metric1 1.0", "metric2 2.0"]


def test_read_cached_metrics_stale(tmp_path):
    """get_processed returns None when version doesn't match."""
    cache = ArtifactCache(str(tmp_path / "cache"))
    gcs_path = "some/path/prometheus.tar"

    _write_cached_metrics(cache, gcs_path, "old", ["metric1 1.0"])
    result = _read_cached_metrics(cache, gcs_path, "new")
    assert result is None


def test_read_cached_metrics_empty(tmp_path):
    """get_processed returns empty list for skipped tars."""
    cache = ArtifactCache(str(tmp_path / "cache"))
    gcs_path = "some/path/prometheus.tar"
    version = "1.1"

    _write_cached_metrics(cache, gcs_path, version, [])
    result = _read_cached_metrics(cache, gcs_path, version)
    assert result == []


def test_read_cached_metrics_missing(tmp_path):
    """get_processed returns None when no .metrics file exists."""
    cache = ArtifactCache(str(tmp_path / "cache"))
    result = _read_cached_metrics(cache, "nonexistent/path", "1.0")
    assert result is None


# --- worker tests (tar staged, unstaged after processing) ---

def _make_real_pipeline(tmp_path):
    """Create a pipeline with real ArtifactCache and ScrapeState for cache path tests."""
    cache = ArtifactCache(str(tmp_path / "cache"))
    state = ScrapeState(str(tmp_path / "state.db"))
    gcs = MagicMock(spec=CachedGCSClient)
    sink = MagicMock()
    pipeline = TestClusterMetricsPipeline(sink, gcs, cache, state)
    return pipeline, sink, gcs, cache


def _create_staged_tar(cache, gcs_path, valid=True):
    """Create a tar file in the staging directory. Returns the Path."""
    import io
    import tarfile
    import hashlib
    import uuid

    path_hash = hashlib.sha256(gcs_path.encode()).hexdigest()[:16]
    unique = uuid.uuid4().hex[:8]
    tar_file = cache._staging / f"{path_hash}-{unique}"
    tar_file.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        with tarfile.open(str(tar_file), mode="w") as tf:
            data = b"fake tsdb data"
            info = tarfile.TarInfo(name="wal/00000001")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    else:
        tar_file.write_bytes(b"not a valid tar archive")
    return tar_file


GCS_PATH = "pr-logs/pull/org_repo/42/job/123/artifacts/my-e2e-step/gather-extra/artifacts/metrics/prometheus.tar"


@patch("scraper.test_cluster_metrics._run_promtool")
def test_tar_deleted_after_successful_processing(mock_promtool, tmp_path):
    """Worker unstages tar after successful processing."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    tar_file = _create_staged_tar(cache, GCS_PATH)
    assert tar_file.exists()

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    assert not tar_file.exists(), "staged tar should be unstaged after successful processing"


def test_tar_deleted_after_corrupt_tar(tmp_path):
    """Worker unstages corrupt tar despite TarError."""
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    tar_file = _create_staged_tar(cache, GCS_PATH, valid=False)
    assert tar_file.exists()

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    assert not tar_file.exists(), "corrupt staged tar should still be unstaged"


@patch("scraper.test_cluster_metrics._run_promtool")
def test_tar_deleted_after_promtool_failure(mock_promtool, tmp_path):
    """Worker unstages tar even when promtool returns nothing."""
    mock_promtool.return_value = []  # simulates promtool failure/timeout
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    tar_file = _create_staged_tar(cache, GCS_PATH)

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    assert not tar_file.exists(), "staged tar should be unstaged even when promtool returns nothing"


@patch("scraper.test_cluster_metrics.extract_test_cluster_metrics", side_effect=RuntimeError("boom"))
@patch("scraper.test_cluster_metrics._run_promtool")
def test_tar_deleted_after_unexpected_exception(mock_promtool, mock_extract, tmp_path):
    """Worker unstages tar even on unexpected exceptions."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    tar_file = _create_staged_tar(cache, GCS_PATH)

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    assert not tar_file.exists(), "staged tar should be unstaged even on unexpected exceptions"


@patch("scraper.test_cluster_metrics._run_promtool")
def test_worker_cache_hit_skips_promtool(mock_promtool, tmp_path):
    """Worker uses .metrics cache written by another scraper, skipping promtool."""
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)

    # Simulate another scraper already processing this build.
    _write_cached_metrics(cache, GCS_PATH, pipeline.version, [
        'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123"} 4.2 1710000000',
    ])
    tar_file = _create_staged_tar(cache, GCS_PATH)

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    assert not tar_file.exists(), "staged tar should be unstaged on cache hit"
    mock_promtool.assert_not_called()
    sink.push.assert_called_once()


@patch("scraper.test_cluster_metrics._run_promtool")
def test_worker_cache_hit_tar_already_deleted(mock_promtool, tmp_path):
    """Worker handles tar deleted by other scraper when .metrics already exists."""
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)

    _write_cached_metrics(cache, GCS_PATH, pipeline.version, [
        'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123"} 4.2 1710000000',
    ])
    # Tar does NOT exist (other scraper's worker already deleted it).
    tar_file = cache._staging / "nonexistent"
    assert not tar_file.exists()

    step_labels = {**SAMPLE_LABELS, "test_step": "my-e2e-step"}
    pipeline._pool.submit(
        pipeline._process_tar_from_path, tar_file, GCS_PATH, step_labels,
        "123", "42", "my-e2e-step",
    )
    pipeline.drain()

    mock_promtool.assert_not_called()
    sink.push.assert_called_once()


# --- Prometheus exposition format parser tests ---

def test_parse_exposition_line_basic():
    line = 'kube_node_status_allocatable{node="ip-10-0-1-1",resource="cpu",unit="core"} 4.0 1710849600000'
    result = parse_exposition_line(line)
    assert result is not None
    name, labels, value, ts = result
    assert name == "kube_node_status_allocatable"
    assert labels == {"node": "ip-10-0-1-1", "resource": "cpu", "unit": "core"}
    assert value == 4.0
    assert ts == 1710849600


def test_parse_exposition_line_no_labels():
    line = 'cluster_healthy 1.0 1710849600000'
    result = parse_exposition_line(line)
    assert result is not None
    name, labels, value, ts = result
    assert name == "cluster_healthy"
    assert labels == {}
    assert value == 1.0
    assert ts == 1710849600


def test_parse_exposition_line_no_timestamp():
    line = 'cluster_healthy 1.0'
    result = parse_exposition_line(line)
    assert result is not None
    name, labels, value, ts = result
    assert name == "cluster_healthy"
    assert value == 1.0
    assert ts is None


def test_parse_exposition_line_comments():
    assert parse_exposition_line('# HELP cluster_healthy Overall cluster health') is None
    assert parse_exposition_line('# TYPE cluster_healthy gauge') is None


def test_parse_exposition_line_empty():
    assert parse_exposition_line('') is None
    assert parse_exposition_line('   ') is None
    assert parse_exposition_line('\n') is None


# --- Health metrics extraction tests ---

def test_extract_health_metrics_basic():
    content = (
        '# HELP cluster_healthy Overall cluster health\n'
        '# TYPE cluster_healthy gauge\n'
        'cluster_healthy 1.0 1710849600000\n'
        'machine_cpu_cores{node="ip-10-0-1-1"} 4.0 1710849600000\n'
    )
    job_labels = {"build_id": "123", "pr_number": "42"}
    metrics = extract_health_metrics(content, job_labels)
    assert len(metrics) == 2
    assert "ci_test_cluster_cluster_healthy" in metrics[0]
    assert 'build_id="123"' in metrics[0]
    assert 'metrics_source="health"' in metrics[0]
    assert "ci_test_cluster_machine_cpu_cores" in metrics[1]
    assert 'metrics_source="health"' in metrics[1]


def test_extract_health_metrics_role_enrichment():
    content = (
        'kube_node_role{node="ip-10-0-1-1",role="master"} 1.0 1710849600000\n'
        'kube_node_role{node="ip-10-0-1-2",role="worker"} 1.0 1710849600000\n'
        'machine_cpu_cores{node="ip-10-0-1-1"} 4.0 1710849600000\n'
        'machine_cpu_cores{node="ip-10-0-1-2"} 8.0 1710849600000\n'
        'kube_node_status_allocatable{node="ip-10-0-1-1",resource="cpu",unit="core"} 3.5 1710849600000\n'
    )
    job_labels = {"build_id": "1"}
    metrics = extract_health_metrics(content, job_labels)
    # kube_node_role should not be emitted
    assert not any("kube_node_role" in m for m in metrics)
    # Per-node metrics should get role labels
    cpu_master = [m for m in metrics if "machine_cpu_cores" in m and "4.0" in m][0]
    assert 'role="master"' in cpu_master
    cpu_worker = [m for m in metrics if "machine_cpu_cores" in m and "8.0" in m][0]
    assert 'role="worker"' in cpu_worker
    # kube_node_status_allocatable should also get role
    alloc = [m for m in metrics if "kube_node_status_allocatable" in m][0]
    assert 'role="master"' in alloc


def test_extract_health_metrics_comma_role():
    """Health metrics with comma-separated role like 'control-plane,master' are recognized as master."""
    content = (
        'kube_node_role{node="ip-10-0-1-1",role="control-plane,master"} 1.0 1710849600000\n'
        'kube_node_role{node="ip-10-0-1-2",role="worker"} 1.0 1710849600000\n'
        'machine_cpu_cores{node="ip-10-0-1-1"} 4.0 1710849600000\n'
        'machine_cpu_cores{node="ip-10-0-1-2"} 8.0 1710849600000\n'
    )
    job_labels = {"build_id": "1"}
    metrics = extract_health_metrics(content, job_labels)
    cpu_master = [m for m in metrics if "machine_cpu_cores" in m and "4.0" in m][0]
    assert 'role="master"' in cpu_master
    cpu_worker = [m for m in metrics if "machine_cpu_cores" in m and "8.0" in m][0]
    assert 'role="worker"' in cpu_worker


def test_extract_health_metrics_pod_no_node():
    """Per-pod metrics without node label get no role (graceful)."""
    content = (
        'kube_node_role{node="ip-10-0-1-1",role="master"} 1.0 1710849600000\n'
        'kube_pod_status_phase{namespace="default",pod="my-pod",phase="Running"} 1.0 1710849600000\n'
    )
    job_labels = {"build_id": "1"}
    metrics = extract_health_metrics(content, job_labels)
    pod_metric = [m for m in metrics if "kube_pod_status_phase" in m][0]
    assert 'role=' not in pod_metric


def test_extract_health_metrics_filters_unknown():
    content = (
        'some_unknown_metric{foo="bar"} 99 1710849600000\n'
        'cluster_healthy 1.0 1710849600000\n'
    )
    metrics = extract_health_metrics(content, {"build_id": "1"})
    assert len(metrics) == 1
    assert "cluster_healthy" in metrics[0]


def test_extract_health_metrics_empty_content():
    assert extract_health_metrics("", {"build_id": "1"}) == []


def test_extract_health_metrics_malformed_lines():
    content = (
        'cluster_healthy 1.0 1710849600000\n'
        'this is not valid prometheus format\n'
        '{__name__="promtool_format"} 1.0 1710000000000\n'
        'machine_cpu_cores{node="x"} 4.0 1710849600000\n'
        '\n'
    )
    metrics = extract_health_metrics(content, {"build_id": "1"})
    assert len(metrics) == 2
    assert "cluster_healthy" in metrics[0]
    assert "machine_cpu_cores" in metrics[1]


def test_health_output_names():
    assert _HEALTH_OUTPUT_NAMES["cluster_healthy"] == "ci_test_cluster_cluster_healthy"
    assert _HEALTH_OUTPUT_NAMES["kube_node_status_allocatable"] == "ci_test_cluster_kube_node_status_allocatable"
    assert _HEALTH_OUTPUT_NAMES["kube_pod_status_phase"] == "ci_test_cluster_kube_pod_status_phase"
    assert _HEALTH_OUTPUT_NAMES["kube_deployment_status_replicas"] == "ci_test_cluster_kube_deployment_status_replicas"
    assert "kube_node_role" not in _HEALTH_OUTPUT_NAMES


def test_filter_overlapping():
    """Overlapping metrics (machine_cpu_cores etc) are removed; recording rules kept."""
    metrics = [
        'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="1",metrics_source="tsdb"} 4.2 1710000000',
        'ci_test_cluster_machine_cpu_cores{build_id="1",metrics_source="tsdb",node="x"} 8.0 1710000000',
        'ci_test_cluster_kube_pod_container_resource_requests{build_id="1",metrics_source="tsdb"} 0.5 1710000000',
        'ci_test_cluster_cluster_capacity_cpu_cores_sum{build_id="1",metrics_source="tsdb"} 24.0 1710000000',
    ]
    filtered = _filter_overlapping(metrics)
    names = [m.split("{")[0] for m in filtered]
    # Recording rules kept
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in names
    assert "ci_test_cluster_cluster_capacity_cpu_cores_sum" in names
    # Overlapping metrics removed
    assert "ci_test_cluster_machine_cpu_cores" not in names
    assert "ci_test_cluster_kube_pod_container_resource_requests" not in names


def test_overlapping_output_names():
    """Verify the computed overlap matches expected metrics."""
    assert "ci_test_cluster_machine_cpu_cores" in _OVERLAPPING_OUTPUT_NAMES
    assert "ci_test_cluster_kube_pod_container_resource_requests" in _OVERLAPPING_OUTPUT_NAMES
    assert "ci_test_cluster_container_memory_working_set_bytes" in _OVERLAPPING_OUTPUT_NAMES
    # Recording rules should NOT be in overlap
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" not in _OVERLAPPING_OUTPUT_NAMES
    assert "ci_test_cluster_cluster_capacity_cpu_cores_sum" not in _OVERLAPPING_OUTPUT_NAMES
    # Health-only metrics should NOT be in overlap
    assert "ci_test_cluster_cluster_healthy" not in _OVERLAPPING_OUTPUT_NAMES


# --- Pipeline integration tests for health metrics ---

def _list_dirs_side_effect(test_steps, sub_steps=None):
    """Return a side_effect for ctx.list_artifact_dirs that distinguishes top-level vs sub-step listings."""
    if sub_steps is None:
        sub_steps = ["e2e", "gather-extra"]

    def side_effect(prefix):
        if prefix == "artifacts/":
            return test_steps
        # Sub-step listing for any test step
        return sub_steps
    return side_effect


def test_process_health_only():
    """Step has health file but no prometheus.tar; metrics pushed, state marked done."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        if path.endswith(_HEALTH_METRICS_FILE):
            return 'cluster_healthy 1.0 1710849600000\n'
        return None

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = None  # no prometheus.tar

    pipeline.process(ctx)
    pipeline.drain()

    # Health metrics pushed
    sink.push.assert_called_once()
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_healthy" in pushed[0]
    assert 'metrics_source="health"' in pushed[0]
    # State marked done
    state.mark_done.assert_called_once()


@patch("scraper.test_cluster_metrics._run_promtool")
def test_process_both_sources(mock_promtool, tmp_path):
    """Step has both health file and prometheus.tar; both contribute metrics."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    tar_file = _create_staged_tar(cache, GCS_PATH)

    health_content = 'cluster_healthy 1.0 1710849600000\n'

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        if path.endswith(_HEALTH_METRICS_FILE):
            return health_content
        return None

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = tar_file
    ctx.artifact_gcs_path.return_value = GCS_PATH

    pipeline.process(ctx)
    pipeline.drain()

    # Two pushes: health (sync) + tar (async)
    assert sink.push.call_count == 2
    # First push: health metrics
    health_push = sink.push.call_args_list[0][0][0]
    assert any("cluster_healthy" in m for m in health_push)
    assert any('metrics_source="health"' in m for m in health_push)
    # Second push: tar metrics -- recording rules only, overlapping metrics filtered
    tar_push = sink.push.call_args_list[1][0][0]
    assert any("cpu_usage_cores_sum" in m for m in tar_push)
    assert any('metrics_source="tsdb"' in m for m in tar_push)
    # Overlapping metrics should NOT be in tar push
    assert not any("ci_test_cluster_machine_cpu_cores" in m for m in tar_push)


def test_process_no_health_file():
    """Step has no health file; falls through to existing tar logic unchanged."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        return None  # no health file, no other artifacts

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = None  # no prometheus.tar either

    pipeline.process(ctx)
    pipeline.drain()

    sink.push.assert_not_called()


@patch("scraper.test_cluster_metrics._run_promtool")
def test_process_health_no_early_mark(mock_promtool, tmp_path):
    """Step A has health only, step B has tar in pool; state NOT marked done until pool worker completes."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, cache = _make_real_pipeline(tmp_path)
    gcs_path_b = "pr-logs/pull/org_repo/42/job/123/artifacts/step-b/gather-extra/artifacts/metrics/prometheus.tar"
    tar_file_b = _create_staged_tar(cache, gcs_path_b)

    health_content = 'cluster_healthy 1.0 1710849600000\n'

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        if "step-a" in path and path.endswith(_HEALTH_METRICS_FILE):
            return health_content
        return None

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["step-a", "step-b"])
    gcs.ensure_staged.side_effect = lambda path: tar_file_b if "step-b" in path else None
    ctx.artifact_gcs_path.side_effect = lambda path: gcs_path_b if "step-b" in path else path

    pipeline.process(ctx)
    pipeline.drain()

    # State should be marked done after drain (pool worker completes)
    state = pipeline._state
    assert not state.should_process("123", pipeline.name, pipeline.version)


def test_health_error_does_not_block_tar():
    """GCS error during health fetch is caught; tar processing continues."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        if path.endswith(_HEALTH_METRICS_FILE):
            raise ConnectionError("simulated GCS error")
        return None

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = None  # no tar either

    # Should not raise
    pipeline.process(ctx)
    pipeline.drain()


def test_health_push_failure_does_not_set_pushed():
    """If sink.push() raises during health metrics, health_pushed stays False."""
    pipeline, sink, gcs, cache, state = _make_pipeline()
    sink.push.side_effect = ConnectionError("sink error")

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        if path.endswith(_HEALTH_METRICS_FILE):
            return 'cluster_healthy 1.0 1710849600000\n'
        return None

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["my-e2e-step"])
    gcs.ensure_staged.return_value = None  # no tar

    pipeline.process(ctx)
    pipeline.drain()

    # State should NOT be marked done because push raised before health_pushed was set.
    state.mark_done.assert_not_called()


def test_process_all_cache_hits_no_health():
    """All steps have tar cache hits; state marked done for each."""
    pipeline, sink, gcs, cache, state = _make_pipeline()

    cached_content = 'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123",metrics_source="tsdb"} 4.2 1710000000\n'
    cache.get_processed.return_value = cached_content

    def fetch_side_effect(path):
        if path.endswith("clusterClaim.json"):
            return "{}"
        return None  # no health file

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.side_effect = fetch_side_effect
    ctx.list_artifact_dirs.side_effect = _list_dirs_side_effect(["step-a", "step-b"])
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    # State marked done by _submit_step cache-hit path (2 times, one per step).
    assert state.mark_done.call_count == 2
    # Metrics pushed from cache (2 times)
    assert sink.push.call_count == 2
