"""Pipeline processing state backed by SQLite.

Tracks which pipeline+build combinations have been processed, at which
version, with retry counting for transient failures. Replaces the
ci_pipeline_scraped sentinel metrics in VictoriaMetrics.
"""
import logging
import sqlite3
import threading
from typing import Optional

log = logging.getLogger("scraper")


def _open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


class ScrapeState:
    """Pipeline processing state with retry tracking.

    Each pipeline+build combination is tracked with a status ('ok' or 'error')
    and an attempt counter. Success is a terminal state: once a build is marked
    done, a subsequent mark_failed call will not revert it (handles the race
    where two containers process the same build concurrently).
    """

    def __init__(self, db_path: str):
        try:
            self._db = _open_db(db_path)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_state (
                    build_id TEXT NOT NULL,
                    pipeline TEXT NOT NULL,
                    pipeline_v TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (build_id, pipeline)
                )
            """)
            self._db.commit()
            self._ok = True
        except sqlite3.Error:
            log.warning("Failed to open state database; all builds will be "
                        "processed (no skip list)", exc_info=True)
            self._db = None
            self._ok = False
        self._lock = threading.Lock()

    def mark_done(self, build_id: str, pipeline: str, version: str) -> None:
        """Mark a pipeline+build as successfully processed.

        Always overwrites -- success is the terminal state.
        """
        if not self._ok:
            return
        try:
            with self._lock:
                self._db.execute("""
                    INSERT OR REPLACE INTO pipeline_state
                        (build_id, pipeline, pipeline_v, status, attempts)
                    VALUES (?, ?, ?, 'ok', 0)
                """, (build_id, pipeline, version))
                self._db.commit()
        except sqlite3.Error:
            log.debug("Failed to mark build %s pipeline %s as done",
                      build_id, pipeline, exc_info=True)

    def mark_failed(self, build_id: str, pipeline: str, version: str) -> None:
        """Record a pipeline failure, incrementing the attempt counter.

        Will NOT overwrite a success status (handles the race where
        another container already succeeded for this build).
        """
        if not self._ok:
            return
        try:
            with self._lock:
                self._db.execute("""
                    INSERT INTO pipeline_state
                        (build_id, pipeline, pipeline_v, status, attempts)
                    VALUES (?, ?, ?, 'error', 1)
                    ON CONFLICT(build_id, pipeline) DO UPDATE SET
                        pipeline_v = excluded.pipeline_v,
                        attempts = attempts + 1
                    WHERE status != 'ok'
                """, (build_id, pipeline, version))
                self._db.commit()
        except sqlite3.Error:
            log.debug("Failed to mark build %s pipeline %s as failed",
                      build_id, pipeline, exc_info=True)

    def should_process(self, build_id: str, pipeline: str, version: str,
                       max_retries: int = 3) -> bool:
        """Check whether a pipeline should process a build.

        Returns True if:
        - Build is not in the table (new)
        - Build has a different version (version bump → reprocess)
        - Build has status='error' with attempts < max_retries (retry)

        Returns False if:
        - Build has status='ok' at the current version (already done)
        - Build has status='error' with attempts >= max_retries (exhausted)
        """
        if not self._ok:
            return True
        try:
            with self._lock:
                row = self._db.execute("""
                    SELECT status, pipeline_v, attempts FROM pipeline_state
                    WHERE build_id = ? AND pipeline = ?
                """, (build_id, pipeline)).fetchone()
        except sqlite3.Error:
            return True

        if row is None:
            return True

        status, stored_version, attempts = row
        if stored_version != version:
            return True  # version change → reprocess
        if status == "ok":
            return False  # already done at current version
        # status == 'error'
        if attempts >= max_retries:
            return False  # exhausted retries
        return True  # retry transient failure

    def get_known_builds(self, pipeline: str, version: str) -> set[str]:
        """Return build_ids successfully processed at the given version."""
        if not self._ok:
            return set()
        try:
            with self._lock:
                cursor = self._db.execute("""
                    SELECT build_id FROM pipeline_state
                    WHERE pipeline = ? AND pipeline_v = ? AND status = 'ok'
                """, (pipeline, version))
                return {row[0] for row in cursor.fetchall()}
        except sqlite3.Error:
            return set()

    def clear_stale_versions(self, pipeline: str,
                             current_version: str) -> None:
        """Delete entries for old versions of a pipeline."""
        if not self._ok:
            return
        try:
            with self._lock:
                self._db.execute("""
                    DELETE FROM pipeline_state
                    WHERE pipeline = ? AND pipeline_v != ?
                """, (pipeline, current_version))
                self._db.commit()
        except sqlite3.Error:
            log.debug("Failed to clear stale versions for pipeline %s",
                      pipeline, exc_info=True)

    def reset(self) -> None:
        """Delete all state. Used by make wipe-db."""
        if not self._ok:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM pipeline_state")
                self._db.commit()
        except sqlite3.Error:
            log.warning("Failed to reset state", exc_info=True)
