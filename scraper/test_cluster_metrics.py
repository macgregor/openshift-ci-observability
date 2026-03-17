"""Pipeline for extracting utilization metrics from test cluster Prometheus TSDBs."""
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from scraper import SHARED_VERSION
from scraper.context import BuildContext
from scraper.gcs import GCSClient
from scraper.metrics import format_prometheus_line, sanitize_metric_name
from scraper.models import Sink

log = logging.getLogger("scraper")

# Prometheus TSDB processing gets its own thread pool to avoid starving the main
# scraper pool.  WAL replay is CPU/IO-intensive and benefits from limited
# concurrency -- 4 workers keeps throughput high without the contention that
# comes from 16 workers all replaying WALs simultaneously.
_PROMTOOL_WORKERS = 4

METRICS = [
    "cluster:cpu_usage_cores:sum",
    "cluster:capacity_cpu_cores:sum",
    "cluster:memory_usage_bytes:sum",
    "cluster:capacity_memory_bytes:sum",
    "instance:node_memory_utilisation:ratio",
    "node_memory_MemTotal_bytes",
    "machine_cpu_cores",
    "kube_node_role",
]

# Metrics that get emitted. kube_node_role is extracted only for the node→role mapping.
_EMITTED_METRICS = [m for m in METRICS if m != "kube_node_role"]

# Map original metric names to output names.
# Colons become underscores via sanitize_metric_name, then prefixed with ci_test_cluster_.
_OUTPUT_NAMES = {name: f"ci_test_cluster_{sanitize_metric_name(name)}" for name in _EMITTED_METRICS}

# Per-node metrics that get enriched with a role label from kube_node_role.
_PER_NODE_METRICS = {
    "instance:node_memory_utilisation:ratio",
    "node_memory_MemTotal_bytes",
    "machine_cpu_cores",
}

# Artifact directories that are never test steps.
_SKIP_DIRS = {"build-logs", "build-resources", "release"}

_PROMETHEUS_TAR_SUFFIX = "gather-extra/artifacts/metrics/prometheus.tar"

_METRICS_CACHE_VERSION_PREFIX = "# version="


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


def _wal_size_mb(tsdb_path):
    """Measure WAL directory size in MB."""
    wal_dir = os.path.join(tsdb_path, "wal")
    if not os.path.isdir(wal_dir):
        return 0
    total = sum(e.stat().st_size for e in os.scandir(wal_dir) if e.is_file())
    return total // (1024 * 1024)


def _promtool_timeout(wal_size_mb):
    """Calculate promtool timeout proportional to WAL size.

    With 4 concurrent workers, observed WAL replay rate is ~10 MB/s.
    The formula gives generous headroom: 200 MB → 70s, 400 MB → 110s, 700 MB → 170s.
    """
    return min(300, 30 + int(wal_size_mb * 0.2))


