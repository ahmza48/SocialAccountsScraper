"""Hardened job executor with concurrency control, circuit breaker, and smart retries.

Called by RQ workers. Each invocation:
    1. Acquires a browser slot (global + per-platform limit)
    2. Acquires an account (distributed lock + cooldown)
    3. Takes a cache stampede lock
    4. Runs the platform scraper (Playwright)
    5. Stores result, updates job state, releases resources
    6. Reports success/failure to the circuit breaker
"""
import importlib
import json

from core.config import Config
from core.logging_config import get_logger
from core.redis import get_sync_redis
from core.job_state import JobStateManager
from core.platform_config import get_platform_config
from core.exceptions import (
    AccountBlockedError,
    AccountUnavailableError,
    BrowserLimitError,
    ParsingError,
    ScrapingError,
    SessionExpiredError,
)
from account_pool.manager import AccountPoolManager
from sessions.manager import SessionManager
from cache.manager import CacheManager
from queues.dead_letter import DeadLetterQueue
from utils.metrics import increment_counter, TimingContext
from utils.retry import RetryContext
from utils.circuit_breaker import CircuitBreaker
from utils.concurrency import BrowserConcurrencyGuard

logger = get_logger(__name__)

SCRAPER_MAP = {
    "instagram": "scrapers.instagram.scraper.InstagramScraper",
    "tiktok": "scrapers.tiktok.scraper.TikTokScraper",
    "facebook": "scrapers.facebook.scraper.FacebookScraper",
}


def _get_scraper_class(platform: str):
    """Dynamically import the scraper class for a given platform."""
    dotted_path = SCRAPER_MAP[platform]
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def execute_scrape_job(
    job_id: str,
    username: str,
    platform: str,
    cursor: str = None,
):
    """Main job execution function — called by RQ workers.

    Failure categories:
        - AccountBlockedError  → mark account invalid, retry with new account
        - SessionExpiredError  → mark session invalid, retry (no cooldown)
        - BrowserLimitError    → wait for slot, retry
        - ParsingError         → non-retryable, push to DLQ immediately
        - ScrapingError/other  → retry with exponential backoff
    """
    log = get_logger(f"worker.{platform}")
    log.info(
        f"Starting job {job_id}: {username}@{platform}",
        extra={"job_id": job_id, "platform": platform, "username": username},
    )

    r = get_sync_redis()
    pc = get_platform_config()
    job_state = JobStateManager(r)
    account_pool = AccountPoolManager()
    session_mgr = SessionManager()
    cache_mgr = CacheManager()
    dlq = DeadLetterQueue()
    circuit = CircuitBreaker(r)

    job_state.set_processing(job_id, username, platform)

    retry_ctx = RetryContext()
    account = None
    attempt = 0

    try:
        while True:
            attempt += 1
            account = None

            try:
                # ── 1. Acquire browser slot (waits up to 60s) ──
                browser_guard = BrowserConcurrencyGuard(
                    r, platform, pc.max_browsers(platform)
                )
                with browser_guard:
                    # ── 2. Acquire account ────────────────────
                    account = account_pool.acquire_account(platform, job_id)
                    account_id = account["account_id"]

                    # ── 3. Cache stampede protection ──────────
                    cache_lock_held = cache_mgr.acquire_populate_lock(
                        platform, username
                    )
                    try:
                        if not cache_lock_held:
                            # Another worker is populating — check if result appeared
                            log.info(
                                f"Cache lock held by another worker for "
                                f"{username}@{platform}",
                            )
                            cached = cache_mgr.get_profile(platform, username)
                            if cached:
                                job_state.set_completed(job_id, cached)
                                circuit.record_success(platform)
                                increment_counter("jobs_completed", platform)
                                account_pool.release_account(
                                    account_id, platform, apply_cooldown=False
                                )
                                account = None
                                return cached

                        # ── 4. Scrape ─────────────────────────
                        ScraperClass = _get_scraper_class(platform)
                        scraper = ScraperClass(account, session_mgr)

                        with TimingContext("scrape_duration", platform):
                            result = scraper.execute(username, cursor)

                        # ── 5. Cache result ───────────────────
                        cache_mgr.set_profile(platform, username, result)
                        if result.get("next_cursor"):
                            cache_mgr.set_page(
                                platform,
                                username,
                                result["next_cursor"],
                                result,
                            )

                        # ── 6. Store result + release ─────────
                        job_state.set_completed(job_id, result)
                        account_pool.release_account(account_id, platform)
                        account = None

                        circuit.record_success(platform)
                        increment_counter("jobs_completed", platform)
                        log.info(
                            f"Job {job_id} completed successfully",
                            extra={"job_id": job_id},
                        )
                        return result

                    finally:
                        if cache_lock_held:
                            cache_mgr.release_populate_lock(platform, username)

            except BrowserLimitError as e:
                log.warning(
                    f"Browser limit for job {job_id}: {e}",
                    extra={"job_id": job_id},
                )
                if not retry_ctx.should_retry(e):
                    raise
                retry_ctx.wait()

            except AccountBlockedError as e:
                log.error(
                    f"Account blocked: {e}",
                    extra={"job_id": job_id},
                )
                circuit.record_failure(platform)
                if account:
                    account_pool.mark_invalid(
                        account["account_id"], platform, str(e)
                    )
                    account = None
                if not retry_ctx.should_retry(e):
                    raise
                retry_ctx.wait()

            except SessionExpiredError as e:
                log.warning(
                    f"Session expired for account "
                    f"{account['account_id'] if account else '?'}",
                    extra={"job_id": job_id},
                )
                if account:
                    session_mgr.mark_invalid(
                        platform, account["account_id"], str(e)
                    )
                    account_pool.release_account(
                        account["account_id"], platform, apply_cooldown=False
                    )
                    account = None
                if not retry_ctx.should_retry(e):
                    raise
                retry_ctx.wait()

            except AccountUnavailableError:
                raise

            except ParsingError as e:
                log.error(
                    f"Parsing error in job {job_id}: {e}",
                    exc_info=True,
                    extra={"job_id": job_id},
                )
                circuit.record_failure(platform)
                raise

            except (ScrapingError, Exception) as e:
                log.error(
                    f"Scraping error in job {job_id} attempt {attempt}: {e}",
                    extra={"job_id": job_id},
                )
                circuit.record_failure(platform)
                if account:
                    account_pool.release_account(
                        account["account_id"], platform
                    )
                    account = None
                if not retry_ctx.should_retry(e):
                    raise
                retry_ctx.wait()

    except Exception as e:
        job_state.set_failed(job_id, str(e))
        dlq.push(job_id, platform, username, str(e), attempt)
        increment_counter("jobs_failed", platform)
        log.error(
            f"Job {job_id} failed permanently: {e}",
            extra={"job_id": job_id},
        )
        raise

    finally:
        # Clean up: release account if still held, clear dedup key
        if account:
            try:
                account_pool.release_account(account["account_id"], platform)
            except Exception:
                pass
        job_state.clear_dedup(platform, username)
