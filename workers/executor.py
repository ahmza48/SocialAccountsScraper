"""Job executor with concurrency control, circuit breaker, and smart retries.

Called by RQ workers. Each invocation:

1. Acquires a browser slot (global + per-platform limit)
2. Acquires an account (distributed lock + cooldown)
3. Takes a cache stampede lock
4. Runs the platform scraper (Playwright)
5. Stores result, updates job state, releases resources
6. Reports success/failure to the circuit breaker

Resource acquisition is delegated to small context managers in
:mod:`workers.resources` so this module only contains the execution-flow
logic (single-attempt orchestration + retry classification + terminal-state
bookkeeping). Each piece is independently unit-testable.
"""
from __future__ import annotations

import importlib
from typing import Optional

import redis

from account_pool.manager import AccountPoolManager
from cache.manager import CacheManager
from core.exceptions import (
    AccountBlockedError,
    AccountUnavailableError,
    BrowserLimitError,
    ParsingError,
    ScrapingError,
    SessionExpiredError,
)
from core.job_state import JobStateManager
from core.logging_config import get_logger
from core.platform_config import PlatformConfig, get_platform_config
from core.platforms import Platform, parse_platform
from core.redis import get_sync_redis
from queues.dead_letter import DeadLetterQueue
from sessions.manager import SessionManager
from utils.circuit_breaker import CircuitBreaker
from utils.metrics import TimingContext, increment_counter
from utils.retry import RetryContext
from workers.resources import (
    RELEASE_IDLE,
    RELEASE_INVALID,
    JobOutcome,
    acquired_account,
    browser_slot,
    cache_lock,
)

logger = get_logger(__name__)

# Mapping from canonical Platform enum to the dotted import path of the
# scraper class. Keyed by the enum so a typo in a string platform name
# becomes a KeyError at import time, not a silent missing-platform bug.
SCRAPER_MAP: dict = {
    Platform.INSTAGRAM: "scrapers.instagram.scraper.InstagramScraper",
    Platform.TIKTOK: "scrapers.tiktok.scraper.TikTokScraper",
    Platform.FACEBOOK: "scrapers.facebook.scraper.FacebookScraper",
}


def _get_scraper_class(platform: Platform):
    """Dynamically import the scraper class for a given platform."""
    dotted_path = SCRAPER_MAP[platform]
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


# ── Single-attempt orchestration ──────────────────────────────────


def _run_single_attempt(
    *,
    sync_redis: redis.Redis,
    pc: PlatformConfig,
    job_state: JobStateManager,
    account_pool: AccountPoolManager,
    session_mgr: SessionManager,
    cache_mgr: CacheManager,
    circuit: CircuitBreaker,
    outcome: JobOutcome,
    platform_enum: Platform,
    job_id: str,
    username: str,
    cursor: Optional[str],
    log,
) -> Optional[dict]:
    """Run one scrape attempt end-to-end (slot \u2192 account \u2192 cache \u2192 scrape).

    Returns the scrape result on success. Re-raises typed scraper exceptions
    after marking the account context with the appropriate release mode so
    the surrounding context manager performs the right cleanup
    (mark-invalid, release-idle, or normal cooldown).
    """
    platform = platform_enum.value
    with browser_slot(sync_redis, platform, pc.max_browsers(platform)):
        with acquired_account(account_pool, platform, job_id) as acct:
            try:
                with cache_lock(cache_mgr, platform, username) as lock:
                    # Another worker is currently populating the cache for
                    # this (platform, username). If their result has already
                    # landed we can serve it without doing our own scrape;
                    # otherwise we fall through and scrape ourselves.
                    if not lock.held:
                        log.info(
                            "Cache lock held by another worker for %s@%s",
                            username, platform,
                        )
                        cached = cache_mgr.get_profile(platform, username)
                        if cached is not None:
                            job_state.set_completed(job_id, cached)
                            outcome.mark_terminal()
                            circuit.record_success(platform)
                            increment_counter("jobs_completed", platform)
                            # Account wasn't really used \u2014 skip cooldown.
                            acct.release_mode = RELEASE_IDLE
                            return cached

                    ScraperClass = _get_scraper_class(platform_enum)
                    scraper = ScraperClass(acct.data, session_mgr)

                    with TimingContext("scrape_duration", platform):
                        result = scraper.execute(username, cursor)

                    cache_mgr.set_profile(platform, username, result)
                    if result.get("next_cursor"):
                        cache_mgr.set_page(
                            platform, username, result["next_cursor"], result
                        )

                    job_state.set_completed(job_id, result)
                    outcome.mark_terminal()
                    circuit.record_success(platform)
                    increment_counter("jobs_completed", platform)
                    log.info(
                        "Job %s completed successfully", job_id,
                        extra={"job_id": job_id},
                    )
                    return result

            except AccountBlockedError as e:
                # Account is banned/blocked at the platform level; permanently
                # remove from the pool so subsequent jobs don't pick it.
                circuit.record_failure(platform)
                acct.release_mode = RELEASE_INVALID
                acct.invalid_reason = str(e)
                raise

            except SessionExpiredError as e:
                # Session is dead but the account itself is fine; mark the
                # session record invalid and release the account without
                # cooldown so it can be picked up immediately by another job
                # (which will re-login).
                session_mgr.mark_invalid(platform, acct.account_id, str(e))
                acct.release_mode = RELEASE_IDLE
                raise

            except (ParsingError, ScrapingError, BrowserLimitError):
                # These failure classes don't taint the account; let the
                # default cooldown release apply and re-raise for the
                # outer retry classifier to handle.
                circuit.record_failure(platform)
                raise


