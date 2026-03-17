"""Log pipeline for parsing and ingesting ci-operator logs."""
import json
import logging

from scraper import SHARED_VERSION
from scraper.models import Sink
from scraper.context import BuildContext

log = logging.getLogger("scraper")


class LogPipeline:
    name = "logs"
    version = f"{SHARED_VERSION}.1"
    _pushes_logs = True

    def __init__(self, sink: Sink):
        self.sink = sink

    def process(self, ctx: BuildContext) -> int:
        content = ctx.fetch_artifact("artifacts/ci-operator.log")
        if content is None:
            return 0

        records = []
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            time_val = entry.get("time", "")
            msg_val = entry.get("msg", "")

            # Flatten scalar fields from the log entry
            scalars = {}
            for k, v in entry.items():
                if k in ("time", "msg"):
                    continue
                if isinstance(v, (str, int, float, bool)):
                    scalars[k] = v

            # Labels win over log scalars; _time, _msg, source, pipeline always set
            record = {**scalars, **ctx.labels, "_time": time_val, "_msg": msg_val,
                      "source": "ci-operator", "pipeline": "logs"}
            records.append(json.dumps(record))

        self.sink.push(records)
        return len(records)
