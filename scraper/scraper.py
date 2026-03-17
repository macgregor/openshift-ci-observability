"""Scraper orchestrator coordinating discovery, pipelines, and state."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timezone

import requests

from scraper.models import Build, Pipeline
from scraper.gcs import GCSClient
from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line

log = logging.getLogger("scraper")


def _fetch_known_for_pipeline(session: requests.Session, vm_url: str,
                              pipeline_name: str, pipeline_version: str) -> set[str]:
    """Query VictoriaMetrics for build_ids matching a pipeline at its current version."""
    try:
        resp = session.get(
            f"{vm_url}/api/v1/label/build_id/values",
            params={"match[]": f'ci_pipeline_scraped{{pipeline="{pipeline_name}",pipeline_v="{pipeline_version}"}}'},
            timeout=30,
        )
        resp.raise_for_status()
        return set(resp.json().get("data", []))
    except Exception:
        log.warning("Failed to query VM for known build_ids (pipeline=%s), "
                    "proceeding without skip list", pipeline_name, exc_info=True)
        return set()


def push_pipeline_sentinel(session: requests.Session, vm_url: str,
                           pipeline_name: str, pipeline_version: str,
                           build_id: str):
    """Push a per-pipeline sentinel metric to mark a pipeline+build as processed."""
    line = format_prometheus_line(
        "ci_pipeline_scraped",
        {"build_id": build_id, "pipeline": pipeline_name, "pipeline_v": pipeline_version},
        1, None,
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
                 vl_url: str, pipelines: list[Pipeline], workers: int):
        self.gcs = gcs
        self.session = session
        self.vm_url = vm_url
        self.vl_url = vl_url
        self.pipelines = pipelines
        self.workers = workers

    def scrape(self, base_path: str, since: int, until: int, dry_run: bool) -> None:
        since_str = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
        until_str = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d")

        # Query known builds per pipeline at current version
        known = {}
        for p in self.pipelines:
            known[p.name] = _fetch_known_for_pipeline(
                self.session, self.vm_url, p.name, p.version,
            )
            log.info("Pipeline %s v%s: %d known builds in VM",
                     p.name, p.version, len(known[p.name]))

        # Skip builds where ALL pipelines already processed at current version
        all_known = set.intersection(*known.values()) if known else set()

        log.info("Listing PRs from %s", base_path)
        prs = self.gcs.list_prs(base_path)
        prs.sort(key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
        log.info("Found %d PRs, scanning for builds in [%s, %s] (newest first, %d workers)",
                 len(prs), since_str, until_str, self.workers)
        ingested = 0
        skipped = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            pr_iter = iter(enumerate(prs))
            discover_pending = {}  # future → pr
            build_pending = {}     # future → (pr, bid)

            # Seed pool with initial discoveries
            for _ in range(min(self.workers, len(prs))):
                i, pr = next(pr_iter)
                f = executor.submit(self._discover_builds,
                                    base_path, pr, i + 1, len(prs), all_known)
                discover_pending[f] = pr

            while discover_pending or build_pending:
                done, _ = wait({**discover_pending, **build_pending},
                               return_when=FIRST_COMPLETED)
                for future in done:
                    if future in discover_pending:
                        pr = discover_pending.pop(future)
                        try:
                            new_builds, pr_skipped = future.result()
                            skipped += pr_skipped
                            for bpr, job, bid in new_builds:
                                f = executor.submit(
                                    self._process_build, base_path, bpr, job,
                                    bid, since, until, dry_run, known)
                                build_pending[f] = (bpr, bid)
                        except Exception:
                            log.error("Failed to discover PR %s", pr, exc_info=True)
                        # Submit next discovery to keep pipeline fed
                        pair = next(pr_iter, None)
                        if pair is not None:
                            i, pr = pair
                            f = executor.submit(self._discover_builds,
                                                base_path, pr, i + 1, len(prs), all_known)
                            discover_pending[f] = pr
                    else:
                        pr, bid = build_pending.pop(future)
                        try:
                            if future.result():
                                ingested += 1
                        except Exception:
                            log.error("Failed to process PR %s build %s",
                                      pr, bid, exc_info=True)
        # Wait for async pipeline work (e.g. prometheus.tar processing in its own pool).
        for pipeline in self.pipelines:
            if hasattr(pipeline, 'drain'):
                pipeline.drain()

        log.info("Scrape complete: %d ingested, %d skipped (already in VM)", ingested, skipped)

    def _discover_builds(self, base_path, pr, pr_index, pr_total, all_known):
        """List jobs and builds for a PR, returning new build tuples."""
        log.info("Scanning PR %s (%d/%d)", pr, pr_index, pr_total)
        results = []
        skipped = 0
        jobs = self.gcs.list_jobs(base_path, pr)
        for job in jobs:
            builds = self.gcs.list_builds(base_path, pr, job)
            new_builds = [b for b in builds if b not in all_known]
            skipped += len(builds) - len(new_builds)
            for bid in new_builds:
                results.append((pr, job, bid))
        return results, skipped

    def _process_build(self, base_path, pr, job, build_id, since, until,
                       dry_run, known):
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
            if build_id in known.get(pipeline.name, set()):
                continue  # This pipeline already processed at current version
            try:
                count = pipeline.process(ctx)
                counts[pipeline.name] = count
                if not getattr(pipeline, 'pushes_own_sentinel', False):
                    self._delete_stale_logs_if_needed(pipeline, build_id)
                    push_pipeline_sentinel(
                        self.session, self.vm_url,
                        pipeline.name, pipeline.version, build_id,
                    )
            except Exception:
                log.error("Pipeline %s failed for build %s", pipeline.name, build_id, exc_info=True)

        if counts:
            counts_str = ", ".join(f"{count} {name}" for name, count in counts.items())
            log.info("PR %s build %s: %s", pr, build_id, counts_str)

        return bool(counts)

    def _delete_stale_logs_if_needed(self, pipeline, build_id: str):
        """Delete old log entries before re-pushing on version change."""
        if not getattr(pipeline, '_pushes_logs', False):
            return
        try:
            query = f'build_id:"{build_id}" AND pipeline:"{pipeline.name}"'
            resp = self.session.post(
                f"{self.vl_url}/delete/logsql/query",
                params={"query": query},
                timeout=10,
            )
            if resp.status_code not in (200, 204):
                log.debug("VictoriaLogs delete returned %d for build %s pipeline %s",
                          resp.status_code, build_id, pipeline.name)
        except Exception:
            log.debug("Failed to delete stale logs for build %s pipeline %s",
                      build_id, pipeline.name, exc_info=True)