# ── Public entry point ────────────────────────────────────────────


def execute_scrape_job(
    job_id: str,
    username: str,
    platform: str,
    cursor: str = None,
):
    """Main job execution function \u2014 called by RQ workers.

    ``platform`` is accepted as a plain string (RQ serialises job kwargs as
    JSON, so we can't take a :class:`Platform` directly across the wire) but
    is immediately coerced to the enum so the rest of the function uses a
    type-safe value.

    Failure handling:

    * ``AccountBlockedError`` \u2014 mark account invalid, retry with new account
    * ``SessionExpiredError`` \u2014 mark session invalid, retry (no cooldown)
    * ``BrowserLimitError`` \u2014 wait for slot, retry
    * ``ParsingError`` \u2014 non-retryable, push to DLQ immediately
    * ``ScrapingError`` / other \u2014 retry with exponential backoff
    """
    platform_enum = parse_platform(platform)
    platform = platform_enum.value  # canonical string for downstream consumers

    log = get_logger(f"worker.{platform}")
    log.info(
        "Starting job %s: %s@%s", job_id, username, platform,
        extra={"job_id": job_id, "platform": platform, "username": username},
    )

    sync_redis = get_sync_redis()
    pc = get_platform_config()
    job_state = JobStateManager(sync_redis)
    account_pool = AccountPoolManager()
    session_mgr = SessionManager()
    cache_mgr = CacheManager()
    dlq = DeadLetterQueue()
    circuit = CircuitBreaker(sync_redis)

    job_state.set_processing(job_id, username, platform)

    retry_ctx = RetryContext()
    outcome = JobOutcome()
    attempt = 0

    try:
        while True:
            attempt += 1
            try:
                return _run_single_attempt(
                    sync_redis=sync_redis,
                    pc=pc,
                    job_state=job_state,
                    account_pool=account_pool,
                    session_mgr=session_mgr,
                    cache_mgr=cache_mgr,
                    circuit=circuit,
                    outcome=outcome,
                    platform_enum=platform_enum,
                    job_id=job_id,
                    username=username,
                    cursor=cursor,
                    log=log,
                )

            except AccountUnavailableError:
                # Pool is empty \u2014 retry won't help, surface immediately.
                raise

            except ParsingError:
                # Non-retryable; circuit failure already recorded inside.
                raise

            except (
                AccountBlockedError,
                SessionExpiredError,
                BrowserLimitError,
                ScrapingError,
            ) as e:
                if not retry_ctx.should_retry(e):
                    raise
                log.warning(
                    "Retrying job %s after %s: %s",
                    job_id, type(e).__name__, e,
                    extra={"job_id": job_id},
                )
                retry_ctx.wait()

            except Exception as e:
                # Unknown error class \u2014 treat as transient but log loudly so
                # we can promote it to a typed exception in a future revision.
                log.exception(
                    "Unexpected error in job %s attempt %s",
                    job_id, attempt,
                    extra={"job_id": job_id},
                )
                circuit.record_failure(platform)
                if not retry_ctx.should_retry(e):
                    raise
                retry_ctx.wait()

    except Exception as e:
        # Best-effort terminal-state write. If this itself fails we want the
        # original exception to propagate (so RQ records the failure) but we
        # must NOT clear the dedup key \u2014 leaving it in place causes the next
        # identical request to be deduplicated against the now-stale job
        # record until the dedup TTL expires, which is the safer failure
        # mode than silently allowing duplicate scrapes against the same
        # account.
        try:
            job_state.set_failed(job_id, str(e))
            dlq.push(job_id, platform, username, str(e), attempt)
            increment_counter("jobs_failed", platform)
            outcome.mark_terminal()
        except Exception:
            log.exception(
                "Failed to record terminal-failure state for job %s; "
                "dedup key will be retained until TTL expiry", job_id,
            )
        log.error(
            "Job %s failed permanently: %s", job_id, e,
            extra={"job_id": job_id},
        )
        raise

    finally:
        # Only clear dedup once a terminal state has been persisted. If a
        # crash or Redis failure prevented that write, the dedup key TTL
        # (Config.JOB_DEDUP_TTL_SECONDS) is the recovery path.
        if outcome.terminal_state_written:
            try:
                job_state.clear_dedup(platform, username)
            except Exception:
                log.exception(
                    "Failed to clear dedup key for %s:%s; will expire via TTL",
                    platform, username,
                )
