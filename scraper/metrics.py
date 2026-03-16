"""Metrics pipeline for extracting and converting Prometheus metrics."""
import json
import logging
import re
from datetime import datetime

from scraper.models import Sink
from scraper.context import BuildContext

log = logging.getLogger("scraper")


def flatten_numeric_fields(obj, prefix=""):
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        full_key = f"{prefix}_{key}" if prefix else key
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            yield (full_key, value)
        elif isinstance(value, dict):
            yield from flatten_numeric_fields(value, full_key)


def extract_string_fields(entry):
    labels = {}
    skip_keys = {"additional_context", "message", "locator", "condition_transition_times",
                 "labels", "resources", "usage_stats", "watch_history", "workloads"}
    for key, value in entry.items():
        if key in skip_keys:
            continue
        if isinstance(value, bool):
            labels[key] = str(value).lower()
        elif isinstance(value, str) and len(value) <= 128:
            labels[key] = value
    return labels


def parse_timestamp_best_effort(entry):
    for field in ("timestamp", "completion_time", "start_time", "from"):
        val = entry.get(field)
        if not val:
            continue
        try:
            val = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(val)
            return int(dt.timestamp())
        except (ValueError, AttributeError):
            continue
    return None


def sanitize_metric_name(name):
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name)
    return name.strip('_').lower()


def escape_label_value(v):
    return v.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def format_prometheus_line(metric_name, labels, value, timestamp):
    metric_name = sanitize_metric_name(metric_name)
    if not metric_name:
        return None
    label_parts = []
    for k, v in sorted(labels.items()):
        k = sanitize_metric_name(k)
        if k and v:
            label_parts.append(f'{k}="{escape_label_value(str(v))}"')
    labels_str = "{" + ",".join(label_parts) + "}" if label_parts else ""
    ts_str = f" {timestamp}" if timestamp else ""
    return f"{metric_name}{labels_str} {value}{ts_str}"


def parse_k8s_quantity(val):
    """Parse K8s resource quantity string to base unit (bytes for memory, cores for cpu)."""
    if not isinstance(val, str):
        return None
    suffixes = {
        'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
        'k': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4,
        'm': 0.001,
    }
    for suffix, multiplier in sorted(suffixes.items(), key=lambda x: -len(x[0])):
        if val.endswith(suffix):
            try:
                return float(val[:-len(suffix)]) * multiplier
            except ValueError:
                return None
    try:
        return float(val)
    except ValueError:
        return None


def apply_known_transforms(section, key, value):
    try:
        if section == "pods" and key.endswith("_latency"):
            return value / 1e9
    except Exception:
        pass
    return value


# Resource fields to extract with (suffix, unit_conversion_fn) pairs.
# CPU is converted to millicores for consistency with usage_stats_*_cpu_milli.
# Memory and ephemeral-storage are in bytes (parse_k8s_quantity handles Ki/Mi/Gi).
# Pods are dimensionless.
_RESOURCE_FIELDS = {
    "cpu": ("cpu_milli", lambda v: v * 1000),
    "memory": ("memory_bytes", lambda v: v),
    "ephemeral-storage": ("ephemeral_storage_bytes", lambda v: v),
    "pods": ("pods", lambda v: v),
}


def flatten_resource_fields(resources):
    """Extract capacity/allocatable K8s quantities as numeric metrics.

    Yields (metric_key_suffix, value) tuples like
    ("resources_capacity_cpu_milli", 16000.0).
    """
    if not isinstance(resources, dict):
        return
    for category in ("capacity", "allocatable"):
        cat_dict = resources.get(category)
        if not isinstance(cat_dict, dict):
            continue
        for field, (suffix, convert) in _RESOURCE_FIELDS.items():
            raw = cat_dict.get(field)
            if raw is None:
                continue
            parsed = parse_k8s_quantity(raw)
            if parsed is not None:
                yield (f"resources_{category}_{suffix}", convert(parsed))


CANONICAL_ALIASES = {
    "ci_pods_scheduling_latency": "ci_pod_scheduling_latency_seconds",
    "ci_openshift_builds_duration_seconds": "ci_build_duration_seconds",
    "ci_events_message_annotations_duration_seconds": "ci_step_duration_seconds",
}


def extract_metrics_from_entry(section, entry, job_labels):
    metrics = []
    timestamp = parse_timestamp_best_effort(entry)
    entry_labels = {**job_labels, **extract_string_fields(entry)}
    ctx = entry.get("additional_context")
    if isinstance(ctx, dict):
        entry_labels.update(extract_string_fields(ctx))

    numeric_fields = list(flatten_numeric_fields(entry))
    if section == "nodes":
        numeric_fields.extend(flatten_resource_fields(entry.get("resources")))

    for key, value in numeric_fields:
        value = apply_known_transforms(section, key, value)
        metric_name = f"ci_{section}_{key}"
        line = format_prometheus_line(metric_name, entry_labels, value, timestamp)
        if line:
            metrics.append(line)
            canonical = CANONICAL_ALIASES.get(sanitize_metric_name(metric_name))
            if canonical:
                alias_line = format_prometheus_line(canonical, entry_labels, value, timestamp)
                if alias_line:
                    metrics.append(alias_line)
    return metrics


SECTIONS = ["events", "pods", "nodes", "openshift_builds", "images", "leases", "test_platform_insights"]


def _parse_iso_seconds(val):
    """Parse ISO timestamp string to Unix seconds."""
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _extract_step_offsets(events, job_labels):
    """Emit step offset metrics relative to pipeline start."""
    metrics = []
    start_times = []
    for event in events:
        t = _parse_iso_seconds(event.get("from"))
        if t is not None:
            start_times.append(t)
    if not start_times:
        return metrics
    pipeline_start = min(start_times)
    pipeline_ts = int(pipeline_start)
    for event in events:
        ev_from = _parse_iso_seconds(event.get("from"))
        ev_to = _parse_iso_seconds(event.get("to"))
        if ev_from is None or ev_to is None:
            continue
        entry_labels = {**job_labels, **extract_string_fields(event)}
        start_offset = ev_from - pipeline_start
        end_offset = ev_to - pipeline_start
        for name, value in [("ci_step_relative_start_seconds", start_offset),
                            ("ci_step_relative_end_seconds", end_offset)]:
            line = format_prometheus_line(name, entry_labels, round(value, 3), pipeline_ts)
            if line:
                metrics.append(line)
    return metrics


def convert_to_metrics(data, job_labels):
    all_metrics = []
    for section in SECTIONS:
        entries = data.get(section, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            try:
                all_metrics.extend(extract_metrics_from_entry(section, entry, job_labels))
            except Exception:
                log.error("Failed to extract metrics from %s entry", section, exc_info=True)
    events = data.get("events", [])
    if isinstance(events, list):
        try:
            all_metrics.extend(_extract_step_offsets(events, job_labels))
        except Exception:
            log.error("Failed to extract step offsets", exc_info=True)
    return all_metrics


class MetricsPipeline:
    name = "metrics"

    def __init__(self, sink: Sink):
        self.sink = sink

    def process(self, ctx: BuildContext) -> int:
        content = ctx.fetch_artifact("artifacts/ci-operator-metrics.json")
        if content is None:
            return 0
        data = json.loads(content)
        metrics = convert_to_metrics(data, ctx.labels)
        self.sink.push(metrics)
        return len(metrics)
