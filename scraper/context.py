"""Build context providing lazy artifact fetching and label extraction."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from scraper.models import Build, JobLabels

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
            "build_id": job_spec.get("buildid", ""),
            "config_hash": "",
        }
    except (StopIteration, KeyError, IndexError):
        log.debug("Incomplete job labels in ci-operator-metrics.json, "
                   "using defaults")
        return {"build_id": "unknown", "config_hash": ""}


class BuildContext:
    def __init__(self, build: Build, gcs):
        self._build = build
        self._gcs = gcs
        self._artifact_cache: dict[str, Optional[str]] = {}
        self._binary_cache: dict[str, Optional[bytes]] = {}
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
                    "build_id": self._build.build_id,
                }
            self._labels["config_hash"] = self._compute_config_hash()
        return self._labels

    def _compute_config_hash(self) -> str:
        content = self.fetch_artifact("artifacts/ci-operator-step-graph.json")
        if content is None:
            return ""
        try:
            steps = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return ""
        structural = sorted(
            [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                    "dependencies": s.get("dependencies", []),
                }
                for s in steps
            ],
            key=lambda s: s["name"],
        )
        canonical = json.dumps(structural, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def fetch_artifact(self, relative_path: str) -> Optional[str]:
        if relative_path in self._artifact_cache:
            return self._artifact_cache[relative_path]
        result = self._gcs.fetch_object(self._full_path(relative_path))
        self._artifact_cache[relative_path] = result
        return result

    def list_artifact_dirs(self, relative_prefix: str) -> list[str]:
        full_prefix = self._full_path(relative_prefix)
        return [p.split(relative_prefix)[-1].rstrip("/")
                for p in self._gcs.list_prefixes(full_prefix)]

    def head_artifact(self, relative_path: str) -> bool:
        return self._gcs.head_object(self._full_path(relative_path))

    def fetch_artifact_binary(self, relative_path: str) -> Optional[bytes]:
        if relative_path in self._binary_cache:
            return self._binary_cache[relative_path]
        result = self._gcs.fetch_binary(self._full_path(relative_path))
        self._binary_cache[relative_path] = result
        return result

    def artifact_cache_path(self, relative_path: str) -> Optional[Path]:
        """Download artifact to disk cache and return its Path (None if 404 or no cache)."""
        full_path = self._full_path(relative_path)
        return self._gcs.ensure_cached(full_path)

    def artifact_gcs_path(self, relative_path: str) -> str:
        """Return the full GCS path for an artifact."""
        return self._full_path(relative_path)

    @property
    def gcs(self) -> GCSClient:
        return self._gcs

    def _full_path(self, relative_path: str) -> str:
        return (f"{self._build.base_path}/{self._build.pr}/{self._build.job}/"
                f"{self._build.build_id}/{relative_path}")
