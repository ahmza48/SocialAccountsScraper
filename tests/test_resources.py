"""Tests for ``workers.resources`` (resource-acquisition context managers)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from workers.resources import (
    RELEASE_COOLDOWN,
    RELEASE_IDLE,
    RELEASE_INVALID,
    AcquiredAccount,
    JobOutcome,
    acquired_account,
    cache_lock,
)


# ── acquired_account ─────────────────────────────────────────────


@pytest.fixture
def fake_pool():
    """Mock AccountPoolManager with the three lifecycle methods we depend on."""
    pool = MagicMock()
    pool.acquire_account.return_value = {"account_id": "acc1", "credentials": {}}
    return pool


class TestAcquiredAccount:
    def test_default_release_mode_applies_cooldown(self, fake_pool) -> None:
        with acquired_account(fake_pool, "instagram", "job1") as acct:
            assert acct.account_id == "acc1"
            assert acct.release_mode == RELEASE_COOLDOWN
        fake_pool.release_account.assert_called_once_with(
            "acc1", "instagram", apply_cooldown=True
        )
        fake_pool.mark_invalid.assert_not_called()

    def test_release_idle_skips_cooldown(self, fake_pool) -> None:
        with acquired_account(fake_pool, "instagram", "job1") as acct:
            acct.release_mode = RELEASE_IDLE
        fake_pool.release_account.assert_called_once_with(
            "acc1", "instagram", apply_cooldown=False
        )
        fake_pool.mark_invalid.assert_not_called()

    def test_release_invalid_calls_mark_invalid(self, fake_pool) -> None:
        with acquired_account(fake_pool, "instagram", "job1") as acct:
            acct.release_mode = RELEASE_INVALID
            acct.invalid_reason = "blocked at platform"
        fake_pool.mark_invalid.assert_called_once_with(
            "acc1", "instagram", "blocked at platform"
        )
        fake_pool.release_account.assert_not_called()

    def test_release_runs_even_on_exception(self, fake_pool) -> None:
        with pytest.raises(RuntimeError):
            with acquired_account(fake_pool, "instagram", "job1"):
                raise RuntimeError("boom")
        fake_pool.release_account.assert_called_once()

    def test_cleanup_failure_does_not_mask_original(self, fake_pool) -> None:
        fake_pool.release_account.side_effect = RuntimeError("redis down")
        # Original RuntimeError("boom") must propagate, not the cleanup error.
        with pytest.raises(RuntimeError, match="boom"):
            with acquired_account(fake_pool, "instagram", "job1"):
                raise RuntimeError("boom")


# ── cache_lock ───────────────────────────────────────────────────


class TestCacheLock:
    def test_held_when_acquire_returns_true(self) -> None:
        cache_mgr = MagicMock()
        cache_mgr.acquire_populate_lock.return_value = True
        with cache_lock(cache_mgr, "instagram", "alice") as lock:
            assert lock.held is True
        cache_mgr.release_populate_lock.assert_called_once_with(
            "instagram", "alice"
        )

    def test_not_held_when_acquire_returns_false(self) -> None:
        cache_mgr = MagicMock()
        cache_mgr.acquire_populate_lock.return_value = False
        with cache_lock(cache_mgr, "instagram", "alice") as lock:
            assert lock.held is False
        # Must NOT release a lock we don't hold.
        cache_mgr.release_populate_lock.assert_not_called()

    def test_release_on_exception(self) -> None:
        cache_mgr = MagicMock()
        cache_mgr.acquire_populate_lock.return_value = True
        with pytest.raises(RuntimeError):
            with cache_lock(cache_mgr, "instagram", "alice"):
                raise RuntimeError("boom")
        cache_mgr.release_populate_lock.assert_called_once()


# ── JobOutcome ───────────────────────────────────────────────────


class TestJobOutcome:
    def test_default_is_not_terminal(self) -> None:
        assert JobOutcome().terminal_state_written is False

    def test_mark_terminal_flips_flag(self) -> None:
        o = JobOutcome()
        o.mark_terminal()
        assert o.terminal_state_written is True
