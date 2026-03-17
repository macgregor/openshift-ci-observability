"""Step graph pipeline for extracting config hash metrics and step-level logs."""
import json
import logging

from scraper import SHARED_VERSION
from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line
from scraper.models import Sink

log = logging.getLogger("scraper")


class StepGraphPipeline:
    name = "step_graph"
    version = f"{SHARED_VERSION}.1"
    _pushes_logs = True

    def __init__(self, metrics_sink: Sink, logs_sink: Sink):
        self.metrics_sink = metrics_sink
        self.logs_sink = logs_sink

    def process(self, ctx: BuildContext) -> int:
        content = ctx.fetch_artifact("artifacts/ci-operator-step-graph.json")
        if content is None:
            return 0

        try:
            steps = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("Invalid JSON in step graph for build %s", ctx.build.build_id)
            return 0

        labels = ctx.labels
        config_hash = labels.get("config_hash", "")
        if not config_hash:
            return 0

        # Push ci_config_hash metric
        metric_line = format_prometheus_line("ci_config_hash", labels, 1, None)
        if metric_line:
            self.metrics_sink.push([metric_line])

        # Push one log entry per step
        log_records = []
        for step in steps:
            duration = step.get("duration", "")
            duration_s = None
            if isinstance(duration, (int, float)):
                duration_s = round(duration / 1e9, 3)

            record = {
                **labels,
                "_time": step.get("started_at", ""),
                "_msg": step.get("description", ""),
                "source": "step_graph",
                "pipeline": "step_graph",
                "step_name": step.get("name", ""),
                "dependencies": json.dumps(step.get("dependencies", []),
                                           separators=(",", ":")),
                "failed": step.get("failed", False),
                "config_hash": config_hash,
            }
            if duration_s is not None:
                record["duration_seconds"] = duration_s
            log_records.append(json.dumps(record))

        self.logs_sink.push(log_records)
        return len(log_records) + 1  # logs + 1 metric
