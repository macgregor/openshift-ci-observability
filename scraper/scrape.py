import argparse
import json
import logging
import os
import re
import time
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

GCS_BASE = "https://storage.googleapis.com"
BUCKET = "test-platform-results"
XML_NS = "{http://doc.s3.amazonaws.com/2006-03-01}"

log = logging.getLogger("scraper")


def make_session(pool_size=10):
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


SESSION = make_session()


def init_session(pool_size):
    global SESSION
    SESSION = make_session(pool_size)


def gcs_get(url, **kwargs):
    log.debug("GET %s", url)
    resp = SESSION.get(url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def list_gcs_prefixes(bucket, prefix, delimiter="/"):
    prefixes = []
    marker = None
    page = 0
    while True:
        params = {"prefix": prefix, "delimiter": delimiter}
        if marker:
            params["marker"] = marker
        resp = gcs_get(f"{GCS_BASE}/{bucket}/", params=params)
        root = ET.fromstring(resp.content)
        page_prefixes = []
        for cp in root.findall(f".//{XML_NS}CommonPrefixes"):
            p = cp.find(f"{XML_NS}Prefix")
            if p is not None and p.text:
                page_prefixes.append(p.text)
        prefixes.extend(page_prefixes)
        page += 1
        log.debug("GCS listing page %d for prefix=%s: %d prefixes", page, prefix, len(page_prefixes))
        is_truncated = root.find(f"{XML_NS}IsTruncated")
        if is_truncated is not None and is_truncated.text == "true":
            next_marker = root.find(f"{XML_NS}NextMarker")
            if next_marker is not None and next_marker.text:
                marker = next_marker.text
            else:
                break
        else:
            break
    return prefixes


def _last_component(prefix):
    return prefix.rstrip("/").split("/")[-1]


def list_prs(base_path):
    return [_last_component(p) for p in list_gcs_prefixes(BUCKET, f"{base_path}/")]


def list_jobs(base_path, pr):
    return [_last_component(p) for p in list_gcs_prefixes(BUCKET, f"{base_path}/{pr}/")]


def list_builds(base_path, pr, job):
    return [_last_component(p) for p in list_gcs_prefixes(BUCKET, f"{base_path}/{pr}/{job}/")]


def fetch_started_json(base_path, pr, job, build_id):
    url = f"{GCS_BASE}/{BUCKET}/{base_path}/{pr}/{job}/{build_id}/started.json"
    try:
        resp = gcs_get(url)
        return resp.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            log.debug("No started.json for PR %s build %s", pr, build_id)
            return None
        raise


def fetch_metrics_json(base_path, pr, job, build_id):
    url = f"{GCS_BASE}/{BUCKET}/{base_path}/{pr}/{job}/{build_id}/artifacts/ci-operator-metrics.json"
    try:
        resp = gcs_get(url)
        return resp.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            log.debug("No ci-operator-metrics.json for PR %s build %s", pr, build_id)
            return None
        raise


def load_state(path):
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            log.debug("Loaded state with %d builds from %s", len(data), path)
            return data
    except FileNotFoundError:
        log.debug("No state file at %s, starting fresh", path)
        return {}
    except json.JSONDecodeError:
        log.warning("Corrupt state file at %s, starting fresh", path)
        return {}


def save_state(state, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, path)
    log.debug("Saved state with %d builds to %s", len(state), path)


def extract_job_labels(data):
    try:
        started = next(e for e in data.get("test_platform_insights", []) if e.get("name") == "started")
        job_spec = started["additional_context"]["job_spec"]
        pulls = job_spec.get("pulls", [])
        return {
            "org": job_spec.get("org", ""),
            "repo": job_spec.get("repo", ""),
            "branch": job_spec.get("branch", ""),
            "job_name": job_spec.get("job", ""),
            "pr_number": str(pulls[0]["number"]) if pulls else "",
            "pr_sha": pulls[0].get("sha", "")[:12] if pulls else "",
            "author": pulls[0].get("author", "") if pulls else "",
            "build_id": job_spec.get("buildid", ""),
        }
    except (StopIteration, KeyError, IndexError):
        log.warning("Could not extract job labels from test_platform_insights")
        return {"build_id": "unknown"}


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
        if section == "nodes" and "resources" in key:
            if isinstance(value, str):
                parsed = parse_k8s_quantity(value)
                if parsed is not None:
                    return parsed
    except Exception:
        pass
    return value


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

    for key, value in flatten_numeric_fields(entry):
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


def _locator_name(event):
    try:
        return event["locator"]["name"]
    except (KeyError, TypeError):
        return "?"


def convert_to_logs(data, job_labels):
    logs = []
    timestamp = None
    try:
        started = next(e for e in data.get("test_platform_insights", []) if e.get("name") == "started")
        timestamp = started.get("timestamp", "")
    except StopIteration:
        pass

    raw_entry = {
        "_time": timestamp or "",
        "_msg": json.dumps(data),
        "source": "raw",
        **job_labels,
    }
    logs.append(json.dumps(raw_entry))

    section_msg_builders = {
        "events": lambda e: f"event: {_locator_name(e)} {e.get('message', {}).get('reason', '')} ({e.get('message', {}).get('annotations', {}).get('duration_seconds', '?')}s)",
        "pods": lambda e: f"pod: {e.get('pod_name', '?')} {e.get('pod_phase', '?')}",
        "openshift_builds": lambda e: f"build: {e.get('name', '?')} {e.get('status', '?')} ({e.get('duration_seconds', '?')}s)",
        "nodes": lambda e: f"node: {e.get('node', '?')} {e.get('arch', '?')} {e.get('machine_type', '?')}",
        "images": lambda e: f"image: {e.get('full_name', '?')} success={e.get('success', '?')}",
        "leases": lambda e: f"lease: {e.get('name', '?')}",
        "test_platform_insights": lambda e: f"insight: {e.get('name', '?')}",
    }

    for section in SECTIONS:
        entries = data.get(section, [])
        if not isinstance(entries, list):
            continue
        msg_builder = section_msg_builders.get(section, lambda e: f"{section}: entry")
        for entry in entries:
            try:
                entry_ts = parse_timestamp_best_effort(entry)
                ts_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat() if entry_ts else (timestamp or "")
                flat = {}
                for k, v in entry.items():
                    if isinstance(v, (str, int, float, bool)):
                        flat[k] = v
                section_source = section.rstrip("s") if section != "test_platform_insights" else "insight"
                log_entry = {
                    "_time": ts_str,
                    "_msg": msg_builder(entry),
                    **flat,
                    **job_labels,
                    "source": section_source,
                }
                logs.append(json.dumps(log_entry))
            except Exception:
                log.error("Failed to convert log from %s entry", section, exc_info=True)
    return logs


METRICS_BATCH_SIZE = 500


def push_metrics(metrics_lines, vm_url):
    url = f"{vm_url}/api/v1/import/prometheus"
    total_batches = (len(metrics_lines) + METRICS_BATCH_SIZE - 1) // METRICS_BATCH_SIZE
    for i in range(0, len(metrics_lines), METRICS_BATCH_SIZE):
        batch = metrics_lines[i:i + METRICS_BATCH_SIZE]
        batch_num = i // METRICS_BATCH_SIZE + 1
        body = "\n".join(batch) + "\n"
        resp = SESSION.post(url, data=body, headers={"Content-Type": "text/plain"}, timeout=30)
        resp.raise_for_status()
        log.debug("Pushed metrics batch %d/%d (%d lines)", batch_num, total_batches, len(batch))


def push_logs(log_lines, vl_url):
    url = f"{vl_url}/insert/jsonline"
    body = "\n".join(log_lines) + "\n"
    resp = SESSION.post(url, data=body, params={"_stream_fields": "job_name,build_id"},
                        headers={"Content-Type": "application/json"}, timeout=30)
    resp.raise_for_status()
    log.debug("Pushed %d log lines", len(log_lines))


def _process_build(base_path, pr, job, build_id, since_ts, until_ts):
    """Fetch and filter a single build. Returns (build_id, data) or None."""
    started = fetch_started_json(base_path, pr, job, build_id)
    if started is None:
        return None
    ts = started.get("timestamp", 0)
    if not (since_ts <= ts <= until_ts):
        log.debug("Build %s out of date range (ts=%d)", build_id, ts)
        return None
    data = fetch_metrics_json(base_path, pr, job, build_id)
    if data is None:
        return None
    return (build_id, data)


def scrape_builds(base_path, since_ts, until_ts, state, state_file, vm_url, vl_url, dry_run, workers):
    since_str = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    until_str = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    log.info("Listing PRs from %s", base_path)
    prs = list_prs(base_path)
    prs.sort(key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
    log.info("Found %d PRs, scanning for builds in [%s, %s] (newest first, %d workers)",
             len(prs), since_str, until_str, workers)
    ingested = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, pr in enumerate(prs):
            log.info("Scanning PR %s (%d/%d)", pr, i + 1, len(prs))
            jobs = list_jobs(base_path, pr)
            log.debug("PR %s has %d jobs", pr, len(jobs))
            for job in jobs:
                builds = list_builds(base_path, pr, job)
                log.debug("PR %s job %s has %d builds", pr, job, len(builds))
                new_builds = [b for b in builds if b not in state]
                skipped += len(builds) - len(new_builds)
                if not new_builds:
                    continue
                log.debug("PR %s job %s: %d new builds to check", pr, job, len(new_builds))
                futures = {
                    executor.submit(_process_build, base_path, pr, job, bid, since_ts, until_ts): bid
                    for bid in new_builds
                }
                for future in as_completed(futures):
                    bid = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        log.error("Failed to fetch PR %s build %s", pr, bid, exc_info=True)
                        continue
                    if result is None:
                        continue
                    build_id, data = result
                    job_labels = extract_job_labels(data)
                    metrics = convert_to_metrics(data, job_labels)
                    logs = convert_to_logs(data, job_labels)
                    log.info("PR %s build %s: %d metrics, %d logs%s",
                             pr, build_id, len(metrics), len(logs),
                             " (dry-run)" if dry_run else "")
                    if not dry_run:
                        try:
                            push_metrics(metrics, vm_url)
                            push_logs(logs, vl_url)
                            state[build_id] = datetime.now(timezone.utc).isoformat()
                            save_state(state, state_file)
                            ingested += 1
                        except Exception:
                            log.error("Failed to ingest PR %s build %s", pr, build_id, exc_info=True)
                            continue
    log.info("Scrape complete: %d ingested, %d skipped (already in state)", ingested, skipped)


def _parse_duration(s):
    """Parse a duration string like 90d, 6m, 1y, 24h into a timedelta."""
    units = {"h": "hours", "d": "days", "w": "weeks"}
    s = s.strip()
    if s.endswith("m"):
        return timedelta(days=int(s[:-1]) * 30)
    if s.endswith("y"):
        return timedelta(days=int(s[:-1]) * 365)
    for suffix, kwarg in units.items():
        if s.endswith(suffix):
            return timedelta(**{kwarg: int(s[:-1])})
    return timedelta(days=int(s))


def parse_args():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--repo", default=os.environ.get("REPO", "opendatahub-io/opendatahub-operator"),
                        help="GitHub org/repo (env: REPO, default: opendatahub-io/opendatahub-operator)")
    parent.add_argument("--vm-url", default=os.environ.get("VM_URL", "http://localhost:8428"),
                        help="VictoriaMetrics URL (env: VM_URL, default: http://localhost:8428)")
    parent.add_argument("--vl-url", default=os.environ.get("VL_URL", "http://localhost:9428"),
                        help="VictoriaLogs URL (env: VL_URL, default: http://localhost:9428)")
    parent.add_argument("--state-file", default=os.environ.get("STATE_FILE", ".scrape-state.json"),
                        help="State file path (env: STATE_FILE, default: .scrape-state.json)")
    parent.add_argument("--dry-run", action="store_true",
                        help="Log what would be ingested without pushing to VM/VL")
    parent.add_argument("--workers", type=int,
                        default=int(os.environ.get("WORKERS", "8")),
                        help="Parallel fetch workers (env: WORKERS, default: 8)")
    parent.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity (env: LOG_LEVEL, default: INFO)")

    parser = argparse.ArgumentParser(description="CI Operator Metrics Scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    watch_p = sub.add_parser("watch", parents=[parent])
    watch_p.add_argument("--window-hours", type=int,
                         default=int(os.environ.get("WATCH_WINDOW_HOURS", "24")),
                         help="Lookback window in hours (env: WATCH_WINDOW_HOURS, default: 24)")
    watch_p.add_argument("--poll-interval", type=int,
                         default=int(os.environ.get("POLL_INTERVAL", "300")),
                         help="Seconds between poll cycles (env: POLL_INTERVAL, default: 300)")

    backfill_p = sub.add_parser("backfill", parents=[parent])
    backfill_p.add_argument("--window",
                            default=os.environ.get("BACKFILL_WINDOW", "90d"),
                            help="How far back to backfill (env: BACKFILL_WINDOW, default: 90d)")

    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")

    init_session(args.workers)
    org_repo = args.repo.replace("/", "_")
    base_path = f"pr-logs/pull/{org_repo}"

    log.info("Starting scraper: repo=%s, vm=%s, vl=%s, state=%s, dry_run=%s, workers=%d",
             args.repo, args.vm_url, args.vl_url, args.state_file, args.dry_run, args.workers)
    state = load_state(args.state_file)

    if args.command == "watch":
        log.info("Watch mode: window=%dh, poll=%ds, repo=%s", args.window_hours, args.poll_interval, args.repo)
        while True:
            now = datetime.now(timezone.utc)
            since_ts = int((now - timedelta(hours=args.window_hours)).timestamp())
            until_ts = int(now.timestamp())
            state = load_state(args.state_file)
            scrape_builds(base_path, since_ts, until_ts, state, args.state_file,
                          args.vm_url, args.vl_url, args.dry_run, args.workers)
            log.info("Sleeping %ds before next poll", args.poll_interval)
            time.sleep(args.poll_interval)

    elif args.command == "backfill":
        now = datetime.now(timezone.utc)
        delta = _parse_duration(args.window)
        since_ts = int((now - delta).timestamp())
        until_ts = int(now.timestamp())
        log.info("Backfill mode: last %s, repo=%s", args.window, args.repo)
        scrape_builds(base_path, since_ts, until_ts, state, args.state_file,
                      args.vm_url, args.vl_url, args.dry_run, args.workers)
        log.info("Backfill complete")


if __name__ == "__main__":
    main()
