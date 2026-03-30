"""CLI entry point for the scraper package."""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from scraper.gcs import GCSClient, make_session
from scraper.sinks import VictoriaMetricsSink, VictoriaLogsSink
from scraper.metrics import MetricsPipeline
from scraper.logs import LogPipeline
from scraper.junit import JunitPipeline
from scraper.cluster_pool import ClusterPoolPipeline
from scraper.test_cluster_metrics import TestClusterMetricsPipeline
from scraper.step_graph import StepGraphPipeline
from scraper.build_resources import BuildResourcesPipeline
from scraper.scraper import Scraper

BUCKET = "test-platform-results"

log = logging.getLogger("scraper")


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


def _normalize_repo(value):
    """Normalize a repo value to org/repo format, handling common mistakes."""
    if not value:
        return value
    # Strip trailing slashes, .git suffix, whitespace
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    # Handle full GitHub URLs: https://github.com/org/repo
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value


def parse_args():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--repo", default=os.environ.get("REPO"),
                        help="GitHub org/repo to scrape (e.g. openshift/cluster-monitoring-operator). "
                             "env: REPO. Required.")
    parent.add_argument("--vm-url", default=os.environ.get("VM_URL", "http://localhost:8428"),
                        help="VictoriaMetrics URL (env: VM_URL, default: http://localhost:8428)")
    parent.add_argument("--vl-url", default=os.environ.get("VL_URL", "http://localhost:9428"),
                        help="VictoriaLogs URL (env: VL_URL, default: http://localhost:9428)")
    parent.add_argument("--dry-run", action="store_true",
                        help="Log what would be ingested without pushing to VM/VL")
    parent.add_argument("--window", default=os.environ.get("WINDOW", "24h"),
                        help="Lookback window, e.g. 24h, 7d, 90d, 6m (env: WINDOW, default: 24h)")
    parent.add_argument("--workers", type=int,
                        default=int(os.environ.get("WORKERS", "8")),
                        help="Parallel fetch workers (env: WORKERS, default: 8)")
    parent.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity (env: LOG_LEVEL, default: INFO)")
    parent.add_argument("--cache-dir", default=os.environ.get("GCS_CACHE_DIR"),
                        help="Local cache directory for GCS objects (env: GCS_CACHE_DIR)")
    parent.add_argument("--no-cache", action="store_true",
                        default=os.environ.get("GCS_NO_CACHE", "").lower() in ("1", "true", "yes"),
                        help="Disable GCS artifact caching (env: GCS_NO_CACHE)")
    parent.add_argument("--cache-retention",
                        default=os.environ.get("CACHE_RETENTION"),
                        help="Cache retention period, e.g. 90d, 6m. Cached builds older than "
                             "this are deleted. Default: max(--window, 90d). (env: CACHE_RETENTION)")

    parser = argparse.ArgumentParser(description="CI Operator Metrics Scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    watch_p = sub.add_parser("watch", parents=[parent])
    watch_p.add_argument("--poll-interval", type=int,
                         default=int(os.environ.get("POLL_INTERVAL", "300")),
                         help="Seconds between poll cycles (env: POLL_INTERVAL, default: 300)")

    sub.add_parser("backfill", parents=[parent])

    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.repo:
        print("Error: --repo is required (or set REPO in .env).\n"
              "  This is the GitHub org/repo whose CI builds you want to scrape.\n"
              "  Use the same org/repo as the GitHub URL path.\n\n"
              "  Examples:\n"
              "    REPO=openshift/cluster-monitoring-operator\n"
              "    REPO=openshift/installer\n"
              "    REPO=opendatahub-io/opendatahub-operator",
              file=sys.stderr)
        sys.exit(1)

    args.repo = _normalize_repo(args.repo)
    if "/" not in args.repo or args.repo.count("/") != 1:
        print(f"Error: --repo must be in org/repo format, got: {args.repo!r}\n"
              "  Use the GitHub org and repo name separated by a slash.\n\n"
              "  Examples:\n"
              "    openshift/cluster-monitoring-operator\n"
              "    opendatahub-io/opendatahub-operator",
              file=sys.stderr)
        sys.exit(1)

    session = make_session(args.workers)
    cache_dir = None if args.no_cache else args.cache_dir
    gcs = GCSClient(session, BUCKET, cache_dir=cache_dir)
    vm_sink = VictoriaMetricsSink(session, args.vm_url)
    vl_sink = VictoriaLogsSink(session, args.vl_url)
    pipelines = [
        MetricsPipeline(vm_sink),
        LogPipeline(vl_sink),
        JunitPipeline(vm_sink, vl_sink),
        ClusterPoolPipeline(vm_sink),
        TestClusterMetricsPipeline(vm_sink, gcs, session, args.vm_url),
        StepGraphPipeline(vm_sink, vl_sink),
        BuildResourcesPipeline(vl_sink),
    ]
    scraper = Scraper(gcs, session, args.vm_url, args.vl_url, pipelines, args.workers)

    org_repo = args.repo.replace("/", "_")
    base_path = f"pr-logs/pull/{org_repo}"

    log.info("Starting scraper: repo=%s, vm=%s, vl=%s, dry_run=%s, workers=%d",
             args.repo, args.vm_url, args.vl_url, args.dry_run, args.workers)

    delta = _parse_duration(args.window)
    if args.cache_retention:
        retention_delta = _parse_duration(args.cache_retention)
    else:
        retention_delta = max(delta, timedelta(days=90))

    if args.command == "watch":
        log.info("Watch mode: window=%s, poll=%ds, repo=%s", args.window, args.poll_interval, args.repo)
        while True:
            now = datetime.now(timezone.utc)
            since_ts = int((now - delta).timestamp())
            until_ts = int(now.timestamp())
            scraper.scrape(base_path, since_ts, until_ts, args.dry_run)
            if gcs.has_cache:
                cutoff = int((datetime.now(timezone.utc) - retention_delta).timestamp())
                gcs.cleanup_aged_builds(cutoff)
            log.info("Sleeping %ds before next poll", args.poll_interval)
            time.sleep(args.poll_interval)

    elif args.command == "backfill":
        now = datetime.now(timezone.utc)
        since_ts = int((now - delta).timestamp())
        until_ts = int(now.timestamp())
        log.info("Backfill mode: last %s, repo=%s", args.window, args.repo)
        scraper.scrape(base_path, since_ts, until_ts, args.dry_run)
        if gcs.has_cache:
            cutoff = int((datetime.now(timezone.utc) - retention_delta).timestamp())
            gcs.cleanup_aged_builds(cutoff)
        log.info("Backfill complete")


if __name__ == "__main__":
    main()
