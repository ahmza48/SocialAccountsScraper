"""Resource-acquisition helpers for the job executor.

The executor was previously a single 180-line function that interleaved
browser-slot reservation, account checkout, cache stampede locking, scrape
invocation, retry classification, and terminal-state bookkeeping. This
module breaks the resource-management pieces out into small, testable
context managers so the executor itself shrinks to a coordination loop.

Each helper is intentionally narrow:

* :class:`BrowserSlot` \u2014 thin wrapper around :class:`BrowserConcurrencyGuard`
  so the executor never imports the implementation directly.
* :class:`AcquiredAccount` \u2014 acquires an account on enter, releases on exit.
  The body sets ``release_mode`` to control whether release applies the
  cooldown, leaves the account idle, or marks it permanently invalid.
* :class:`CacheLock` \u2014 stampede-protection lock; ``held`` tells the body
  whether *this* worker won the race to populate.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import redis

from account_pool.manager import AccountPoolManager
from cache.manager import CacheManager
from core.logging_config import get_logger
from utils.concurrency import BrowserConcurrencyGuard

logger = get_logger(__name__)


# ── Browser slot ────────────────────────────────────────────────


@contextmanager
def browser_slot(
    sync_redis: redis.Redis,
    platform: str,
    max_browsers: int,
    max_wait_seconds: int = 60,
) -> Iterator[None]:
    """Reserve a browser slot for the duration of the ``with`` block.

    Wraps :class:`BrowserConcurrencyGuard` so callers don't need to know about
    the guard's internal Lua scripts. Raises :class:`BrowserLimitError` if the
    slot can't be acquired within ``max_wait_seconds``.
    """
    guard = BrowserConcurrencyGuard(sync_redis, platform, max_browsers)
    guard.acquire(max_wait_seconds=max_wait_seconds)
    try:
        yield
    finally:
        guard.release()


# ── Account ─────────────────────────────────────────────────────


# Release modes are deliberately string sentinels (rather than an Enum) so
# they JSON-serialise cleanly if we ever want to surface the outcome in
# logs or metrics.
RELEASE_COOLDOWN = "cooldown"   # default \u2014 normal end of a successful job
RELEASE_IDLE = "idle"           # short-circuit cases (cache hit, session swap)
RELEASE_INVALID = "invalid"     # account is blocked/banned, never use again


@dataclass
class AcquiredAccount:
    """Container for the acquired account dict + the chosen release mode.

    Mutable on purpose: handlers inside the ``with`` block update
    ``release_mode`` (and optionally ``invalid_reason``) to control how the
    pool entry is cleaned up on exit.
    """

    data: dict
    release_mode: str = RELEASE_COOLDOWN
    invalid_reason: str = ""

    @property
    def account_id(self) -> str:
        return self.data["account_id"]


@contextmanager
def acquired_account(
    pool: AccountPoolManager,
    platform: str,
    job_id: str,
) -> Iterator[AcquiredAccount]:
    """Acquire an account, yield it, and release/invalidate on exit.

    The body controls the cleanup decision by mutating ``acquired.release_mode``
    (and ``acquired.invalid_reason`` for ``RELEASE_INVALID``). This keeps the
    branching out of the executor's main loop while still letting per-exception
    handlers express their intent locally.

    Cleanup is best-effort: a release/invalidate failure is logged but does not
    mask the original exception (if any) thrown inside the ``with`` block.
    """
    account_dict = pool.acquire_account(platform, job_id)
    acquired = AcquiredAccount(data=account_dict)
    try:
        yield acquired
    finally:
        try:
            if acquired.release_mode == RELEASE_INVALID:
                pool.mark_invalid(
                    acquired.account_id, platform, acquired.invalid_reason
                )
            elif acquired.release_mode == RELEASE_IDLE:
                pool.release_account(
                    acquired.account_id, platform, apply_cooldown=False
                )
            else:  # RELEASE_COOLDOWN (default)
                pool.release_account(
                    acquired.account_id, platform, apply_cooldown=True
                )
        except Exception:  # pragma: no cover \u2014 cleanup is best-effort
            logger.exception(
                "Failed to release account %s for %s (mode=%s); "
                "relying on lock TTL",
                acquired.account_id,
                platform,
                acquired.release_mode,
            )


# ── Cache stampede lock ─────────────────────────────────────────


@dataclass
class CacheLockState:
    """Whether the current worker holds the cache populate lock."""

    held: bool


@contextmanager
def cache_lock(
    cache_mgr: CacheManager, platform: str, username: str
) -> Iterator[CacheLockState]:
    """Try to acquire the cache stampede lock; release on exit if acquired.

    Yields a :class:`CacheLockState` with ``held=True`` when *this* worker won
    the race, ``False`` when another worker is already populating. The body
    is expected to handle both cases (typically: re-check the cache when
    ``held=False``, then either return the cached result or fall through to
    a normal scrape).
    """
    state = CacheLockState(
        held=cache_mgr.acquire_populate_lock(platform, username)
    )
    try:
        yield state
    finally:
        if state.held:
            try:
                cache_mgr.release_populate_lock(platform, username)
            except Exception:  # pragma: no cover \u2014 cleanup is best-effort
                logger.exception(
                    "Failed to release cache lock for %s@%s",
                    username,
                    platform,
                )


# ── Outcome bookkeeping ─────────────────────────────────────────


@dataclass
class JobOutcome:
    """Tracks whether the executor has persisted a terminal state.

    The flag gates :func:`JobStateManager.clear_dedup` so a worker crash
    between the failure write and the dedup delete cannot leak the dedup key
    (see F2). Mutating one field here is cheaper and clearer than passing
    a boolean through every code path.
    """

    terminal_state_written: bool = False

    def mark_terminal(self) -> None:
        self.terminal_state_written = True
