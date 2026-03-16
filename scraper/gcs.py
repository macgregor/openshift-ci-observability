"""GCS client for listing and fetching objects from Google Cloud Storage."""
import logging
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
    def __init__(self, session: requests.Session, bucket: str):
        self.session = session
        self.bucket = bucket

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
        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("GET %s", url)
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    def head_object(self, path: str) -> bool:
        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("HEAD %s", url)
        resp = self.session.head(url, timeout=10)
        return resp.status_code == 200

    def fetch_binary(self, path: str) -> Optional[bytes]:
        url = f"{GCS_BASE}/{self.bucket}/{path}"
        log.debug("GET %s (binary)", url)
        resp = self.session.get(url, timeout=300, stream=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def _last_component(self, prefix: str) -> str:
        return prefix.rstrip("/").split("/")[-1]

    def list_prs(self, base_path: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/")]

    def list_jobs(self, base_path: str, pr: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/")]

    def list_builds(self, base_path: str, pr: str, job: str) -> list[str]:
        return [self._last_component(p) for p in self.list_prefixes(f"{base_path}/{pr}/{job}/")]
