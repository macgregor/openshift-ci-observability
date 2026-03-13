import json
from unittest.mock import MagicMock

from scraper.logs import LogPipeline


def test_parse_valid_json_lines(ci_operator_log, sample_job_labels):
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = ci_operator_log
    ctx.labels = sample_job_labels

    count = pipeline.process(ctx)
    assert count > 0
    sink.push.assert_called_once()
    records = sink.push.call_args[0][0]
    for r in records:
        parsed = json.loads(r)
        assert "_time" in parsed
        assert "_msg" in parsed
        assert "source" in parsed
        assert parsed["source"] == "ci-operator"


def test_skip_non_json_lines():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    content = 'not json at all\n{"time":"2024-01-01T00:00:00Z","msg":"valid","level":"info"}\nmore garbage\n'
    ctx = MagicMock()
    ctx.fetch_artifact.return_value = content
    ctx.labels = {"build_id": "test"}

    count = pipeline.process(ctx)
    assert count == 1


def test_skip_empty_lines():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    content = '\n\n{"time":"2024-01-01T00:00:00Z","msg":"valid","level":"info"}\n\n'
    ctx = MagicMock()
    ctx.fetch_artifact.return_value = content
    ctx.labels = {"build_id": "test"}

    count = pipeline.process(ctx)
    assert count == 1


def test_label_collision_guard():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    content = '{"time":"2024-01-01T00:00:00Z","msg":"test","repo":"wrong-value","level":"info"}\n'
    ctx = MagicMock()
    ctx.fetch_artifact.return_value = content
    ctx.labels = {"repo": "correct-value", "build_id": "123"}

    pipeline.process(ctx)
    records = sink.push.call_args[0][0]
    parsed = json.loads(records[0])
    assert parsed["repo"] == "correct-value"


def test_missing_time_field():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    content = '{"msg":"no time field","level":"info"}\n'
    ctx = MagicMock()
    ctx.fetch_artifact.return_value = content
    ctx.labels = {"build_id": "test"}

    pipeline.process(ctx)
    records = sink.push.call_args[0][0]
    parsed = json.loads(records[0])
    assert parsed["_time"] == ""


def test_missing_msg_field():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    content = '{"time":"2024-01-01T00:00:00Z","level":"info"}\n'
    ctx = MagicMock()
    ctx.fetch_artifact.return_value = content
    ctx.labels = {"build_id": "test"}

    pipeline.process(ctx)
    records = sink.push.call_args[0][0]
    parsed = json.loads(records[0])
    assert parsed["_msg"] == ""


def test_process_integration(ci_operator_log, sample_job_labels):
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = ci_operator_log
    ctx.labels = sample_job_labels

    count = pipeline.process(ctx)
    assert count > 0
    sink.push.assert_called_once()
    records = sink.push.call_args[0][0]
    assert len(records) == count

    # Verify labels are merged into each record
    for r in records:
        parsed = json.loads(r)
        assert parsed["build_id"] == sample_job_labels["build_id"]
        assert parsed["org"] == sample_job_labels["org"]


def test_process_missing_artifact():
    sink = MagicMock()
    pipeline = LogPipeline(sink)

    ctx = MagicMock()
    ctx.fetch_artifact.return_value = None

    count = pipeline.process(ctx)
    assert count == 0
    sink.push.assert_not_called()
