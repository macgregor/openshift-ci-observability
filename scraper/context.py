"""Build context providing lazy artifact fetching and label extraction."""
from __future__ import annotations

import json
import logging
from typing import Optional

from scraper.models import Build, JobLabels
from scraper.gcs import GCSClient

log = logging.getLogger("scraper")


def extract_job_labels(data) -> JobLabels:
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


class BuildContext:
    def __init__(self, build: Build, gcs: GCSClient):
        self._build = build
        self._gcs = gcs
        self._artifact_cache: dict[str, Optional[str]] = {}
        self._labels: Optional[JobLabels] = None

    @property
    def build(self) -> Build:
        return self._build

    @property
    def labels(self) -> JobLabels:
        if self._labels is None:
            content = self.fetch_artifact("artifacts/ci-operator-metrics.json")
            if content is not None:
                data = json.loads(content)
                self._labels = extract_job_labels(data)
                # Concern: extract_job_labels returns {"build_id": "unknown"} on
                # failure. Use the actual build_id from the Build object instead.
                if self._labels.get("build_id") in ("unknown", ""):
                    self._labels["build_id"] = self._build.build_id
            else:
                self._labels = {
                    "org": "",
                    "repo": "",
                    "branch": "",
                    "job_name": "",
                    "pr_number": "",
                    "pr_sha": "",
                    "author": "",
                    "build_id": self._build.build_id,
                }
        return self._labels

    def fetch_artifact(self, relative_path: str) -> Optional[str]:
        if relative_path in self._artifact_cache:
            return self._artifact_cache[relative_path]
        full_path = (f"{self._build.base_path}/{self._build.pr}/{self._build.job}/"
                     f"{self._build.build_id}/{relative_path}")
        result = self._gcs.fetch_object(full_path)
        self._artifact_cache[relative_path] = result
        return result
