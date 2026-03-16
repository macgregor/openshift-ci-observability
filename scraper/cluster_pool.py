"""Cluster pool pipeline for extracting Hive clusterClaim/clusterDeployment metrics."""
import json
import logging
import re
from datetime import datetime

from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line
from scraper.models import Sink

log = logging.getLogger("scraper")

GO_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


def parse_go_duration(s):
    """Parse Go duration string like '4h0m0s' to seconds."""
    if not isinstance(s, str):
        return None
    m = GO_DURATION_RE.fullmatch(s.strip())
    if not m or not any(m.groups()):
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _parse_iso(val):
    """Parse ISO timestamp to Unix seconds."""
    if not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _find_condition(conditions, condition_type):
    """Find a condition by type in a Hive conditions list."""
    if not isinstance(conditions, list):
        return None
    for c in conditions:
        if c.get("type") == condition_type:
            return c
    return None


def extract_cluster_pool_metrics(claim_data, deployment_data, job_labels):
    """Extract Prometheus metrics from clusterClaim and clusterDeployment JSON."""
    metrics = []

    # Common labels from clusterClaim
    pool_name = ""
    if claim_data:
        pool_name = claim_data.get("spec", {}).get("clusterPoolName", "")

    # Enrich from clusterDeployment labels
    ocp_version = ""
    cloud_region = ""
    power_state = ""
    if deployment_data:
        deploy_labels = deployment_data.get("metadata", {}).get("labels", {})
        ocp_version = deploy_labels.get("hive.openshift.io/version", "")
        cloud_region = deployment_data.get("spec", {}).get("platform", {}).get("aws", {}).get("region", "")
        power_state = deployment_data.get("spec", {}).get("powerState", "")

    pool_labels = {
        **job_labels,
        "cluster_pool": pool_name,
        "ocp_version": ocp_version,
        "cloud_region": cloud_region,
        "power_state": power_state,
    }

    # Use claimedTimestamp as the metric timestamp (when the build got its cluster)
    claimed_ts = None
    if deployment_data:
        claimed_ts = _parse_iso(
            deployment_data.get("spec", {}).get("clusterPoolRef", {}).get("claimedTimestamp")
        )
    if claim_data and claimed_ts is None:
        # Fall back to the Pending condition transition
        pending = _find_condition(
            claim_data.get("status", {}).get("conditions", []), "Pending"
        )
        if pending:
            claimed_ts = _parse_iso(pending.get("lastTransitionTime"))
    ts = int(claimed_ts) if claimed_ts else None

    # --- Claim wait time ---
    if claim_data:
        created_ts = _parse_iso(claim_data.get("metadata", {}).get("creationTimestamp"))
        if created_ts and claimed_ts:
            wait = round(claimed_ts - created_ts, 1)
            line = format_prometheus_line("ci_cluster_claim_wait_seconds", pool_labels, wait, ts)
            if line:
                metrics.append(line)

        # Claim lifetime
        lifetime = parse_go_duration(claim_data.get("spec", {}).get("lifetime"))
        if lifetime is not None:
            line = format_prometheus_line("ci_cluster_claim_lifetime_seconds", pool_labels, lifetime, ts)
            if line:
                metrics.append(line)

    # --- Install duration ---
    if deployment_data:
        install_start = _parse_iso(deployment_data.get("status", {}).get("installStartedTimestamp"))
        install_end = _parse_iso(deployment_data.get("status", {}).get("installedTimestamp"))
        if install_start and install_end:
            duration = round(install_end - install_start, 1)
            line = format_prometheus_line("ci_cluster_install_duration_seconds", pool_labels, duration, ts)
            if line:
                metrics.append(line)

        # Idle time: how long cluster sat in pool before being claimed
        if install_end and claimed_ts:
            idle = round(claimed_ts - install_end, 1)
            line = format_prometheus_line("ci_cluster_idle_seconds", pool_labels, idle, ts)
            if line:
                metrics.append(line)

    return metrics


class ClusterPoolPipeline:
    name = "cluster_pool"

    def __init__(self, sink: Sink):
        self.sink = sink

    def process(self, ctx: BuildContext) -> int:
        claim_content = ctx.fetch_artifact("artifacts/build-resources/clusterClaim.json")
        deploy_content = ctx.fetch_artifact("artifacts/build-resources/clusterDeployment.json")

        if claim_content is None and deploy_content is None:
            return 0

        claim_data = json.loads(claim_content) if claim_content else None
        deploy_data = json.loads(deploy_content) if deploy_content else None

        metrics = extract_cluster_pool_metrics(claim_data, deploy_data, ctx.labels)
        self.sink.push(metrics)
        return len(metrics)
