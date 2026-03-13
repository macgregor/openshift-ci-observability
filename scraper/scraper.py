"""Scraper orchestrator coordinating discovery, pipelines, and state."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from scraper.models import Build, Pipeline
from scraper.gcs import GCSClient
from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line

log = logging.getLogger("scraper")


def _fetch_known_build_ids(session: requests.Session, vm_url: str) -> set[str]:
    """Query VictoriaMetrics for all build_ids that have a sentinel metric."""
    try:
        resp = session.get(f"{vm_url}/api/v1/label/build_id/values", timeout=30)
        resp.raise_for_status()
        return set(resp.json().get("data", []))
    except Exception:
        log.warning("Failed to query VM for known build_ids, proceeding without skip list", exc_info=True)
        return set()


def _push_sentinel(session: requests.Session, vm_url: str, build_id: str, timestamp: int):
    """Push a sentinel metric to mark a build as processed."""
    line = format_prometheus_line(
        "ci_build_scraped", {"build_id": build_id}, 1, timestamp,
    )
    if line:
        resp = session.post(
            f"{vm_url}/api/v1/import/prometheus",
            data=line + "\n",
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        resp.raise_for_status()


class Scraper:
    def __init__(self, gcs: GCSClient, session: requests.Session, vm_url: str,
                 pipelines: list[Pipeline], workers: int):
        self.gcs = gcs
        self.session = session
        self.vm_url = vm_url
        self.pipelines = pipelines
        self.workers = workers

    def scrape(self, base_path: str, since: int, until: int, dry_run: bool) -> None:
        since_str = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
        until_str = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d")

        known = _fetch_known_build_ids(self.session, self.vm_url)
        log.info("Found %d known build_ids in VictoriaMetrics", len(known))

        log.info("Listing PRs from %s", base_path)
        prs = self.gcs.list_prs(base_path)
        prs.sort(key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
        log.info("Found %d PRs, scanning for builds in [%s, %s] (newest first, %d workers)",
                 len(prs), since_str, until_str, self.workers)
        ingested = 0
        skipped = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for i, pr in enumerate(prs):
                log.info("Scanning PR %s (%d/%d)", pr, i + 1, len(prs))
                jobs = self.gcs.list_jobs(base_path, pr)
                log.debug("PR %s has %d jobs", pr, len(jobs))
                for job in jobs:
                    builds = self.gcs.list_builds(base_path, pr, job)
                    log.debug("PR %s job %s has %d builds", pr, job, len(builds))
                    new_builds = [b for b in builds if b not in known]
                    skipped += len(builds) - len(new_builds)
                    if not new_builds:
                        continue
                    log.debug("PR %s job %s: %d new builds to check", pr, job, len(new_builds))
                    futures = {
                        executor.submit(self._process_build, base_path, pr, job, bid, since, until, dry_run): bid
                        for bid in new_builds
                    }
                    for future in as_completed(futures):
                        bid = futures[future]
                        try:
                            result = future.result()
                            if result:
                                ingested += 1
                        except Exception:
                            log.error("Failed to process PR %s build %s", pr, bid, exc_info=True)
        log.info("Scrape complete: %d ingested, %d skipped (already in VM)", ingested, skipped)

    def _process_build(self, base_path, pr, job, build_id, since, until, dry_run):
        started_content = self.gcs.fetch_object(f"{base_path}/{pr}/{job}/{build_id}/started.json")
        if started_content is None:
            return False
        started = json.loads(started_content)
        ts = started.get("timestamp", 0)
        if not (since <= ts <= until):
            log.debug("Build %s out of date range (ts=%d)", build_id, ts)
            return False

        build = Build(build_id=build_id, pr=pr, job=job, base_path=base_path)
        ctx = BuildContext(build, self.gcs)

        if dry_run:
            log.info("PR %s build %s: found (dry-run)", pr, build_id)
            return False

        counts = {}
        for pipeline in self.pipelines:
            try:
                count = pipeline.process(ctx)
                counts[pipeline.name] = count
            except Exception:
                log.error("Pipeline %s failed for build %s", pipeline.name, build_id, exc_info=True)

        counts_str = ", ".join(f"{count} {name}" for name, count in counts.items())
        log.info("PR %s build %s: %s", pr, build_id, counts_str)

        _push_sentinel(self.session, self.vm_url, build_id, ts)
        return True
