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
# scraper pool.  WAL replay is CPU/IO-intensive and uses ~3-5x the WAL size in
# memory (e.g. a 400 MB WAL can use ~1.5 GB RAM).  With the scraper container's
# 4 GB memory limit, 2 concurrent replays is the safe maximum.
_PROMTOOL_WORKERS = 2

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

# Artifact directories that are never test steps.
_SKIP_DIRS = {"build-logs", "build-resources", "release"}

# Labels from promtool dump that add cardinality without being queried by dashboards.
_DROP_LABELS = {"boot_id", "machine_id", "system_uuid", "endpoint", "metrics_path", "service", "job"}

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
        # Enrich per-node and per-pod metrics with role from kube_node_role
        if (metric_name in _PER_NODE_METRICS or metric_name in _PER_POD_METRICS) and role_map:
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


def _delete_cached_tar(gcs: GCSClient, gcs_path: str):
    """Delete the raw prometheus.tar from the cache after .metrics extraction.

    The .metrics file retains the processed output, so the large tar is no
    longer needed.  Silently ignores missing files (already deleted or never
    cached).
    """
    cp = gcs._cache_path(gcs_path)
    if cp is not None and cp.exists():
        try:
            cp.unlink()
        except OSError:
            pass


def _write_cached_metrics(gcs: GCSClient, gcs_path: str, version: str, metric_lines: list[str]):
    """Write .metrics cache file with version header."""
    content = f"{_METRICS_CACHE_VERSION_PREFIX}{version}\n"
    if metric_lines:
        content += "\n".join(metric_lines) + "\n"
    gcs.write_processed(gcs_path, content)


class TestClusterMetricsPipeline:
    name = "test_cluster_metrics"
    version = f"{SHARED_VERSION}.5"
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
        self._cleanup_stale_tars()

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
            self._push_sentinel(ctx.build.build_id, ctx.labels.get("build_id", ctx.build.build_id),
                                repo=ctx.labels.get("repo", ""))
            # Clean up raw tar if it still exists (pre-deletion builds)
            _delete_cached_tar(self._gcs, gcs_path)
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
            _delete_cached_tar(self._gcs, gcs_path)
            self.sink.push(metrics)
            if metrics:
                log.info("PR %s build %s step %s: %d test_cluster_metrics "
                         "(wal=%dMB, timeout=%ds)",
                         pr, build_id, step_name, len(metrics), wal_mb, timeout)
            self._push_sentinel(build_id, step_labels.get("build_id", build_id),
                                repo=step_labels.get("repo", ""))
        except (tarfile.TarError, OSError) as e:
            log.warning("Failed to process prometheus.tar for build %s step %s: %s",
                        build_id, step_name, e)
        except Exception:
            log.error("Prometheus pipeline failed for build %s step %s",
                      build_id, step_name, exc_info=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _push_sentinel(self, build_id, label_build_id, repo=""):
        """Push per-pipeline sentinel metric from pool worker."""
        from scraper.scraper import push_pipeline_sentinel
        push_pipeline_sentinel(
            self._session, self._vm_url,
            self.name, self.version, label_build_id, repo=repo,
        )

    def _cleanup_stale_tars(self):
        """Delete prometheus.tar files that already have a .metrics sibling.

        Normally the tar is deleted right after .metrics is written, but OOM kills
        or crashes can leave orphans.  Any tar with a .metrics sibling is safe to
        remove — if the build needs reprocessing (version change), the tar will be
        re-downloaded from GCS.  Builds that aged out of the discovery window will
        never be reprocessed, so their tars are pure dead weight.
        """
        cache_dir = getattr(self._gcs, '_cache_dir', None)
        if cache_dir is None:
            return
        deleted = 0
        for dirpath, _dirnames, filenames in os.walk(cache_dir):
            if "prometheus.tar" not in filenames:
                continue
            if "prometheus.tar.metrics" not in filenames:
                continue
            try:
                (Path(dirpath) / "prometheus.tar").unlink()
                deleted += 1
            except OSError:
                pass
        if deleted:
            log.info("Cleaned up %d stale prometheus.tar files from cache", deleted)

    def drain(self):
        """Wait for all outstanding prometheus processing to complete."""
        self._pool.shutdown(wait=True)
        self._pool = ThreadPoolExecutor(
            max_workers=_PROMTOOL_WORKERS, thread_name_prefix="promtool",
        )
