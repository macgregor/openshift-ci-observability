"""Pipeline for extracting utilization metrics from test cluster Prometheus TSDBs."""
import logging
import re
import subprocess
import tarfile
import tempfile
from io import BytesIO

from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line, sanitize_metric_name
from scraper.models import Sink

log = logging.getLogger("scraper")

METRICS = [
    "cluster:cpu_usage_cores:sum",
    "cluster:capacity_cpu_cores:sum",
    "cluster:memory_usage_bytes:sum",
    "cluster:capacity_memory_bytes:sum",
    "instance:node_memory_utilisation:ratio",
    "node_memory_MemTotal_bytes",
    "machine_cpu_cores",
]

# Map original metric names to output names.
# Colons become underscores via sanitize_metric_name, then prefixed with ci_test_cluster_.
_OUTPUT_NAMES = {name: f"ci_test_cluster_{sanitize_metric_name(name)}" for name in METRICS}

# Artifact directories that are never test steps.
_SKIP_DIRS = {"build-logs", "build-resources", "release"}

_PROMETHEUS_TAR_SUFFIX = "gather-extra/artifacts/metrics/prometheus.tar"


def discover_test_steps(ctx: BuildContext) -> list[str]:
    """List artifact directories that could contain test cluster data."""
    return [s for s in ctx.list_artifact_dirs("artifacts/") if s not in _SKIP_DIRS]


# Regex to parse a promtool tsdb dump line:
# {label1="val1", label2="val2"} value timestamp_ms
_DUMP_LINE_RE = re.compile(r'^\{(.+?)\}\s+(\S+)\s+(\d+)$')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_promtool_line(line):
    """Parse a single promtool tsdb dump output line.

    Returns (metric_name, labels_dict, value, timestamp_seconds) or None.
    """
    m = _DUMP_LINE_RE.match(line)
    if not m:
        return None
    labels_str, value_str, ts_ms_str = m.group(1), m.group(2), m.group(3)
    labels = {}
    metric_name = None
    for lm in _LABEL_RE.finditer(labels_str):
        k, v = lm.group(1), lm.group(2)
        if k == "__name__":
            metric_name = v
        else:
            labels[k] = v
    if metric_name is None:
        return None
    try:
        value = float(value_str)
    except ValueError:
        return None
    # promtool outputs millisecond timestamps; Prometheus text format uses seconds
    ts_seconds = int(ts_ms_str) // 1000
    return metric_name, labels, value, ts_seconds


def _build_match_arg():
    """Build the --match argument for promtool tsdb dump."""
    names = "|".join(re.escape(m) for m in METRICS)
    return f'{{__name__=~"{names}"}}'


def _run_promtool(tsdb_path):
    """Run promtool tsdb dump and return stdout lines."""
    match_arg = _build_match_arg()
    try:
        result = subprocess.run(
            ["promtool", "tsdb", "dump", f"--match={match_arg}", tsdb_path],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        log.warning("promtool not found in PATH, skipping test cluster metrics")
        return []
    except subprocess.TimeoutExpired:
        log.warning("promtool timed out after 120s for %s", tsdb_path)
        return []
    if result.returncode != 0:
        log.warning("promtool exited %d: %s", result.returncode, result.stderr[:500])
        return []
    return result.stdout.splitlines()


def extract_test_cluster_metrics(lines, job_labels):
    """Convert promtool dump lines to Prometheus text format with job labels."""
    metrics = []
    for line in lines:
        parsed = parse_promtool_line(line)
        if parsed is None:
            continue
        metric_name, prom_labels, value, ts = parsed
        output_name = _OUTPUT_NAMES.get(metric_name)
        if output_name is None:
            continue
        combined_labels = {**job_labels, **prom_labels}
        formatted = format_prometheus_line(output_name, combined_labels, value, ts)
        if formatted:
            metrics.append(formatted)
    return metrics


class TestClusterMetricsPipeline:
    name = "test_cluster_metrics"

    def __init__(self, sink: Sink):
        self.sink = sink

    def process(self, ctx: BuildContext) -> int:
        # Skip builds without a cluster claim -- no test cluster means no Prometheus data.
        # The cluster_pool pipeline runs before this one, so clusterClaim.json is already
        # cached in the artifact cache (None if it doesn't exist).
        if ctx.fetch_artifact("artifacts/build-resources/clusterClaim.json") is None:
            return 0

        steps = discover_test_steps(ctx)
        if not steps:
            return 0

        total = 0
        for step_name in steps:
            count = self._process_step(ctx, step_name)
            total += count
        return total

    def _process_step(self, ctx: BuildContext, step_name: str) -> int:
        artifact_path = f"artifacts/{step_name}/{_PROMETHEUS_TAR_SUFFIX}"
        tar_bytes = ctx.fetch_artifact_binary(artifact_path)
        if tar_bytes is None:
            return 0

        step_labels = {**ctx.labels, "test_step": step_name}
        try:
            with tempfile.TemporaryDirectory(prefix="prom-tsdb-") as tmpdir:
                with tarfile.open(fileobj=BytesIO(tar_bytes)) as tf:
                    tf.extractall(tmpdir, filter="data")
                lines = _run_promtool(tmpdir)
                metrics = extract_test_cluster_metrics(lines, step_labels)
                self.sink.push(metrics)
                return len(metrics)
        except (tarfile.TarError, OSError) as e:
            log.warning("Failed to extract prometheus.tar for build %s step %s: %s",
                        ctx.build.build_id, step_name, e)
            return 0
