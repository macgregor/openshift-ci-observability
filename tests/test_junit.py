import json
from pathlib import Path
from unittest.mock import MagicMock

from scraper.junit import JunitPipeline, parse_junit_xml, extract_test_names, filter_leaf_tests

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name):
    with open(FIXTURES / name) as f:
        return f.read()


def test_parse_junit_xml_passing():
    content = _load_fixture("junit_operator.xml")
    suite_attrs, cases = parse_junit_xml(content)
    assert suite_attrs["name"] == "step graph"
    assert len(cases) == 10
    for c in cases:
        assert c["status"] == "passed"
        assert c["failure_message"] is None
        assert float(c["time"]) >= 0


def test_parse_junit_xml_with_failures():
    content = _load_fixture("junit_operator_failure.xml")
    suite_attrs, cases = parse_junit_xml(content)
    failed = [c for c in cases if c["status"] == "failed"]
    assert len(failed) == 1
    assert "timed out waiting" in failed[0]["failure_message"]
    assert failed[0]["name"] == "Run multi-stage test opendatahub-operator-e2e"


def test_parse_junit_xml_skipped():
    content = _load_fixture("junit_report.xml")
    suite_attrs, cases = parse_junit_xml(content)
    skipped = [c for c in cases if c["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["name"].endswith("Validate_Dashboard_CRD")


def test_extract_test_names():
    content = _load_fixture("junit_operator.xml")
    _, cases = parse_junit_xml(content)
    names = extract_test_names(cases)
    assert names == ["opendatahub-operator-e2e"]


def test_extract_test_names_no_matches():
    cases = [
        {"name": "Build image foo", "time": "1.0", "status": "passed", "failure_message": None},
        {"name": "Clone source code", "time": "2.0", "status": "passed", "failure_message": None},
    ]
    assert extract_test_names(cases) == []


def test_filter_leaf_tests():
    cases = [
        {"name": "TestFoo", "time": "10", "status": "passed", "failure_message": None},
        {"name": "TestFoo/Bar", "time": "5", "status": "passed", "failure_message": None},
        {"name": "TestFoo/Bar/Baz", "time": "2", "status": "passed", "failure_message": None},
        {"name": "TestFoo/Qux", "time": "3", "status": "passed", "failure_message": None},
        {"name": "TestOther", "time": "1", "status": "passed", "failure_message": None},
    ]
    leaves = filter_leaf_tests(cases)
    leaf_names = {c["name"] for c in leaves}
    assert leaf_names == {"TestFoo/Bar/Baz", "TestFoo/Qux", "TestOther"}


def test_filter_leaf_tests_all_leaves():
    cases = [
        {"name": "TestA", "time": "1", "status": "passed", "failure_message": None},
        {"name": "TestB", "time": "2", "status": "passed", "failure_message": None},
    ]
    leaves = filter_leaf_tests(cases)
    assert len(leaves) == 2


def _make_ctx(operator_xml="junit_operator.xml", report_xml="junit_report.xml",
              started_json=None, labels=None):
    ctx = MagicMock()
    ctx.labels = labels or {
        "org": "opendatahub-io",
        "repo": "opendatahub-operator",
        "branch": "main",
        "job_name": "pull-ci-opendatahub-io-opendatahub-operator-main-opendatahub-operator-e2e",
        "pr_number": "3229",
        "pr_sha": "abc123def456",
        "author": "testuser",
        "build_id": "2030989391509327872",
    }

    if started_json is None:
        started_json = '{"timestamp": 1700000000}'

    def fetch_artifact(path):
        if path == "artifacts/junit_operator.xml":
            return _load_fixture(operator_xml) if operator_xml else None
        if path == "started.json":
            return started_json
        if "junit_report.xml" in path:
            return _load_fixture(report_xml) if report_xml else None
        return None

    ctx.fetch_artifact.side_effect = fetch_artifact
    return ctx


def test_process_emits_step_metrics():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator.xml")
    count = pipeline.process(ctx)
    assert count > 0

    metrics_sink.push.assert_called()
    records = metrics_sink.push.call_args[0][0]
    step_metrics = [r for r in records if "ci_junit_step_duration_seconds" in r]
    assert len(step_metrics) == 10
    # Check a specific step
    assert any("Build image opendatahub-operator from the repository" in m for m in step_metrics)


def test_process_emits_test_metrics():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx()
    pipeline.process(ctx)

    metrics_sink.push.assert_called()
    records = metrics_sink.push.call_args[0][0]
    test_metrics = [r for r in records if "ci_junit_test_duration_seconds" in r]
    assert len(test_metrics) == 20
    assert any("TestOdhOperator" in m for m in test_metrics)

    # Parent tests get leaf="false", leaf tests get leaf="true"
    parent_metrics = [m for m in test_metrics if 'leaf="false"' in m]
    leaf_metrics = [m for m in test_metrics if 'leaf="true"' in m]
    assert len(parent_metrics) > 0
    assert len(leaf_metrics) > 0
    # TestOdhOperator (root) should be leaf="false" since it has children
    root_metrics = [m for m in test_metrics if 'test_name="TestOdhOperator"' in m]
    assert all('leaf="false"' in m for m in root_metrics)


def test_process_emits_failure_logs():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator_failure.xml")
    pipeline.process(ctx)

    logs_sink.push.assert_called()
    log_records = logs_sink.push.call_args[0][0]

    # The operator failure should produce a log record
    step_logs = [json.loads(r) for r in log_records if "junit_step" in r]
    assert len(step_logs) == 1
    assert "timed out waiting" in step_logs[0]["_msg"]
    assert step_logs[0]["source"] == "junit_step"
    assert step_logs[0]["status"] == "failed"


def test_process_no_logs_for_passing():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator.xml", report_xml=None)
    pipeline.process(ctx)

    # No failures = no log records
    if logs_sink.push.called:
        log_records = logs_sink.push.call_args[0][0]
        assert len(log_records) == 0


def test_process_missing_junit_operator():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml=None)
    count = pipeline.process(ctx)
    assert count == 0


def test_process_missing_junit_report():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator.xml", report_xml=None)
    count = pipeline.process(ctx)
    # Should still emit step metrics from junit_operator.xml
    assert count > 0
    metrics_sink.push.assert_called()
    records = metrics_sink.push.call_args[0][0]
    step_metrics = [r for r in records if "ci_junit_step_duration_seconds" in r]
    assert len(step_metrics) == 10


def test_labels_merged():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator_failure.xml")
    pipeline.process(ctx)

    # Check metrics have labels
    records = metrics_sink.push.call_args[0][0]
    for r in records:
        assert 'build_id="2030989391509327872"' in r
        assert 'pr_number="3229"' in r

    # Check log records have labels
    log_records = logs_sink.push.call_args[0][0]
    for r in log_records:
        parsed = json.loads(r)
        assert parsed["build_id"] == "2030989391509327872"
        assert parsed["pr_number"] == "3229"


def test_timestamp_from_started_json():
    metrics_sink = MagicMock()
    logs_sink = MagicMock()
    pipeline = JunitPipeline(metrics_sink, logs_sink)

    ctx = _make_ctx(operator_xml="junit_operator.xml", report_xml=None,
                    started_json='{"timestamp": 1700000000}')
    pipeline.process(ctx)

    records = metrics_sink.push.call_args[0][0]
    # All metrics should have the timestamp from started.json
    for r in records:
        assert "1700000000" in r
