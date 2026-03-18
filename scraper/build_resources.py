"""Build resources pipeline for ingesting K8s events, pods, and deployments."""
import json
import logging
import re
from datetime import datetime

from scraper import SHARED_VERSION
from scraper.context import BuildContext
from scraper.junit import parse_junit_xml, extract_test_names
from scraper.models import Sink

log = logging.getLogger("scraper")


def _parse_iso_ts(val):
    """Parse ISO timestamp string to Unix epoch seconds, or None."""
    if not isinstance(val, str) or not val:
        return None
    try:
        return int(datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _extract_events(data, scope, job_labels):
    """Extract log records from a K8s EventList JSON."""
    records = []
    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        obj = item.get("involvedObject", {})
        ts = (_parse_iso_ts(item.get("lastTimestamp"))
              or _parse_iso_ts(item.get("firstTimestamp"))
              or _parse_iso_ts(item.get("metadata", {}).get("creationTimestamp")))
        if ts is None:
            continue

        record = {
            **job_labels,
            "_time": ts,
            "_msg": item.get("message", ""),
            "source": "k8s_event",
            "pipeline": "build_resources",
            "scope": scope,
            "reason": item.get("reason", ""),
            "type": item.get("type", ""),
            "event_count": item.get("count", 1),
            "object_kind": obj.get("kind", ""),
            "object_name": obj.get("name", ""),
            "object_namespace": obj.get("namespace", ""),
            "source_component": item.get("source", {}).get("component", ""),
        }
        records.append(json.dumps(record))
    return records


def _container_summary(container_statuses):
    """Build a compact summary of non-ready containers."""
    problems = []
    for cs in (container_statuses or []):
        if cs.get("ready"):
            continue
        name = cs.get("name", "?")
        restarts = cs.get("restartCount", 0)
        state = cs.get("state", {})
        if "waiting" in state:
            reason = state["waiting"].get("reason", "Waiting")
            problems.append(f"{name}: {reason} (restarts={restarts})")
        elif "terminated" in state:
            reason = state["terminated"].get("reason", "Terminated")
            exit_code = state["terminated"].get("exitCode", "?")
            problems.append(f"{name}: {reason} exit={exit_code} (restarts={restarts})")
        elif restarts > 0:
            problems.append(f"{name}: restarts={restarts}")
    return "; ".join(problems)


def _extract_pods(data, scope, job_labels):
    """Extract log records from a K8s PodList JSON."""
    records = []
    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        meta = item.get("metadata", {})
        status = item.get("status", {})
        phase = status.get("phase", "Unknown")
        ts = _parse_iso_ts(meta.get("creationTimestamp"))
        if ts is None:
            continue

        container_statuses = status.get("containerStatuses", [])
        init_statuses = status.get("initContainerStatuses", [])
        total_restarts = sum(c.get("restartCount", 0) for c in container_statuses)
        problems = _container_summary(container_statuses + init_statuses)
        msg = f"phase={phase}" + (f" | {problems}" if problems else "")

        record = {
            **job_labels,
            "_time": ts,
            "_msg": msg,
            "source": "k8s_pod",
            "pipeline": "build_resources",
            "scope": scope,
            "pod_name": meta.get("name", ""),
            "pod_namespace": meta.get("namespace", ""),
            "phase": phase,
            "restart_count": total_restarts,
        }
        records.append(json.dumps(record))
    return records


def _extract_deployments(data, scope, job_labels):
    """Extract log records from a K8s DeploymentList JSON."""
    records = []
    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        ts = _parse_iso_ts(meta.get("creationTimestamp"))
        if ts is None:
            continue

        replicas = spec.get("replicas", 0)
        ready = status.get("readyReplicas", 0)
        available = status.get("availableReplicas", 0)
        unavailable = status.get("unavailableReplicas", 0)

        conditions = status.get("conditions", [])
        cond_summary = "; ".join(
            f"{c['type']}={c['status']}" + (f" ({c.get('reason', '')})" if c.get("reason") else "")
            for c in conditions
        )
        msg = f"{ready}/{replicas} ready" + (f" | {cond_summary}" if cond_summary else "")

        record = {
            **job_labels,
            "_time": ts,
            "_msg": msg,
            "source": "k8s_deployment",
            "pipeline": "build_resources",
            "scope": scope,
            "deployment_name": meta.get("name", ""),
            "deployment_namespace": meta.get("namespace", ""),
            "replicas": replicas,
            "ready_replicas": ready,
            "available_replicas": available,
            "unavailable_replicas": unavailable,
        }
        records.append(json.dumps(record))
    return records


def _fetch_and_parse(ctx, path):
    """Fetch a JSON artifact and parse it, returning None on failure."""
    content = ctx.fetch_artifact(path)
    if content is None:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        log.warning("Invalid JSON in %s for build %s", path, ctx.build.build_id)
        return None


def _discover_test_names(ctx):
    """Discover test names from junit_operator.xml (same logic as JunitPipeline)."""
    content = ctx.fetch_artifact("artifacts/junit_operator.xml")
    if content is None:
        return []
    try:
        _, cases = parse_junit_xml(content)
        return extract_test_names(cases)
    except Exception:
        return []


class BuildResourcesPipeline:
    name = "build_resources"
    version = f"{SHARED_VERSION}.1"
    _pushes_logs = True

    def __init__(self, logs_sink: Sink):
        self.logs_sink = logs_sink

    def process(self, ctx: BuildContext) -> int:
        labels = ctx.labels
        records = []

        # --- ci-operator work namespace resources (build-resources/) ---
        for filename, extractor in [
            ("events.json", _extract_events),
            ("pods.json", _extract_pods),
        ]:
            data = _fetch_and_parse(ctx, f"artifacts/build-resources/{filename}")
            if data:
                records.extend(extractor(data, "build", labels))

        # --- test cluster resources (gather-extra/artifacts/) ---
        test_names = _discover_test_names(ctx)
        for test_name in test_names:
            base = f"artifacts/{test_name}/gather-extra/artifacts"
            for filename, extractor in [
                ("events.json", _extract_events),
                ("pods.json", _extract_pods),
                ("deployments.json", _extract_deployments),
            ]:
                data = _fetch_and_parse(ctx, f"{base}/{filename}")
                if data:
                    records.extend(extractor(data, "cluster", labels))

        self.logs_sink.push(records)
        return len(records)
