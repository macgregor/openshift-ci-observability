import json
from unittest.mock import MagicMock

from scraper.step_graph import StepGraphPipeline
from scraper.context import BuildContext


SAMPLE_STEP_GRAPH = json.dumps([
    {
        "name": "ipi-install",
        "description": "Install OpenShift via IPI",
        "dependencies": ["lease"],
        "started_at": "2026-03-10T10:00:00Z",
        "finished_at": "2026-03-10T10:30:00Z",
        "duration": 1800000000000,
        "failed": False,
        "manifests": ["some-manifest"],
    },
    {
        "name": "e2e",
        "description": "Run end-to-end tests",
        "dependencies": ["ipi-install"],
        "started_at": "2026-03-10T10:30:00Z",
        "finished_at": "2026-03-10T11:00:00Z",
        "duration": 1800000000000,
        "failed": True,
        "manifests": [],
    },
])

SAMPLE_LABELS = {
    "org": "opendatahub-io",
    "repo": "opendatahub-operator",
    "branch": "main",
    "job_name": "test-job",
    "pr_number": "100",
    "pr_sha": "abc123",
    "author": "dev",
    "build_id": "12345",
    "config_hash": "aabbccddee11",
}


def test_process_emits_metric_and_logs():
    vm_sink = MagicMock()
    vl_sink = MagicMock()
    pipeline = StepGraphPipeline(vm_sink, vl_sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = SAMPLE_STEP_GRAPH
    ctx.labels = SAMPLE_LABELS
    ctx.build.build_id = "12345"

    count = pipeline.process(ctx)

    # 2 log entries + 1 metric = 3
    assert count == 3

    # Metric pushed
    vm_sink.push.assert_called_once()
    metric_lines = vm_sink.push.call_args[0][0]
    assert len(metric_lines) == 1
    assert metric_lines[0].startswith("ci_config_hash{")
    assert 'config_hash="aabbccddee11"' in metric_lines[0]

    # Log entries pushed
    vl_sink.push.assert_called_once()
    log_records = vl_sink.push.call_args[0][0]
    assert len(log_records) == 2

    step1 = json.loads(log_records[0])
    assert step1["source"] == "step_graph"
    assert step1["step_name"] == "ipi-install"
    assert step1["config_hash"] == "aabbccddee11"
    assert step1["failed"] is False
    assert step1["duration_seconds"] == 1800.0
    assert step1["build_id"] == "12345"

    step2 = json.loads(log_records[1])
    assert step2["step_name"] == "e2e"
    assert step2["failed"] is True


def test_process_missing_artifact():
    vm_sink = MagicMock()
    vl_sink = MagicMock()
    pipeline = StepGraphPipeline(vm_sink, vl_sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = None

    count = pipeline.process(ctx)
    assert count == 0
    vm_sink.push.assert_not_called()
    vl_sink.push.assert_not_called()


def test_process_invalid_json():
    vm_sink = MagicMock()
    vl_sink = MagicMock()
    pipeline = StepGraphPipeline(vm_sink, vl_sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = "not valid json"
    ctx.build.build_id = "12345"

    count = pipeline.process(ctx)
    assert count == 0
    vm_sink.push.assert_not_called()
    vl_sink.push.assert_not_called()


def test_process_empty_config_hash():
    vm_sink = MagicMock()
    vl_sink = MagicMock()
    pipeline = StepGraphPipeline(vm_sink, vl_sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = SAMPLE_STEP_GRAPH
    ctx.labels = {**SAMPLE_LABELS, "config_hash": ""}

    count = pipeline.process(ctx)
    assert count == 0


def test_config_hash_computation():
    """Test that _compute_config_hash produces a stable 12-char hex hash."""
    build = MagicMock()
    build.build_id = "123"
    build.base_path = "pr-logs/pull/org_repo"
    build.pr = "1"
    build.job = "test-job"

    gcs = MagicMock()

    ctx = BuildContext(build, gcs)

    step_graph = json.dumps([
        {"name": "b-step", "description": "Second", "dependencies": ["a-step"],
         "started_at": "2026-01-01T00:00:00Z", "duration": 100, "failed": False},
        {"name": "a-step", "description": "First", "dependencies": [],
         "started_at": "2026-01-01T00:00:00Z", "duration": 200, "failed": True},
    ])

    # Mock fetch_artifact to return step graph only for the step graph path
    def mock_fetch(path):
        if path == "artifacts/ci-operator-step-graph.json":
            return step_graph
        return None
    gcs.fetch_object.side_effect = lambda p: mock_fetch(p.split(f"{build.build_id}/")[-1])

    result = ctx._compute_config_hash()
    assert len(result) == 12
    assert all(c in "0123456789abcdef" for c in result)

    # Same input produces same hash
    gcs.fetch_object.side_effect = lambda p: mock_fetch(p.split(f"{build.build_id}/")[-1])
    ctx2 = BuildContext(build, gcs)
    assert ctx2._compute_config_hash() == result


def test_config_hash_order_independent():
    """Hash should be the same regardless of step order in the JSON."""
    build = MagicMock()
    build.build_id = "123"
    build.base_path = "pr-logs/pull/org_repo"
    build.pr = "1"
    build.job = "test-job"

    gcs = MagicMock()

    steps_order1 = [
        {"name": "a", "description": "A", "dependencies": []},
        {"name": "b", "description": "B", "dependencies": ["a"]},
    ]
    steps_order2 = [
        {"name": "b", "description": "B", "dependencies": ["a"]},
        {"name": "a", "description": "A", "dependencies": []},
    ]

    gcs.fetch_object.return_value = json.dumps(steps_order1)
    ctx1 = BuildContext(build, gcs)
    hash1 = ctx1._compute_config_hash()

    gcs.fetch_object.return_value = json.dumps(steps_order2)
    ctx2 = BuildContext(build, gcs)
    hash2 = ctx2._compute_config_hash()

    assert hash1 == hash2


def test_config_hash_ignores_runtime_fields():
    """Hash should ignore runtime fields like started_at, duration, failed."""
    build = MagicMock()
    build.build_id = "123"
    build.base_path = "pr-logs/pull/org_repo"
    build.pr = "1"
    build.job = "test-job"

    gcs = MagicMock()

    steps_run1 = [
        {"name": "a", "description": "A", "dependencies": [],
         "started_at": "2026-01-01T00:00:00Z", "duration": 100, "failed": False},
    ]
    steps_run2 = [
        {"name": "a", "description": "A", "dependencies": [],
         "started_at": "2026-02-01T00:00:00Z", "duration": 999, "failed": True},
    ]

    gcs.fetch_object.return_value = json.dumps(steps_run1)
    ctx1 = BuildContext(build, gcs)
    hash1 = ctx1._compute_config_hash()

    gcs.fetch_object.return_value = json.dumps(steps_run2)
    ctx2 = BuildContext(build, gcs)
    hash2 = ctx2._compute_config_hash()

    assert hash1 == hash2


def test_config_hash_missing_artifact():
    build = MagicMock()
    build.build_id = "123"
    build.base_path = "pr-logs/pull/org_repo"
    build.pr = "1"
    build.job = "test-job"

    gcs = MagicMock()
    gcs.fetch_object.return_value = None

    ctx = BuildContext(build, gcs)
    assert ctx._compute_config_hash() == ""
