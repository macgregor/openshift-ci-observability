from scraper.metrics import (
    flatten_numeric_fields,
    extract_string_fields,
    parse_timestamp_best_effort,
    sanitize_metric_name,
    escape_label_value,
    format_prometheus_line,
    parse_k8s_quantity,
    apply_known_transforms,
    CANONICAL_ALIASES,
    extract_metrics_from_entry,
    convert_to_metrics,
    _extract_step_offsets,
)


def test_flatten_numeric_fields():
    obj = {"a": 1, "b": 2.5, "c": "skip", "d": True, "nested": {"x": 10, "y": "skip"}}
    result = dict(flatten_numeric_fields(obj))
    assert result == {"a": 1, "b": 2.5, "nested_x": 10}
    assert "c" not in result
    assert "d" not in result


def test_extract_string_fields():
    entry = {
        "name": "test-pod",
        "pod_phase": "Running",
        "success": True,
        "long_value": "x" * 200,
        "additional_context": {"should": "skip"},
        "count": 42,
    }
    result = extract_string_fields(entry)
    assert result["name"] == "test-pod"
    assert result["pod_phase"] == "Running"
    assert result["success"] == "true"
    assert "long_value" not in result
    assert "additional_context" not in result
    assert "count" not in result


def test_parse_timestamp_best_effort():
    assert parse_timestamp_best_effort({"timestamp": "2024-01-01T00:00:00Z"}) is not None
    assert parse_timestamp_best_effort({"completion_time": "2024-06-15T12:30:00Z"}) is not None
    assert parse_timestamp_best_effort({"start_time": "2024-06-15T12:30:00+00:00"}) is not None
    assert parse_timestamp_best_effort({"from": "2024-01-01T00:00:00Z"}) is not None
    assert parse_timestamp_best_effort({}) is None
    assert parse_timestamp_best_effort({"timestamp": "not-a-date"}) is None


def test_sanitize_metric_name():
    assert sanitize_metric_name("hello-world") == "hello_world"
    assert sanitize_metric_name("__leading__") == "leading"
    assert sanitize_metric_name("a___b") == "a_b"
    assert sanitize_metric_name("MixedCase") == "mixedcase"
    assert sanitize_metric_name("with.dots") == "with_dots"


def test_escape_label_value():
    assert escape_label_value('hello') == 'hello'
    assert escape_label_value('back\\slash') == 'back\\\\slash'
    assert escape_label_value('with"quote') == 'with\\"quote'
    assert escape_label_value('new\nline') == 'new\\nline'


def test_format_prometheus_line():
    line = format_prometheus_line("test_metric", {"env": "prod"}, 42, 1000)
    assert line == 'test_metric{env="prod"} 42 1000'


def test_format_prometheus_line_empty_labels():
    line = format_prometheus_line("test_metric", {}, 42, None)
    assert line == "test_metric 42"


def test_format_prometheus_line_empty_name():
    assert format_prometheus_line("", {}, 42, None) is None


def test_format_prometheus_line_no_timestamp():
    line = format_prometheus_line("test_metric", {"a": "b"}, 1, None)
    assert " 1" in line
    assert line.endswith(" 1")


def test_parse_k8s_quantity():
    assert parse_k8s_quantity("1Gi") == 1024 ** 3
    assert parse_k8s_quantity("500m") == 0.5
    assert parse_k8s_quantity("1024") == 1024.0
    assert parse_k8s_quantity("100Mi") == 100 * 1024 ** 2
    assert parse_k8s_quantity("2k") == 2000.0
    assert parse_k8s_quantity("invalid") is None
    assert parse_k8s_quantity(42) is None


def test_apply_known_transforms():
    assert apply_known_transforms("pods", "scheduling_latency", 1_000_000_000) == 1.0
    assert apply_known_transforms("nodes", "resources_cpu", "500m") == 0.5
    assert apply_known_transforms("events", "some_field", 42) == 42


def test_canonical_aliases():
    entry = {"message": {"annotations": {"duration_seconds": 120}}, "timestamp": "2024-01-01T00:00:00Z"}
    job_labels = {"build_id": "test"}
    metrics = extract_metrics_from_entry("events", entry, job_labels)
    metric_names = [m.split("{")[0] for m in metrics]
    assert "ci_step_duration_seconds" in metric_names


def test_convert_to_metrics_full(metrics_json, sample_job_labels):
    metrics = convert_to_metrics(metrics_json, sample_job_labels)
    assert len(metrics) > 0
    names = [m.split("{")[0] for m in metrics]
    assert any("ci_events" in n for n in names)
    assert any("ci_pods" in n for n in names)
    assert any(sample_job_labels["build_id"] in m for m in metrics)


def test_extract_step_offsets():
    events = [
        {"from": "2024-01-01T00:00:00Z", "to": "2024-01-01T00:01:00Z", "name": "step1"},
        {"from": "2024-01-01T00:00:30Z", "to": "2024-01-01T00:02:00Z", "name": "step2"},
    ]
    job_labels = {"build_id": "test"}
    metrics = _extract_step_offsets(events, job_labels)
    assert len(metrics) > 0
    names = [m.split("{")[0] for m in metrics]
    assert "ci_step_relative_start_seconds" in names
    assert "ci_step_relative_end_seconds" in names