def _run_promtool(tsdb_path, timeout=120):
    """Run promtool tsdb dump and return stdout lines."""
    match_arg = _build_match_arg()
    try:
        result = subprocess.run(
            ["promtool", "tsdb", "dump", f"--match={match_arg}", tsdb_path],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        log.warning("promtool not found in PATH, skipping test cluster metrics")
        return []
    except subprocess.TimeoutExpired:
        log.warning("promtool timed out after %ds for %s", timeout, tsdb_path)
        return []
    if result.returncode != 0:
        log.warning("promtool exited %d: %s", result.returncode, result.stderr[:500])
        return []
    return result.stdout.splitlines()


def _build_node_role_map(parsed_lines):
    """Build a node hostname → role mapping from kube_node_role entries.

    OCP nodes can have multiple roles (master + control-plane). We normalize
    to just "master" or "worker" since that's the meaningful distinction.
    """
    role_map = {}
    for metric_name, labels, value, ts in parsed_lines:
        if metric_name == "kube_node_role" and value == 1.0:
            node = labels.get("node", "")
            role = labels.get("role", "")
            if not node or not role:
                continue
            # Normalize: "control-plane" and "master" both mean master
            if role in ("master", "control-plane"):
                role = "master"
            # "master" takes precedence over "worker" if a node has both
            if role_map.get(node) == "master":
                continue
            role_map[node] = role
    return role_map


def extract_test_cluster_metrics(lines, job_labels):
    """Convert promtool dump lines to Prometheus text format with job labels.

    Per-node metrics are enriched with a ``role`` label (master/worker) derived
    from ``kube_node_role`` entries in the same TSDB dump.
    """
    # Parse all lines first so we can build the node→role map before emitting.
    parsed = []
    for line in lines:
        result = parse_promtool_line(line)
        if result is not None:
            parsed.append(result)

    role_map = _build_node_role_map(parsed)

    metrics = []
    for metric_name, prom_labels, value, ts in parsed:
        output_name = _OUTPUT_NAMES.get(metric_name)
        if output_name is None:
            continue
        combined_labels = {**job_labels, **prom_labels}
        # Enrich per-node metrics with role from kube_node_role
        if metric_name in _PER_NODE_METRICS and role_map:
            node_key = prom_labels.get("node") or prom_labels.get("instance", "")
            role = role_map.get(node_key, "")
            if role:
                combined_labels["role"] = role
        formatted = format_prometheus_line(output_name, combined_labels, value, ts)
        if formatted:
            metrics.append(formatted)
    return metrics


def _read_cached_metrics(gcs: GCSClient, gcs_path: str, version: str):
    """Read cached .metrics file if it exists and version matches.

    Returns list of metric lines, or None on cache miss/stale.
    """
    content = gcs.read_processed(gcs_path)
    if content is None:
        return None
    first_line, _, rest = content.partition("\n")
    if first_line != f"{_METRICS_CACHE_VERSION_PREFIX}{version}":
        return None
    return [line for line in rest.splitlines() if line]


def _write_cached_metrics(gcs: GCSClient, gcs_path: str, version: str, metric_lines: list[str]):
    """Write .metrics cache file with version header."""
    content = f"{_METRICS_CACHE_VERSION_PREFIX}{version}\n"
    if metric_lines:
        content += "\n".join(metric_lines) + "\n"
    gcs.write_processed(gcs_path, content)


class TestClusterMetricsPipeline:
    name = "test_cluster_metrics"
    version = f"{SHARED_VERSION}.2"
    pushes_own_sentinel = True

    def __init__(self, sink: Sink, gcs: GCSClient,
                 session: requests.Session, vm_url: str):
        self.sink = sink
        self._gcs = gcs
        self._session = session
        self._vm_url = vm_url
        self._pool = ThreadPoolExecutor(
            max_workers=_PROMTOOL_WORKERS, thread_name_prefix="promtool",
        )

    def process(self, ctx: BuildContext) -> int:
        if not self._gcs.has_cache:
            log.warning("TSDB pipeline requires disk cache (GCS_NO_CACHE disables it); skipping")
            return 0

        # Skip builds without a cluster claim -- no test cluster means no Prometheus data.
        # The cluster_pool pipeline runs before this one, so clusterClaim.json is already
        # cached in the artifact cache (None if it doesn't exist).
        if ctx.fetch_artifact("artifacts/build-resources/clusterClaim.json") is None:
            return 0

        steps = discover_test_steps(ctx)
        if not steps:
            return 0

        for step_name in steps:
            self._submit_step(ctx, step_name)
        # Actual count is logged asynchronously when each task completes.
        return 0

    def _submit_step(self, ctx: BuildContext, step_name: str):
        artifact_path = f"artifacts/{step_name}/{_PROMETHEUS_TAR_SUFFIX}"
        gcs_path = ctx.artifact_gcs_path(artifact_path)

        # Fast path: check .metrics cache
        cached = _read_cached_metrics(self._gcs, gcs_path, self.version)
        if cached is not None:
            if cached:
                self.sink.push(cached)
                log.info("PR %s build %s step %s: %d test_cluster_metrics (from cache)",
                         ctx.build.pr, ctx.build.build_id, step_name, len(cached))
            self._push_sentinel(ctx.build.build_id, ctx.labels.get("build_id", ctx.build.build_id))
            return

        # Slow path: ensure tar is on disk
        tar_path = ctx.artifact_cache_path(artifact_path)
        if tar_path is None:
            return

        step_labels = {**ctx.labels, "test_step": step_name}
        self._pool.submit(
            self._process_tar_from_path, tar_path, gcs_path, step_labels,
            ctx.build.build_id, ctx.build.pr, step_name,
        )

    def _process_tar_from_path(self, tar_path: Path, gcs_path: str,
                               step_labels, build_id, pr, step_name):
        tmpdir = tempfile.mkdtemp(prefix="prom-tsdb-")
        try:
            with tarfile.open(str(tar_path)) as tf:
                tf.extractall(tmpdir, filter="data")
            wal_mb = _wal_size_mb(tmpdir)
            timeout = _promtool_timeout(wal_mb)
            lines = _run_promtool(tmpdir, timeout=timeout)
            metrics = extract_test_cluster_metrics(lines, step_labels)
            _write_cached_metrics(self._gcs, gcs_path, self.version, metrics)
            self.sink.push(metrics)
            if metrics:
                log.info("PR %s build %s step %s: %d test_cluster_metrics "
                         "(wal=%dMB, timeout=%ds)",
                         pr, build_id, step_name, len(metrics), wal_mb, timeout)
            self._push_sentinel(build_id, step_labels.get("build_id", build_id))
        except (tarfile.TarError, OSError) as e:
            log.warning("Failed to process prometheus.tar for build %s step %s: %s",
                        build_id, step_name, e)
        except Exception:
            log.error("Prometheus pipeline failed for build %s step %s",
                      build_id, step_name, exc_info=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _push_sentinel(self, build_id, label_build_id):
        """Push per-pipeline sentinel metric from pool worker."""
        from scraper.scraper import push_pipeline_sentinel
        push_pipeline_sentinel(
            self._session, self._vm_url,
            self.name, self.version, label_build_id,
        )

    def drain(self):
        """Wait for all outstanding prometheus processing to complete."""
        self._pool.shutdown(wait=True)
        self._pool = ThreadPoolExecutor(
            max_workers=_PROMTOOL_WORKERS, thread_name_prefix="promtool",
        )
