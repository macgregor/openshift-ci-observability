"""Artifact cache and cached GCS client.

ArtifactCache manages all on-disk caching (artifact files, miss tracking,
build metadata, processed output, staging) backed by SQLite + filesystem.

CachedGCSClient composes a pure-HTTP GCSClient with an optional
ArtifactCache to provide transparent fetch-with-cache semantics.
"""
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Iterable, Iterator, Optional

log = logging.getLogger("scraper")

_METRICS_VERSION_PREFIX = "# version="


def _open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


class ArtifactCache:
    """On-disk artifact cache with SQLite metadata.

    Owns: cached artifact files, miss tracking, build metadata, processed
    output (.metrics files), and a staging directory for temporary large files.
    """

    def __init__(self, cache_dir: str):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._staging = self._dir / ".staging"

        self._db_lock = threading.Lock()
        try:
            self._db = _open_db(self._dir / "cache.db")
            self._db.execute("CREATE TABLE IF NOT EXISTS misses "
                             "(gcs_path TEXT PRIMARY KEY)")
            self._db.execute("CREATE TABLE IF NOT EXISTS builds "
                             "(prefix TEXT PRIMARY KEY, started_ts INTEGER NOT NULL)")
            self._db.commit()
            self._db_ok = True
        except sqlite3.Error:
            log.warning("Failed to open cache database; operating without "
                        "miss tracking or build metadata", exc_info=True)
            self._db = None
            self._db_ok = False

        self._wipe_staging()
        self._log_stats()

    # ------------------------------------------------------------------
    # Artifact files
    # ------------------------------------------------------------------

    def get(self, gcs_path: str) -> Optional[bytes]:
        """Read a cached artifact. Returns None if not cached."""
        fp = self._artifact_path(gcs_path)
        if fp.exists():
            return fp.read_bytes()
        return None

    def put(self, gcs_path: str, data: bytes) -> None:
        """Atomic-write an artifact to the cache."""
        self._atomic_write(self._artifact_path(gcs_path), data)

    def put_stream(self, gcs_path: str, chunks: Iterable[bytes]) -> Path:
        """Stream an artifact to disk. Returns the cache path."""
        return self._atomic_write_stream(self._artifact_path(gcs_path), chunks)

    def file_path(self, gcs_path: str) -> Optional[Path]:
        """Return the on-disk path if the artifact is cached, else None."""
        fp = self._artifact_path(gcs_path)
        return fp if fp.exists() else None

    # ------------------------------------------------------------------
    # Miss tracking
    # ------------------------------------------------------------------

    def is_miss(self, gcs_path: str) -> bool:
        """Check if a GCS path is a known 404."""
        if not self._db_ok:
            return False
        try:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT 1 FROM misses WHERE gcs_path = ?", (gcs_path,),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def record_miss(self, gcs_path: str) -> None:
        """Record a GCS path as a known 404."""
        if not self._db_ok:
            return
        try:
            with self._db_lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO misses (gcs_path) VALUES (?)",
                    (gcs_path,),
                )
                self._db.commit()
        except sqlite3.Error:
            log.debug("Failed to record miss for %s", gcs_path, exc_info=True)

    # ------------------------------------------------------------------
    # Processed output (.metrics files)
    # ------------------------------------------------------------------

    def get_processed(self, gcs_path: str, version: str) -> Optional[str]:
        """Read a .metrics file if it exists and the version matches.

        Returns the metric content (may be empty string for builds with no
        metrics), or None on cache miss or version mismatch.
        """
        metrics_path = self._artifact_path(gcs_path + ".metrics")
        if not metrics_path.exists():
            return None
        try:
            content = metrics_path.read_text(encoding="utf-8")
        except OSError:
            return None
        first_line, _, rest = content.partition("\n")
        if first_line != f"{_METRICS_VERSION_PREFIX}{version}":
            return None
        return rest

    def put_processed(self, gcs_path: str, version: str, content: str) -> None:
        """Atomic-write a .metrics file with a version header."""
        header = f"{_METRICS_VERSION_PREFIX}{version}\n"
        data = (header + content).encode("utf-8")
        self._atomic_write(self._artifact_path(gcs_path + ".metrics"), data)

    # ------------------------------------------------------------------
    # Staging (temporary large files like prometheus.tar)
    # ------------------------------------------------------------------

    def stage(self, gcs_path: str, chunks: Iterable[bytes]) -> Path:
        """Stream a large file to the staging directory. Returns its path.

        Staging paths include a UUID so concurrent downloads of the same GCS
        path get unique files. The entire staging directory is wiped at init.
        """
        path_hash = hashlib.sha256(gcs_path.encode()).hexdigest()[:16]
        unique = uuid.uuid4().hex[:8]
        dest = self._staging / f"{path_hash}-{unique}"
        return self._atomic_write_stream(dest, chunks)

    def unstage(self, staged_path: Path) -> None:
        """Delete a staged file."""
        try:
            staged_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Build metadata
    # ------------------------------------------------------------------

    def register_build(self, prefix: str, started_ts: int) -> None:
        """Record a build's prefix and start timestamp for age-based cleanup."""
        if not self._db_ok:
            return
        try:
            with self._db_lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO builds (prefix, started_ts) VALUES (?, ?)",
                    (prefix, started_ts),
                )
                self._db.commit()
        except sqlite3.Error:
            log.debug("Failed to register build %s", prefix, exc_info=True)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, cutoff_ts: int) -> None:
        """Delete cached builds older than cutoff_ts and their associated data."""
        if not self._db_ok:
            self._cleanup_walk_fallback(cutoff_ts)
            return
        try:
            self._cleanup_from_db(cutoff_ts)
        except sqlite3.Error:
            log.warning("SQLite cleanup failed, falling back to tree walk",
                        exc_info=True)
            self._cleanup_walk_fallback(cutoff_ts)

    def _cleanup_from_db(self, cutoff_ts: int) -> None:
        with self._db_lock:
            cursor = self._db.execute(
                "SELECT prefix FROM builds WHERE started_ts < ?", (cutoff_ts,),
            )
            prefixes = [row[0] for row in cursor.fetchall()]
        if not prefixes:
            return

        builds_deleted = 0
        for prefix in prefixes:
            build_path = self._dir / prefix
            if build_path.exists():
                shutil.rmtree(build_path, ignore_errors=True)
                builds_deleted += 1

        with self._db_lock:
            for prefix in prefixes:
                try:
                    self._db.execute(
                        "DELETE FROM misses WHERE gcs_path LIKE ?",
                        (prefix + "%",),
                    )
                except sqlite3.Error:
                    pass
            try:
                self._db.execute(
                    "DELETE FROM builds WHERE started_ts < ?", (cutoff_ts,),
                )
                self._db.commit()
            except sqlite3.Error:
                pass

        # Prune empty directories under deleted prefixes
        for prefix in prefixes:
            # Walk upward from the prefix parent, removing empty dirs
            parts = prefix.split("/")
            for i in range(len(parts) - 1, 0, -1):
                parent = self._dir / "/".join(parts[:i])
                try:
                    os.rmdir(parent)
                except OSError:
                    break  # non-empty or doesn't exist

        if builds_deleted:
            log.info("Cache cleanup: %d aged builds removed", builds_deleted)

    def _cleanup_walk_fallback(self, cutoff_ts: int) -> None:
        """Fallback cleanup via filesystem walk when SQLite is unavailable."""
        import json as _json
        builds_deleted = 0
        for dirpath, dirnames, filenames in os.walk(self._dir):
            if "started.json" not in filenames:
                continue
            started_path = os.path.join(dirpath, "started.json")
            try:
                ts = _json.loads(Path(started_path).read_text()).get("timestamp", 0)
            except (ValueError, OSError):
                ts = 0
            if ts >= cutoff_ts:
                continue
            shutil.rmtree(dirpath, ignore_errors=True)
            builds_deleted += 1
            dirnames.clear()
        if builds_deleted:
            log.info("Cache cleanup (fallback): %d aged builds removed",
                     builds_deleted)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _artifact_path(self, gcs_path: str) -> Path:
        return self._dir / gcs_path

    def _atomic_write(self, dest: Path, data: bytes) -> Path:
        """Write data to dest atomically via tempfile + rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent)
        try:
            os.write(tmp_fd, data)
            os.close(tmp_fd)
            os.rename(tmp_path, dest)
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
        return dest

    def _atomic_write_stream(self, dest: Path, chunks: Iterable[bytes]) -> Path:
        """Stream chunks to dest atomically via tempfile + rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
            os.rename(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return dest

    def _wipe_staging(self) -> None:
        """Remove all staged files (crash recovery)."""
        if self._staging.exists():
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging.mkdir(parents=True, exist_ok=True)

    def _log_stats(self) -> None:
        total_bytes = 0
        file_count = 0
        for dirpath, _dirnames, filenames in os.walk(self._dir):
            if Path(dirpath) == self._staging:
                continue
            for f in filenames:
                file_count += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        size_mb = total_bytes / (1024 * 1024)
        log.info("Artifact cache: %s (%.1f MB, %d files)", self._dir,
                 size_mb, file_count)


class CachedGCSClient:
    """Composition wrapper: GCSClient + optional ArtifactCache.

    Provides cache-aware fetch methods with the same interface that
    BuildContext expects. When cache is None, all operations delegate
    directly to the underlying GCSClient.
    """

    def __init__(self, gcs, cache: Optional[ArtifactCache] = None):
        self._gcs = gcs
        self._cache = cache

    @property
    def cache(self) -> Optional[ArtifactCache]:
        return self._cache

    # ------------------------------------------------------------------
    # Cache-aware fetch methods
    # ------------------------------------------------------------------

    def fetch_object(self, path: str) -> Optional[str]:
        """Fetch a text object, using cache if available."""
        if self._cache is not None:
            cached = self._cache.get(path)
            if cached is not None:
                return cached.decode("utf-8")
            if self._cache.is_miss(path):
                return None

        result = self._gcs.fetch_text(path)
        if result is None:
            if self._cache is not None:
                self._cache.record_miss(path)
            return None
        if self._cache is not None:
            self._cache.put(path, result.encode("utf-8"))
        return result

    def fetch_binary(self, path: str) -> Optional[bytes]:
        """Fetch a binary object, using cache if available."""
        if self._cache is not None:
            cached = self._cache.get(path)
            if cached is not None:
                return cached
            if self._cache.is_miss(path):
                return None

        result = self._gcs.fetch_bytes(path)
        if result is None:
            if self._cache is not None:
                self._cache.record_miss(path)
            return None
        if self._cache is not None:
            self._cache.put(path, result)
        return result

    def head_object(self, path: str) -> bool:
        """Check if an object exists, using cache if available."""
        if self._cache is not None:
            fp = self._cache.file_path(path)
            if fp is not None:
                return True
            if self._cache.is_miss(path):
                return False

        exists = self._gcs.head(path)
        if not exists and self._cache is not None:
            self._cache.record_miss(path)
        return exists

    def ensure_cached(self, path: str) -> Optional[Path]:
        """Ensure an object is on disk. Returns the cache path, or None."""
        if self._cache is None:
            return None
        fp = self._cache.file_path(path)
        if fp is not None:
            return fp
        if self._cache.is_miss(path):
            return None

        chunks = self._gcs.stream(path)
        if chunks is None:
            self._cache.record_miss(path)
            return None
        return self._cache.put_stream(path, chunks)

    def ensure_staged(self, path: str) -> Optional[Path]:
        """Download an object to the staging directory. Returns the staged
        path, or None if 404 or no cache. Unlike ensure_cached, staged files
        are wiped at init (crash recovery) and should be explicitly unstaged
        after use.
        """
        if self._cache is None:
            return None
        if self._cache.is_miss(path):
            return None

        chunks = self._gcs.stream(path)
        if chunks is None:
            self._cache.record_miss(path)
            return None
        return self._cache.stage(path, chunks)

    # ------------------------------------------------------------------
    # Delegation (listing is never cached)
    # ------------------------------------------------------------------

    def list_prefixes(self, prefix: str) -> list[str]:
        return self._gcs.list_prefixes(prefix)

    def list_prs(self, base_path: str) -> list[str]:
        return self._gcs.list_prs(base_path)

    def list_jobs(self, base_path: str, pr: str) -> list[str]:
        return self._gcs.list_jobs(base_path, pr)

    def list_builds(self, base_path: str, pr: str, job: str) -> list[str]:
        return self._gcs.list_builds(base_path, pr, job)
