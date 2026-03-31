"""Tests for ScrapeState."""
import threading

import pytest

from scraper.state import ScrapeState


@pytest.fixture
def state(tmp_path):
    return ScrapeState(str(tmp_path / "state.db"))


# ------------------------------------------------------------------
# Basic state transitions
# ------------------------------------------------------------------


def test_new_build_should_process(state):
    assert state.should_process("build-1", "metrics", "1.0")


def test_mark_done_skips_reprocessing(state):
    state.mark_done("build-1", "metrics", "1.0")
    assert not state.should_process("build-1", "metrics", "1.0")


def test_mark_failed_allows_retry(state):
    state.mark_failed("build-1", "metrics", "1.0")
    assert state.should_process("build-1", "metrics", "1.0", max_retries=3)


def test_max_retries_exhausted(state):
    for _ in range(3):
        state.mark_failed("build-1", "metrics", "1.0")
    assert not state.should_process("build-1", "metrics", "1.0", max_retries=3)


def test_version_change_reprocesses(state):
    state.mark_done("build-1", "metrics", "1.0")
    assert not state.should_process("build-1", "metrics", "1.0")
    # Version bump → should reprocess
    assert state.should_process("build-1", "metrics", "2.0")


# ------------------------------------------------------------------
# Race condition: mark_done vs mark_failed
# ------------------------------------------------------------------


def test_mark_done_overrides_failed(state):
    state.mark_failed("build-1", "metrics", "1.0")
    state.mark_failed("build-1", "metrics", "1.0")
    # Now succeed
    state.mark_done("build-1", "metrics", "1.0")
    assert not state.should_process("build-1", "metrics", "1.0")


def test_mark_failed_preserves_done(state):
    """Simulates race: container B succeeds, then container A reports failure.
    The success must not be reverted."""
    state.mark_done("build-1", "metrics", "1.0")
    state.mark_failed("build-1", "metrics", "1.0")
    # Should still be 'ok'
    assert not state.should_process("build-1", "metrics", "1.0")


# ------------------------------------------------------------------
# Bulk queries
# ------------------------------------------------------------------


def test_get_known_builds(state):
    state.mark_done("build-1", "metrics", "1.0")
    state.mark_done("build-2", "metrics", "1.0")
    state.mark_done("build-3", "logs", "1.0")  # different pipeline
    state.mark_failed("build-4", "metrics", "1.0")  # failed

    known = state.get_known_builds("metrics", "1.0")
    assert known == {"build-1", "build-2"}


def test_get_known_builds_version_filter(state):
    state.mark_done("build-1", "metrics", "1.0")
    state.mark_done("build-2", "metrics", "2.0")
    assert state.get_known_builds("metrics", "1.0") == {"build-1"}
    assert state.get_known_builds("metrics", "2.0") == {"build-2"}


def test_clear_stale_versions(state):
    state.mark_done("build-1", "metrics", "1.0")
    state.mark_done("build-2", "metrics", "1.0")
    state.mark_done("build-3", "metrics", "2.0")

    state.clear_stale_versions("metrics", "2.0")

    # Old version entries should be gone
    assert state.should_process("build-1", "metrics", "2.0")
    assert state.should_process("build-2", "metrics", "2.0")
    # Current version preserved
    assert not state.should_process("build-3", "metrics", "2.0")


def test_reset_clears_all(state):
    state.mark_done("build-1", "metrics", "1.0")
    state.mark_done("build-2", "logs", "1.0")
    state.reset()
    assert state.should_process("build-1", "metrics", "1.0")
    assert state.should_process("build-2", "logs", "1.0")


# ------------------------------------------------------------------
# Concurrency
# ------------------------------------------------------------------


def test_concurrent_mark_done(state):
    errors = []

    def mark(build_id):
        try:
            state.mark_done(build_id, "metrics", "1.0")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=mark, args=(f"build-{i}",))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    known = state.get_known_builds("metrics", "1.0")
    assert len(known) == 20


# ------------------------------------------------------------------
# Error degradation
# ------------------------------------------------------------------


def test_sqlite_error_degrades_gracefully(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("not a database")
    state = ScrapeState(str(db_path))

    assert not state._ok
    # All operations should degrade without crashing
    assert state.should_process("build-1", "metrics", "1.0")  # returns True
    state.mark_done("build-1", "metrics", "1.0")  # no-op
    state.mark_failed("build-1", "metrics", "1.0")  # no-op
    assert state.get_known_builds("metrics", "1.0") == set()
    state.clear_stale_versions("metrics", "1.0")  # no-op
    state.reset()  # no-op


# ------------------------------------------------------------------
# Different pipelines are independent
# ------------------------------------------------------------------


def test_pipelines_are_independent(state):
    state.mark_done("build-1", "metrics", "1.0")
    assert not state.should_process("build-1", "metrics", "1.0")
    # Same build, different pipeline → should process
    assert state.should_process("build-1", "logs", "1.0")
