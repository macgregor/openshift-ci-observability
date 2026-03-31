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
from typing import Optional

from scraper import SHARED_VERSION
from scraper.cache import ArtifactCache
from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line, sanitize_metric_name
from scraper.models import Sink
from scraper.state import ScrapeState

log = logging.getLogger("scraper")

METRICS = [
    "cluster:cpu_usage_cores:sum",
    "cluster:capacity_cpu_cores:sum",
    "cluster:memory_usage_bytes:sum",
    "cluster:capacity_memory_bytes:sum",
    "instance:node_memory_utilisation:ratio",
    "node_memory_MemTotal_bytes",
    "machine_cpu_cores",
    "kube_node_role",
    "kube_pod_container_resource_requests",
    "kube_pod_container_resource_limits",
    "container_memory_working_set_bytes",
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

# Per-pod metrics enriched with role via their node label and kube_node_role.
_PER_POD_METRICS = {
    "kube_pod_container_resource_requests",
    "kube_pod_container_resource_limits",
    "container_memory_working_set_bytes",
}

# Cluster-level metrics that have label_node_role_kubernetes_io from the OCP
# recording rule.  We normalize it to the same "role" label used on per-node
# and per-pod metrics so dashboards can filter consistently.
_CLUSTER_METRICS_WITH_NATIVE_ROLE = {
    "cluster:capacity_cpu_cores:sum",
    "cluster:capacity_memory_bytes:sum",
}

# Artifact directories that are never test steps.
_SKIP_DIRS = {"build-logs", "build-resources", "release"}

# Labels from promtool dump that add cardinality without being queried by dashboards.
_DROP_LABELS = {"boot_id", "machine_id", "system_uuid", "endpoint", "metrics_path", "service", "job"}

_PROMETHEUS_TAR_SUFFIX = "gather-extra/artifacts/metrics/prometheus.tar"

_HEALTH_METRICS_FILE = "cluster-health-metrics.txt"

HEALTH_METRICS = [
    "kube_node_role",
    "kube_node_status_allocatable",
    "kube_node_status_capacity",
    "machine_cpu_cores",
    "node_memory_MemTotal_bytes",
    "node_cpu_usage_cores",
    "kube_node_status_condition",
    "kube_pod_status_phase",
    "kube_pod_container_resource_requests",
    "kube_pod_container_resource_limits",
    "container_memory_working_set_bytes",
    "kube_deployment_status_replicas",
    "kube_deployment_status_replicas_updated",
    "kube_deployment_status_replicas_available",
    "kube_deployment_status_replicas_ready",
    "cluster_healthy",
]

_HEALTH_EMITTED_METRICS = [m for m in HEALTH_METRICS if m != "kube_node_role"]

_HEALTH_OUTPUT_NAMES = {
    name: f"ci_test_cluster_{sanitize_metric_name(name)}"
    for name in _HEALTH_EMITTED_METRICS
}

_HEALTH_PER_NODE_METRICS = {
    "kube_node_status_allocatable",
    "kube_node_status_capacity",
    "machine_cpu_cores",
    "node_memory_MemTotal_bytes",
    "node_cpu_usage_cores",
    "kube_node_status_condition",
}

_HEALTH_PER_POD_METRICS = {
    "kube_pod_status_phase",
    "kube_pod_container_resource_requests",
    "kube_pod_container_resource_limits",
    "container_memory_working_set_bytes",
}

# Output names produced by BOTH prometheus.tar and health metrics.  When health
# data exists for a step we skip these from the tar so the dashboard sees exactly
# one series per metric — the cheaper, more granular health version.
_OVERLAPPING_OUTPUT_NAMES = set(_OUTPUT_NAMES.values()) & set(_HEALTH_OUTPUT_NAMES.values())

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


# Regex to parse a Prometheus text exposition format line:
# metric_name{label1="val1", label2="val2"} value [timestamp_ms]
_EXPO_LINE_RE = re.compile(r'^([\w:]+)(?:\{(.*?)\})?\s+(\S+)(?:\s+(\d+))?$')


def parse_exposition_line(line):
    """Parse a single Prometheus text exposition format line.

    Returns (metric_name, labels_dict, value, timestamp_seconds) or None.
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    m = _EXPO_LINE_RE.match(line)
    if not m:
        return None
    metric_name = m.group(1)
    labels_str = m.group(2)
    value_str = m.group(3)
    ts_ms_str = m.group(4)
    labels = {}
    if labels_str:
        for lm in _LABEL_RE.finditer(labels_str):
            labels[lm.group(1)] = lm.group(2)
    try:
        value = float(value_str)
    except ValueError:
        return None
    ts_seconds = int(ts_ms_str) // 1000 if ts_ms_str else None
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
            # Normalize: "control-plane" and "master" both mean master.
            # Health metrics may emit "control-plane,master" as a single value.
            if "master" in role or "control-plane" in role:
                role = "master"
            # "master" takes precedence over "worker" if a node has both
            if role_map.get(node) == "master":
                continue
            role_map[node] = role
    return role_map


def extract_test_cluster_metrics(lines, job_labels):
    """Convert promtool dump lines to Prometheus text format with job labels.

    Per-node and per-pod metrics are enriched with a ``role`` label
    (master/worker) derived from ``kube_node_role`` entries in the same TSDB
    dump.  For per-pod metrics the ``node`` label is used to look up the role.
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
        combined_labels = {**job_labels, **{k: v for k, v in prom_labels.items() if k not in _DROP_LABELS}}
        combined_labels["metrics_source"] = "tsdb"
        # Enrich per-node and per-pod metrics with role from kube_node_role
        if (metric_name in _PER_NODE_METRICS or metric_name in _PER_POD_METRICS) and role_map:
            node_key = prom_labels.get("node") or prom_labels.get("instance", "")
            role = role_map.get(node_key, "")
            if role:
                combined_labels["role"] = role
        # Normalize native label_node_role_kubernetes_io to "role" on cluster metrics
        if metric_name in _CLUSTER_METRICS_WITH_NATIVE_ROLE:
            native = prom_labels.get("label_node_role_kubernetes_io", "")
            combined_labels["role"] = "master" if native == "master" else "worker"
        formatted = format_prometheus_line(output_name, combined_labels, value, ts)
        if formatted:
            metrics.append(formatted)
    return metrics


def extract_health_metrics(content, job_labels):
    """Convert cluster-health-metrics.txt content to Prometheus lines with job labels.

    Structurally parallel to extract_test_cluster_metrics but parses exposition
    format instead of promtool dump format.
    """
    parsed = []
    for line in content.splitlines():
        result = parse_exposition_line(line)
        if result is not None:
            parsed.append(result)

    role_map = _build_node_role_map(parsed)

    metrics = []
    for metric_name, prom_labels, value, ts in parsed:
        output_name = _HEALTH_OUTPUT_NAMES.get(metric_name)
        if output_name is None:
            continue
        combined = {**job_labels, **{k: v for k, v in prom_labels.items() if k not in _DROP_LABELS}}
        combined["metrics_source"] = "health"
        if (metric_name in _HEALTH_PER_NODE_METRICS or metric_name in _HEALTH_PER_POD_METRICS) and role_map:
            node_key = prom_labels.get("node") or prom_labels.get("instance", "")
            role = role_map.get(node_key, "")
            if role:
                combined["role"] = role
        formatted = format_prometheus_line(output_name, combined, value, ts)
        if formatted:
            metrics.append(formatted)
    return metrics


def _filter_overlapping(metric_lines):
    """Remove metrics whose names overlap with health metrics."""
    return [line for line in metric_lines
            if line.split("{")[0] not in _OVERLAPPING_OUTPUT_NAMES]


def _read_cached_metrics(cache: Optional[ArtifactCache], gcs_path: str, version: str):
    """Read cached .metrics file if it exists and version matches.

    Returns list of metric lines, or None on cache miss/stale.
    """
    if cache is None:
        return None
    content = cache.get_processed(gcs_path, version)
    if content is None:
        return None
    return [line for line in content.splitlines() if line]


def _write_cached_metrics(cache: Optional[ArtifactCache], gcs_path: str, version: str, metric_lines: list[str]):
    """Write .metrics cache file with version header."""
    if cache is None:
        return
    content = "\n".join(metric_lines) + "\n" if metric_lines else ""
    cache.put_processed(gcs_path, version, content)


def _wal_size_from_tar(tar_path: Path) -> int:
    """Measure WAL size in MB by reading tar member headers (no extraction)."""
    try:
        total = 0
        with tarfile.open(str(tar_path)) as tf:
            for member in tf.getmembers():
                # WAL segments can be at wal/00000001 or ./wal/00000001 or
                # some-prefix/wal/00000001 depending on how the tar was created.
                name = member.name
                if not member.isfile():
                    continue
                if name.startswith("wal/") or "/wal/" in name:
                    total += member.size
        return total // (1024 * 1024)
    except (tarfile.TarError, OSError, EOFError):
        return 0


class TestClusterMetricsPipeline:
    name = "test_cluster_metrics"
    version = f"{SHARED_VERSION}.9"
    pushes_own_sentinel = True

    def __init__(self, sink: Sink, gcs, cache: Optional[ArtifactCache],
                 state: Optional[ScrapeState],
                 promtool_workers: int = 1, max_wal_mb: int = 256):
        self.sink = sink
        self._gcs = gcs
        self._cache = cache
        self._state = state
        self._max_wal_mb = max_wal_mb
        self._promtool_workers = promtool_workers
        self._pool = ThreadPoolExecutor(
            max_workers=promtool_workers, thread_name_prefix="promtool",
        )

    def process(self, ctx: BuildContext) -> int:
        if self._cache is None:
            log.warning("TSDB pipeline requires disk cache (GCS_NO_CACHE disables it); skipping")
            self._mark_done(ctx.build.build_id)
            return 0

        # Skip builds without a cluster claim -- no test cluster means no Prometheus data.
        if ctx.fetch_artifact("artifacts/build-resources/clusterClaim.json") is None:
            self._mark_done(ctx.build.build_id)
            return 0

        steps = discover_test_steps(ctx)
        if not steps:
            self._mark_done(ctx.build.build_id)
            return 0

        any_pool_submitted = False
        any_health_pushed = False
        for step_name in steps:
            pool_submitted, health_pushed = self._submit_step(ctx, step_name)
            any_pool_submitted = any_pool_submitted or pool_submitted
            any_health_pushed = any_health_pushed or health_pushed

        # Mark done unless a tar was submitted to the pool (pool worker marks done).
        if not any_pool_submitted:
            self._mark_done(ctx.build.build_id)
        return 0

    def _submit_step(self, ctx: BuildContext, step_name: str):
        """Returns (pool_submitted, health_pushed)."""
        step_labels = {**ctx.labels, "test_step": step_name}

        # --- Health metrics (synchronous, cheap) ---
        health_pushed = False
        health_metrics = None
        try:
            sub_steps = ctx.list_artifact_dirs(f"artifacts/{step_name}/")
            for sub_step in sub_steps:
                if sub_step in _SKIP_DIRS:
                    continue
                health_path = f"artifacts/{step_name}/{sub_step}/artifacts/{_HEALTH_METRICS_FILE}"
                health_content = ctx.fetch_artifact(health_path)
                if health_content is not None:
                    health_metrics = extract_health_metrics(health_content, step_labels)
                    break  # only one sub-step produces health metrics
        except Exception:
            log.warning("Failed to fetch health metrics for build %s step %s",
                        ctx.build.build_id, step_name, exc_info=True)
        if health_metrics:
            self.sink.push(health_metrics)
            health_pushed = True
            log.info("PR %s build %s step %s: %d health_metrics",
                     ctx.build.pr, ctx.build.build_id, step_name, len(health_metrics))

        # --- Prometheus tar ---
        artifact_path = f"artifacts/{step_name}/{_PROMETHEUS_TAR_SUFFIX}"
        gcs_path = ctx.artifact_gcs_path(artifact_path)

        # Fast path: check .metrics cache
        cached = _read_cached_metrics(self._cache, gcs_path, self.version)
        if cached is not None:
            if cached:
                if health_pushed:
                    cached = _filter_overlapping(cached)
                if cached:
                    self.sink.push(cached)
                    log.info("PR %s build %s step %s: %d test_cluster_metrics (from cache)",
                             ctx.build.pr, ctx.build.build_id, step_name, len(cached))
            self._mark_done(ctx.build.build_id)
            return False, health_pushed

        # Slow path: download tar to staging
        tar_path = self._gcs.ensure_staged(gcs_path)
        if tar_path is None:
            return False, health_pushed

        # WAL size gate: skip tars that would OOM the container
        wal_mb = _wal_size_from_tar(tar_path)
        if wal_mb > self._max_wal_mb:
            log.warning("Skipping prometheus.tar for build %s step %s: "
                        "WAL size %dMB exceeds limit %dMB",
                        ctx.build.build_id, step_name, wal_mb, self._max_wal_mb)
            _write_cached_metrics(self._cache, gcs_path, self.version, [])
            self._cache.unstage(tar_path)
            self._mark_done(ctx.build.build_id)
            return False, health_pushed

        self._pool.submit(
            self._process_tar_from_path, tar_path, gcs_path, step_labels,
            ctx.build.build_id, ctx.build.pr, step_name, health_pushed,
        )
        return True, health_pushed

    def _process_tar_from_path(self, tar_path: Path, gcs_path: str,
                               step_labels, build_id, pr, step_name,
                               health_pushed=False):
        # Another scraper sharing the cache volume may have processed this
        # tar between our submission and execution.
        cached = _read_cached_metrics(self._cache, gcs_path, self.version)
        if cached is not None:
            if cached:
                if health_pushed:
                    cached = _filter_overlapping(cached)
                if cached:
                    self.sink.push(cached)
                    log.info("PR %s build %s step %s: %d test_cluster_metrics (from cache)",
                             pr, build_id, step_name, len(cached))
            self._mark_done(build_id)
            self._cache.unstage(tar_path)
            return

        tmpdir = tempfile.mkdtemp(prefix="prom-tsdb-")
        try:
            with tarfile.open(str(tar_path)) as tf:
                tf.extractall(tmpdir, filter="data")
            wal_mb = _wal_size_mb(tmpdir)
            timeout = _promtool_timeout(wal_mb)
            lines = _run_promtool(tmpdir, timeout=timeout)
            metrics = extract_test_cluster_metrics(lines, step_labels)
            to_push = _filter_overlapping(metrics) if health_pushed else metrics
            _write_cached_metrics(self._cache, gcs_path, self.version, to_push)
            self.sink.push(to_push)
            if to_push:
                log.info("PR %s build %s step %s: %d test_cluster_metrics "
                         "(wal=%dMB, timeout=%ds)",
                         pr, build_id, step_name, len(to_push), wal_mb, timeout)
            self._mark_done(build_id)
        except FileNotFoundError:
            # Tar deleted by another scraper or staging wipe.
            cached = _read_cached_metrics(self._cache, gcs_path, self.version)
            if cached is not None:
                if cached:
                    if health_pushed:
                        cached = _filter_overlapping(cached)
                    if cached:
                        self.sink.push(cached)
                        log.info("PR %s build %s step %s: %d test_cluster_metrics (from cache)",
                                 pr, build_id, step_name, len(cached))
                self._mark_done(build_id)
            else:
                log.debug("prometheus.tar deleted before processing for build %s step %s "
                          "(will retry next cycle)", build_id, step_name)
        except (tarfile.TarError, EOFError, OSError) as e:
            log.warning("Corrupt or truncated prometheus.tar for build %s step %s: %s",
                        build_id, step_name, e)
        except Exception:
            log.error("Unexpected error processing prometheus.tar for build %s "
                      "step %s", build_id, step_name, exc_info=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            if self._cache:
                self._cache.unstage(tar_path)

    def _mark_done(self, build_id):
        """Mark this pipeline as done for a build."""
        if self._state:
            self._state.mark_done(build_id, self.name, self.version)

    def drain(self):
        """Wait for all outstanding prometheus processing to complete."""
        self._pool.shutdown(wait=True)
        self._pool = ThreadPoolExecutor(
            max_workers=self._promtool_workers, thread_name_prefix="promtool",
        )
