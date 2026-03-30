"""GCS client for listing and fetching objects from Google Cloud Storage."""
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

GCS_BASE = "https://storage.googleapis.com"
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


class GCSClient:
    def __init__(self, session: requests.Session, bucket: str, cache_dir: Optional[str] = None):
        self.session = session
        self.bucket = bucket
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._miss_cache: dict[Path, set[str]] = {}
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._log_cache_stats()

    def _log_cache_stats(self):
        total_bytes = 0
        file_count = 0
        for dirpath, _dirnames, filenames in os.walk(self._cache_dir):
            for f in filenames:
                file_count += 1
                total_bytes += os.path.getsize(os.path.join(dirpath, f))
        size_mb = total_bytes / (1024 * 1024)
        log.info("GCS cache: %s (%.1f MB, %d files)", self._cache_dir, size_mb, file_count)

    def _cache_path(self, gcs_path: str) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        return self._cache_dir / gcs_path

    def _cache_read_bytes(self, gcs_path: str) -> Optional[bytes]:
        """Read from cache. Returns content bytes, empty bytes for cached miss, or None for cache miss."""
        cp = self._cache_path(gcs_path)
        if cp is None:
            return None
        if cp.exists():
            return cp.read_bytes()
        miss_set = self._load_misses(gcs_path)
        if miss_set is not None:
            if gcs_path in miss_set:
                return b""  # sentinel: known 404
        else:
            miss = cp.with_suffix(cp.suffix + ".miss")
            if miss.exists():
                return b""  # sentinel: known 404
        return None

    def _cache_write(self, gcs_path: str, content: bytes):
        cp = self._cache_path(gcs_path)
        if cp is None:
            return
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=cp.parent)
        try:
            os.write(tmp_fd, content)
            os.close(tmp_fd)
            os.rename(tmp_path, cp)
        except Exception:
            os.close(tmp_fd) if not os.get_inheritable(tmp_fd) else None
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _build_dir(self, gcs_path: str) -> Optional[Path]:
        """Return the cache build directory for a GCS path, or None for non-build paths."""
        if self._cache_dir is None:
            return None
        parts = gcs_path.split("/")
        if len(parts) < 7:
            return None
        return self._cache_dir / "/".join(parts[:6])

    def _load_misses(self, gcs_path: str) -> Optional[set[str]]:
        """Lazy-load the .misses set for the build containing gcs_path."""
        build_dir = self._build_dir(gcs_path)
        if build_dir is None:
            return None
        if build_dir in self._miss_cache:
            return self._miss_cache[build_dir]
        misses_file = build_dir / ".misses"
        if misses_file.exists():
            content = misses_file.read_text(encoding="utf-8")
            entries = {line for line in content.split("\n") if line}
        else:
            entries = self._migrate_legacy_misses(build_dir)
        self._miss_cache[build_dir] = entries
        return entries

    def _migrate_legacy_misses(self, build_dir: Path) -> set[str]:
        """TEMPORARY -- remove once legacy .miss files are aged out."""
        if not build_dir.exists():
            return set()
        try:
            legacy_files = list(build_dir.rglob("*.miss"))
            if not legacy_files:
                return set()
            entries = set()
            for miss_path in legacy_files:
                gcs_path = str(miss_path.with_suffix("").relative_to(self._cache_dir))
                entries.add(gcs_path)
            misses_file = build_dir / ".misses"
            content = "\n".join(entries) + "\n"
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(build_dir))
            try:
                os.write(tmp_fd, content.encode("utf-8"))
                os.close(tmp_fd)
                os.rename(tmp_path, str(misses_file))
            except Exception:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            for miss_path in legacy_files:
                miss_path.unlink(missing_ok=True)
            dirs_to_check = set()
            for miss_path in legacy_files:
                parent = miss_path.parent
                while parent != build_dir:
                    dirs_to_check.add(parent)
                    parent = parent.parent
            for d in sorted(dirs_to_check, key=lambda p: len(p.parts), reverse=True):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            return entries
        except Exception:
            log.warning("Legacy .miss migration failed for %s", build_dir, exc_info=True)
            return set()

    def _cache_write_miss(self, gcs_path: str):
        cp = self._cache_path(gcs_path)
        if cp is None:
            return
        build_dir = self._build_dir(gcs_path)
        if build_dir is None:
            miss = cp.with_suffix(cp.suffix + ".miss")
            miss.parent.mkdir(parents=True, exist_ok=True)
            miss.touch()
            return
        miss_set = self._load_misses(gcs_path)
        miss_set.add(gcs_path)
        os.makedirs(build_dir, exist_ok=True)
        misses_file = build_dir / ".misses"
        fd = os.open(str(misses_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (gcs_path + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def _cache_has_entry(self, gcs_path: str) -> Optional[bool]:
        """Check if cache has an entry. Returns True (exists), False (cached miss), or None (no entry)."""
        cp = self._cache_path(gcs_path)
        if cp is None:
            return None
        if cp.exists():
            return True
        miss_set = self._load_misses(gcs_path)
        if miss_set is not None:
            if gcs_path in miss_set:
                return False
        else:
            miss = cp.with_suffix(cp.suffix + ".miss")
            if miss.exists():
                return False
        return None

    def list_prefixes(self, prefix: str) -> list[str]:
        prefixes = []
        marker = None
        page = 0
        while True:
            params = {"prefix": prefix, "delimiter": "/"}
            if marker:
                params["marker"] = marker
            url = f"{GCS_BASE}/{self.bucket}/"
            log.debug("GET %s", url)
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
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

    def fetch_object(self, path: str) -> Optional[str]:
        cached = self._cache_read_bytes(path)
        if cached is not None:
            return cached.decode("utf-8") if cached else None  # empty = cached miss

        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("GET %s", url)
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 404:
            self._cache_write_miss(path)
            return None
        resp.raise_for_status()
        self._cache_write(path, resp.content)
        return resp.text

    def head_object(self, path: str) -> bool:
        cached = self._cache_has_entry(path)
        if cached is not None:
            return cached

        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("HEAD %s", url)
        resp = self.session.head(url, timeout=10)
        exists = resp.status_code == 200
        if not exists:
            self._cache_write_miss(path)
        # Don't write a content entry for HEAD -- let fetch_binary populate it
        return exists

    def fetch_binary(self, path: str) -> Optional[bytes]:
        cached = self._cache_read_bytes(path)
        if cached is not None:
            return cached if cached else None  # empty = cached miss

        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("GET %s (binary)", url)
        resp = self.session.get(url, timeout=300, stream=True)
        if resp.status_code == 404:
            self._cache_write_miss(path)
            return None
        resp.raise_for_status()
        content = resp.content
        self._cache_write(path, content)
        return content

    @property
    def has_cache(self) -> bool:
        return self._cache_dir is not None

    def ensure_cached(self, path: str) -> Optional[Path]:
        """Ensure a GCS object is on disk. Returns the cache Path, or None if 404/no cache."""
        if self._cache_dir is None:
            return None
        cp = self._cache_path(path)
        if cp.exists():
            return cp
        miss_set = self._load_misses(path)
        if miss_set is not None:
            if path in miss_set:
                return None
        else:
            miss = cp.with_suffix(cp.suffix + ".miss")
            if miss.exists():
                return None
        # Stream to disk
        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("GET %s (stream-to-disk)", url)
        resp = self.session.get(url, timeout=300, stream=True)
        if resp.status_code == 404:
            self._cache_write_miss(path)
            return None
        resp.raise_for_status()
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=cp.parent)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
            os.rename(tmp_path, cp)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return cp

    def read_processed(self, gcs_path: str) -> Optional[str]:
        """Read a .metrics sibling file for the given GCS path."""
        if self._cache_dir is None:
            return None
        metrics_path = self._cache_path(gcs_path + ".metrics")
        if metrics_path is not None and metrics_path.exists():
            return metrics_path.read_text(encoding="utf-8")
        return None

    def write_processed(self, gcs_path: str, content: str):
        """Atomic-write a .metrics sibling file for the given GCS path."""
        if self._cache_dir is None:
            return
        metrics_path = self._cache_path(gcs_path + ".metrics")
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=metrics_path.parent)
        try:
            os.write(tmp_fd, content.encode("utf-8"))
            os.close(tmp_fd)
            os.rename(tmp_path, metrics_path)
        except Exception:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def cleanup_aged_builds(self, cutoff_ts: int):
        """Delete cached build directories older than cutoff_ts and orphaned temp files.

        A build directory is identified by containing a ``started.json`` file.
        Builds with a timestamp older than *cutoff_ts* (or with an unparseable
        ``started.json``) are deleted entirely.  Orphaned ``tmp*`` files older
        than one hour (from interrupted atomic writes) are also removed.

        Safe to call concurrently from multiple processes sharing the same cache
        volume -- all operations are idempotent.
        """
        if self._cache_dir is None:
            return
        try:
            self._cleanup_walk(cutoff_ts)
        except OSError:
            log.warning("Cache cleanup failed (cache dir inaccessible?)", exc_info=True)

    def _cleanup_walk(self, cutoff_ts: int):
        one_hour_ago = time.time() - 3600
        builds_deleted = 0
        tmp_deleted = 0

        for dirpath, dirnames, filenames in os.walk(self._cache_dir):
            # Delete orphaned temp files from interrupted atomic writes.
            for f in filenames:
                if not f.startswith("tmp"):
                    continue
                fp = os.path.join(dirpath, f)
                try:
                    if os.path.getmtime(fp) < one_hour_ago:
                        os.unlink(fp)
                        tmp_deleted += 1
                except OSError:
                    pass

            # Check for build directories (contain started.json).
            if "started.json" not in filenames:
                continue
            started_path = os.path.join(dirpath, "started.json")
            try:
                ts = json.loads(Path(started_path).read_text()).get("timestamp", 0)
            except (json.JSONDecodeError, OSError, ValueError):
                ts = 0  # corrupt/unreadable → treat as ancient
            if ts >= cutoff_ts:
                continue
            shutil.rmtree(dirpath, ignore_errors=True)
            builds_deleted += 1
            dirnames.clear()

        # Second pass: remove empty directories bottom-up.
        # os.rmdir fails on non-empty dirs (OSError), so just try every dir.
        dirs_deleted = 0
        for dirpath, _dirnames, _filenames in os.walk(self._cache_dir, topdown=False):
            if Path(dirpath) == self._cache_dir:
                continue
            try:
                os.rmdir(dirpath)
                dirs_deleted += 1
            except OSError:
                pass

        if builds_deleted or tmp_deleted:
            log.info("Cache cleanup: %d aged builds, %d orphaned temp files, "
                     "%d empty dirs removed", builds_deleted, tmp_deleted, dirs_deleted)

    def _last_component(self, prefix: str) -> str:
        return prefix.rstrip("/").split("/")[-1]

    def list_prs(self, base_path: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/")]

    def list_jobs(self, base_path: str, pr: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/")]

    def list_builds(self, base_path: str, pr: str, job: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/{job}/")]
