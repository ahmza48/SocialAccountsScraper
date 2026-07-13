"""Integration-style tests for ``workers.executor.execute_scrape_job``.

Patches the resource singletons so the executor exercises its real flow
control (dedup guard, retry loop, terminal-state bookkeeping, account
release-mode handling) without touching Redis, RQ, or Playwright.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import (
    AccountBlockedError,
    AccountUnavailableError,
    ParsingError,
    ScrapingError,
    SessionExpiredError,
)


# ── Helpers ──────────────────────────────────────────────────────


def _stub_account(account_id: str = "acc1") -> dict:
    return {"account_id": account_id, "credentials": {"u": "alice", "p": "pw"}}


@contextmanager
def _patched_executor(
    *,
    scraper_result: dict | Exception = None,
    cache_lock_held: bool = True,
    cached_profile: dict | None = None,
):
    """Patch every collaborator the executor imports.

    Yields a dict of mocks so individual tests can assert on calls.
    """
    if scraper_result is None:
        scraper_result = {"profile": {}, "posts": [], "next_cursor": None}

    sync_redis = MagicMock(name="sync_redis")
    pc = MagicMock(name="platform_config")
    pc.max_browsers.return_value = 5

    job_state = MagicMock(name="job_state")
    pool = MagicMock(name="account_pool")
    pool.acquire_account.return_value = _stub_account()
    session_mgr = MagicMock(name="session_mgr")
    cache_mgr = MagicMock(name="cache_mgr")
    cache_mgr.acquire_populate_lock.return_value = cache_lock_held
    cache_mgr.get_profile.return_value = cached_profile
    dlq = MagicMock(name="dlq")
    circuit = MagicMock(name="circuit")

    scraper_instance = MagicMock(name="scraper_instance")
    if isinstance(scraper_result, Exception):
        scraper_instance.execute.side_effect = scraper_result
    else:
        scraper_instance.execute.return_value = scraper_result
    scraper_class = MagicMock(name="scraper_class", return_value=scraper_instance)

    @contextmanager
    def fake_browser_slot(*_args, **_kwargs):
        yield

    with patch("workers.executor.get_sync_redis", return_value=sync_redis), \
         patch("workers.executor.get_platform_config", return_value=pc), \
         patch("workers.executor.JobStateManager", return_value=job_state), \
         patch("workers.executor.AccountPoolManager", return_value=pool), \
         patch("workers.executor.SessionManager", return_value=session_mgr), \
         patch("workers.executor.CacheManager", return_value=cache_mgr), \
         patch("workers.executor.DeadLetterQueue", return_value=dlq), \
         patch("workers.executor.CircuitBreaker", return_value=circuit), \
         patch("workers.executor._get_scraper_class", return_value=scraper_class), \
         patch("workers.executor.browser_slot", fake_browser_slot), \
         patch("workers.executor.RetryContext") as RetryCtxCls, \
         patch("workers.executor.TimingContext") as TimingCtxCls:

        retry_ctx = MagicMock(name="retry_ctx")
        retry_ctx.should_retry.return_value = False  # no retries by default
        retry_ctx.wait.return_value = None
        RetryCtxCls.return_value = retry_ctx

        # TimingContext is a context manager; make it a no-op.
        timing_instance = MagicMock()
        timing_instance.__enter__ = lambda self: self
        timing_instance.__exit__ = lambda self, *a: None
        TimingCtxCls.return_value = timing_instance

        yield {
            "sync_redis": sync_redis,
            "pc": pc,
            "job_state": job_state,
            "pool": pool,
            "session_mgr": session_mgr,
            "cache_mgr": cache_mgr,
            "dlq": dlq,
            "circuit": circuit,
            "scraper_class": scraper_class,
            "scraper_instance": scraper_instance,
            "retry_ctx": retry_ctx,
        }


# ── Happy path ───────────────────────────────────────────────────


class TestSuccess:
    def test_returns_scraper_result(self) -> None:
        from workers.executor import execute_scrape_job
        result_payload = {"profile": {"name": "alice"}, "posts": [], "next_cursor": None}
        with _patched_executor(scraper_result=result_payload) as m:
            out = execute_scrape_job("job1", "alice", "instagram")
            assert out == result_payload

    def test_clears_dedup_only_after_success(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor() as m:
            execute_scrape_job("job1", "alice", "instagram")
            m["job_state"].set_completed.assert_called_once()
            m["job_state"].clear_dedup.assert_called_once_with(
                "instagram", "alice"
            )

    def test_caches_result(self) -> None:
        from workers.executor import execute_scrape_job
        result_payload = {"profile": {}, "posts": [], "next_cursor": "abc123"}
        with _patched_executor(scraper_result=result_payload) as m:
            execute_scrape_job("job1", "alice", "instagram")
            m["cache_mgr"].set_profile.assert_called_once_with(
                "instagram", "alice", result_payload
            )
            m["cache_mgr"].set_page.assert_called_once_with(
                "instagram", "alice", "abc123", result_payload
            )

    def test_releases_account_with_cooldown(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor() as m:
            execute_scrape_job("job1", "alice", "instagram")
            m["pool"].release_account.assert_called_once_with(
                "acc1", "instagram", apply_cooldown=True
            )
            m["pool"].mark_invalid.assert_not_called()

    def test_records_circuit_success(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor() as m:
            execute_scrape_job("job1", "alice", "instagram")
            m["circuit"].record_success.assert_called_once_with("instagram")
            m["circuit"].record_failure.assert_not_called()


# ── Cache-stampede short-circuit ─────────────────────────────────


class TestCacheStampede:
    def test_serves_cached_when_lock_held_by_other(self) -> None:
        from workers.executor import execute_scrape_job
        cached = {"profile": {"name": "alice"}, "posts": [], "next_cursor": None}
        with _patched_executor(
            cache_lock_held=False, cached_profile=cached
        ) as m:
            out = execute_scrape_job("job1", "alice", "instagram")
            assert out == cached
            m["scraper_instance"].execute.assert_not_called()
            # Releases account without cooldown (we didn't really use it).
            m["pool"].release_account.assert_called_once_with(
                "acc1", "instagram", apply_cooldown=False
            )

    def test_falls_through_when_lock_held_but_cache_empty(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            cache_lock_held=False, cached_profile=None
        ) as m:
            execute_scrape_job("job1", "alice", "instagram")
            m["scraper_instance"].execute.assert_called_once()


# ── Dedup-guard semantics (F2 regression) ────────────────────────


class TestDedupGuard:
    def test_dedup_not_cleared_when_terminal_write_fails(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            scraper_result=ScrapingError("permanent")
        ) as m:
            # Force set_failed itself to raise so terminal state is never written.
            m["job_state"].set_failed.side_effect = RuntimeError("redis down")
            with pytest.raises(ScrapingError):
                execute_scrape_job("job1", "alice", "instagram")
            m["job_state"].clear_dedup.assert_not_called()

    def test_dedup_cleared_when_set_failed_succeeds(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            scraper_result=ScrapingError("permanent")
        ) as m:
            with pytest.raises(ScrapingError):
                execute_scrape_job("job1", "alice", "instagram")
            m["job_state"].set_failed.assert_called_once()
            m["job_state"].clear_dedup.assert_called_once()
            m["dlq"].push.assert_called_once()


# ── Typed-exception cleanup ──────────────────────────────────────


class TestTypedExceptionCleanup:
    def test_account_blocked_marks_invalid(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            scraper_result=AccountBlockedError("banned")
        ) as m:
            with pytest.raises(AccountBlockedError):
                execute_scrape_job("job1", "alice", "instagram")
            m["pool"].mark_invalid.assert_called_once()
            args = m["pool"].mark_invalid.call_args
            assert args.args[0] == "acc1"
            assert args.args[1] == "instagram"
            assert "banned" in args.args[2]
            m["pool"].release_account.assert_not_called()
            m["circuit"].record_failure.assert_called_with("instagram")

    def test_session_expired_releases_idle_and_invalidates_session(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            scraper_result=SessionExpiredError("login redirect")
        ) as m:
            with pytest.raises(SessionExpiredError):
                execute_scrape_job("job1", "alice", "instagram")
            m["session_mgr"].mark_invalid.assert_called_once_with(
                "instagram", "acc1", "login redirect"
            )
            m["pool"].release_account.assert_called_once_with(
                "acc1", "instagram", apply_cooldown=False
            )
            m["pool"].mark_invalid.assert_not_called()

    def test_parsing_error_is_non_retryable(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor(
            scraper_result=ParsingError("bad html")
        ) as m:
            with pytest.raises(ParsingError):
                execute_scrape_job("job1", "alice", "instagram")
            m["retry_ctx"].should_retry.assert_not_called()
            m["circuit"].record_failure.assert_called_with("instagram")
            # Default cooldown release on the account.
            m["pool"].release_account.assert_called_once_with(
                "acc1", "instagram", apply_cooldown=True
            )

    def test_account_unavailable_propagates_without_retry(self) -> None:
        from workers.executor import execute_scrape_job
        with _patched_executor() as m:
            m["pool"].acquire_account.side_effect = AccountUnavailableError("empty")
            with pytest.raises(AccountUnavailableError):
                execute_scrape_job("job1", "alice", "instagram")
            m["retry_ctx"].should_retry.assert_not_called()
