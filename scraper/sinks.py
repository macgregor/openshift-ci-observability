"""Data sinks for pushing metrics and logs to backend services."""
import logging

import requests

log = logging.getLogger("scraper")


class VictoriaMetricsSink:
    def __init__(self, session: requests.Session, url: str, batch_size: int = 500):
        self.session = session
        self.url = url
        self.batch_size = batch_size

    def push(self, records: list[str]) -> None:
        if not records:
            return
        url = f"{self.url}/api/v1/import/prometheus"
        total_batches = (len(records) + self.batch_size - 1) // self.batch_size
        for i in range(0, len(records), self.batch_size):
            batch = records[i:i + self.batch_size]
            batch_num = i // self.batch_size + 1
            body = "\n".join(batch) + "\n"
            resp = self.session.post(url, data=body, headers={"Content-Type": "text/plain"}, timeout=30)
            resp.raise_for_status()
            log.debug("Pushed metrics batch %d/%d (%d lines)", batch_num, total_batches, len(batch))


class VictoriaLogsSink:
    def __init__(self, session: requests.Session, url: str, batch_size: int = 500):
        self.session = session
        self.url = url
        self.batch_size = batch_size

    def push(self, records: list[str]) -> None:
        if not records:
            return
        url = f"{self.url}/insert/jsonline"
        total_batches = (len(records) + self.batch_size - 1) // self.batch_size
        for i in range(0, len(records), self.batch_size):
            batch = records[i:i + self.batch_size]
            batch_num = i // self.batch_size + 1
            body = "\n".join(batch) + "\n"
            resp = self.session.post(url, data=body,
                                     params={"_stream_fields": "job_name,build_id"},
                                     headers={"Content-Type": "application/stream+json"},
                                     timeout=30)
            resp.raise_for_status()
            log.debug("Pushed logs batch %d/%d (%d lines)", batch_num, total_batches, len(batch))
