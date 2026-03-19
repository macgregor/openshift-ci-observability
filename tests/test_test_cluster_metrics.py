from unittest.mock import MagicMock, patch

import requests

from scraper.test_cluster_metrics import (
    discover_test_steps,
    parse_promtool_line,
    extract_test_cluster_metrics,
    _build_node_role_map,
    _delete_cached_tar,
    _read_cached_metrics,
    _write_cached_metrics,
    _OUTPUT_NAMES,
    TestClusterMetricsPipeline,
)
from scraper.gcs import GCSClient


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


def _make_pipeline():
    """Create a TestClusterMetricsPipeline with mock dependencies."""
    sink = MagicMock()
    gcs = MagicMock(spec=GCSClient)
    gcs.has_cache = True
    gcs.read_processed.return_value = None
    session = MagicMock(spec=requests.Session)
    pipeline = TestClusterMetricsPipeline(sink, gcs, session, VM_URL)
    return pipeline, sink, gcs, session


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
    assert "3.14" in metrics[0]
    assert "ci_test_cluster_machine_cpu_cores" in metrics[1]
    assert 'pr_number="42"' in metrics[1]


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
    """Pipeline skips entirely when no clusterClaim.json exists."""
    pipeline, sink, gcs, _ = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = None
    assert pipeline.process(ctx) == 0
    ctx.list_artifact_dirs.assert_not_called()
    sink.push.assert_not_called()


def test_process_skips_without_cache():
    """Pipeline skips when disk cache is disabled."""
    pipeline, sink, gcs, _ = _make_pipeline()
    gcs.has_cache = False
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    assert pipeline.process(ctx) == 0
    ctx.fetch_artifact.assert_not_called()


def test_process_no_test_steps():
    """Pipeline returns 0 when no test step directories exist."""
    pipeline, sink, gcs, _ = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["build-logs", "build-resources", "release"]
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()


def test_process_no_prometheus_tar():
    """Pipeline returns 0 when step exists but prometheus.tar doesn't."""
    pipeline, sink, gcs, _ = _make_pipeline()
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-step"]
    ctx.artifact_cache_path.return_value = None  # prometheus.tar not found
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()


@patch("scraper.test_cluster_metrics._run_promtool")
@patch("scraper.scraper.push_pipeline_sentinel")
def test_process_with_promtool(mock_sentinel, mock_promtool, tmp_path):
    """Pipeline submits promtool work to async pool; drain() completes it."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum", prometheus="k8s"} 4.2 1710000000000',
    ]
    pipeline, sink, gcs, _ = _make_pipeline()

    # Create a minimal valid tar on disk
    import io
    import tarfile
    tar_file = tmp_path / "prometheus.tar"
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
    ctx.list_artifact_dirs.return_value = ["my-e2e-step"]
    ctx.artifact_cache_path.return_value = tar_file
    ctx.artifact_gcs_path.return_value = "pr-logs/pull/org_repo/42/job/123/artifacts/my-e2e-step/gather-extra/artifacts/metrics/prometheus.tar"

    count = pipeline.process(ctx)
    assert count == 0  # Returns immediately; work is async
    pipeline.drain()  # Wait for the promtool pool to finish
    sink.push.assert_called_once()
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in pushed[0]
    assert 'test_step="my-e2e-step"' in pushed[0]
    # Verify .metrics cache was written
    gcs.write_processed.assert_called_once()
    # Verify sentinel was pushed
    mock_sentinel.assert_called_once()


# --- .metrics cache tests ---

def test_cache_hit(tmp_path):
    """Pipeline serves metrics from .metrics cache without promtool."""
    pipeline, sink, gcs, _ = _make_pipeline()

    cached_content = f"# version={pipeline.version}\n" \
        'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123"} 4.2 1710000000\n'
    gcs.read_processed.return_value = cached_content

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-e2e-step"]
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    sink.push.assert_called_once()
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in pushed[0]
    # Should NOT try to download the tar
    ctx.artifact_cache_path.assert_not_called()


def test_cache_stale_version(tmp_path):
    """Pipeline reprocesses when .metrics version doesn't match."""
    pipeline, sink, gcs, _ = _make_pipeline()

    stale_content = "# version=old\n" \
        'ci_test_cluster_cluster_cpu_usage_cores_sum{build_id="123"} 4.2 1710000000\n'
    gcs.read_processed.return_value = stale_content

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-e2e-step"]
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"
    ctx.artifact_cache_path.return_value = None  # tar not available

    pipeline.process(ctx)
    pipeline.drain()

    # Stale cache was rejected, tried to fetch tar (returned None)
    ctx.artifact_cache_path.assert_called_once()
    sink.push.assert_not_called()


def test_cache_empty_metrics():
    """Pipeline handles empty .metrics cache (skipped tar) gracefully."""
    pipeline, sink, gcs, _ = _make_pipeline()

    # Empty .metrics = tar was skipped (oversized, etc.)
    cached_content = f"# version={pipeline.version}\n"
    gcs.read_processed.return_value = cached_content

    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.build.pr = "42"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-e2e-step"]
    ctx.artifact_gcs_path.return_value = "some/path/prometheus.tar"

    pipeline.process(ctx)
    pipeline.drain()

    # No push for empty metrics
    sink.push.assert_not_called()
    ctx.artifact_cache_path.assert_not_called()


# --- Unit tests for cache read/write helpers ---

def test_read_cached_metrics_hit(tmp_path):
    """read_processed returns content when version matches."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    gcs_path = "some/path/prometheus.tar"
    version = "1.1"

    _write_cached_metrics(gcs, gcs_path, version, ["metric1 1.0", "metric2 2.0"])
    result = _read_cached_metrics(gcs, gcs_path, version)
    assert result == ["metric1 1.0", "metric2 2.0"]


def test_read_cached_metrics_stale(tmp_path):
    """read_processed returns None when version doesn't match."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    gcs_path = "some/path/prometheus.tar"

    _write_cached_metrics(gcs, gcs_path, "old", ["metric1 1.0"])
    result = _read_cached_metrics(gcs, gcs_path, "new")
    assert result is None


def test_read_cached_metrics_empty(tmp_path):
    """read_processed returns empty list for skipped tars."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    gcs_path = "some/path/prometheus.tar"
    version = "1.1"

    _write_cached_metrics(gcs, gcs_path, version, [])
    result = _read_cached_metrics(gcs, gcs_path, version)
    assert result == []


def test_read_cached_metrics_missing(tmp_path):
    """read_processed returns None when no .metrics file exists."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    result = _read_cached_metrics(gcs, "nonexistent/path", "1.0")
    assert result is None


# --- tar deletion tests ---

def test_delete_cached_tar(tmp_path):
    """Tar file is deleted after .metrics extraction."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    gcs_path = "some/path/prometheus.tar"
    tar_file = tmp_path / gcs_path
    tar_file.parent.mkdir(parents=True, exist_ok=True)
    tar_file.write_bytes(b"fake tar data")

    _delete_cached_tar(gcs, gcs_path)
    assert not tar_file.exists()


def test_delete_cached_tar_missing(tmp_path):
    """No error when tar doesn't exist."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=str(tmp_path))
    _delete_cached_tar(gcs, "nonexistent/path/prometheus.tar")


def test_delete_cached_tar_no_cache():
    """No error when cache is disabled."""
    gcs = GCSClient(requests.Session(), "bucket", cache_dir=None)
    _delete_cached_tar(gcs, "some/path/prometheus.tar")
