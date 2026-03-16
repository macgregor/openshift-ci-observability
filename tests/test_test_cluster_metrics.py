from unittest.mock import MagicMock, patch

from scraper.test_cluster_metrics import (
    discover_test_steps,
    parse_promtool_line,
    extract_test_cluster_metrics,
    _OUTPUT_NAMES,
    TestClusterMetricsPipeline,
)


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


def test_process_skips_without_cluster_claim():
    """Pipeline skips entirely when no clusterClaim.json exists."""
    sink = MagicMock()
    pipeline = TestClusterMetricsPipeline(sink)
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = None
    assert pipeline.process(ctx) == 0
    ctx.list_artifact_dirs.assert_not_called()
    sink.push.assert_not_called()


def test_process_no_test_steps():
    """Pipeline returns 0 when no test step directories exist."""
    sink = MagicMock()
    pipeline = TestClusterMetricsPipeline(sink)
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["build-logs", "build-resources", "release"]
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()


def test_process_no_prometheus_tar():
    """Pipeline returns 0 when step exists but prometheus.tar doesn't."""
    sink = MagicMock()
    pipeline = TestClusterMetricsPipeline(sink)
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-step"]
    ctx.fetch_artifact_binary.return_value = None  # prometheus.tar not found
    assert pipeline.process(ctx) == 0
    sink.push.assert_not_called()


@patch("scraper.test_cluster_metrics._run_promtool")
def test_process_with_promtool(mock_promtool):
    """Pipeline processes promtool output correctly with step label."""
    mock_promtool.return_value = [
        '{__name__="cluster:cpu_usage_cores:sum", prometheus="k8s"} 4.2 1710000000000',
    ]
    sink = MagicMock()
    pipeline = TestClusterMetricsPipeline(sink)
    ctx = MagicMock()
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "123"
    ctx.fetch_artifact.return_value = "{}"  # clusterClaim exists
    ctx.list_artifact_dirs.return_value = ["my-e2e-step"]
    # Create a minimal valid tar in memory
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"fake tsdb data"
        info = tarfile.TarInfo(name="wal/00000001")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    ctx.fetch_artifact_binary.return_value = buf.getvalue()
    count = pipeline.process(ctx)
    assert count == 1
    sink.push.assert_called_once()
    pushed = sink.push.call_args[0][0]
    assert len(pushed) == 1
    assert "ci_test_cluster_cluster_cpu_usage_cores_sum" in pushed[0]
    assert 'test_step="my-e2e-step"' in pushed[0]
