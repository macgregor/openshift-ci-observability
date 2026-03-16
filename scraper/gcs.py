"""GCS client for listing and fetching objects from Google Cloud Storage."""
import logging
import os
import tempfile
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

    def _cache_write_miss(self, gcs_path: str):
        cp = self._cache_path(gcs_path)
        if cp is None:
            return
        miss = cp.with_suffix(cp.suffix + ".miss")
        miss.parent.mkdir(parents=True, exist_ok=True)
        miss.touch()

    def _cache_has_entry(self, gcs_path: str) -> Optional[bool]:
        """Check if cache has an entry. Returns True (exists), False (cached miss), or None (no entry)."""
        cp = self._cache_path(gcs_path)
        if cp is None:
            return None
        if cp.exists():
            return True
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

    def _last_component(self, prefix: str) -> str:
        return prefix.rstrip("/").split("/")[-1]

    def list_prs(self, base_path: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/")]

    def list_jobs(self, base_path: str, pr: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/")]

    def list_builds(self, base_path: str, pr: str, job: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/{job}/")]
