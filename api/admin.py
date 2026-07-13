"""Admin endpoints — account onboarding, DLQ inspection, job cancellation.

All routes are gated by a bearer token (``ADMIN_AUTH_TOKEN``) that is
intentionally separate from the metrics token so a leaked read-only secret
cannot be escalated into mutating account or job state.

Sync collaborators (``AccountPoolManager``, ``DeadLetterQueue``) are invoked
through ``asyncio.to_thread`` so the FastAPI event loop never blocks on the
sync Redis client. Read-only operations that we can express directly against
the async client (DLQ list, job lookups) skip the thread hop to keep the
admin surface lightweight.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status

from account_pool.manager import AccountPoolManager
from core.exceptions import AccountUnavailableError
from core.job_state import _job_key, _dedup_key
from core.logging_config import get_logger
from core.platform_config import get_platform_config
from core.platforms import parse_platform
from core.redis import get_async_redis, get_sync_redis
from core.security import verify_admin_token
from queues.dead_letter import DeadLetterQueue
from api.schemas import (
    AccountActionResponse,
    AccountInvalidateRequest,
    AccountRegisterRequest,
    DLQEntry,
    DLQListResponse,
    JobCancelResponse,
    PoolStatusResponse,
)

logger = get_logger(__name__)


# ── Auth gate ────────────────────────────────────────────────────


async def _require_admin_token(
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Bearer-token gate for all admin routes (fail-closed)."""
    if not verify_admin_token(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(_require_admin_token)],
)


# ── Helpers ──────────────────────────────────────────────────────


def _ensure_platform_configured(platform: str) -> str:
    """Validate + normalise a platform value or raise 400/404."""
    try:
        canonical = parse_platform(platform).value
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not get_platform_config().has(canonical):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Platform {canonical!r} not configured on this server",
        )
    return canonical


def _new_pool_manager() -> AccountPoolManager:
    """Construct a fresh manager bound to the shared sync pool.

    The manager is cheap (one Lua SCRIPT LOAD per construction), so we don't
    bother caching it across requests — admin traffic is low-volume by
    definition and a per-request instance keeps the call site stateless.
    """
    return AccountPoolManager(redis_client=get_sync_redis())


def _new_dlq() -> DeadLetterQueue:
    return DeadLetterQueue(redis_client=get_sync_redis())


# ── Accounts ─────────────────────────────────────────────────────


@router.post(
    "/accounts",
    response_model=AccountActionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Account already registered"}},
)
async def register_account(body: AccountRegisterRequest) -> AccountActionResponse:
    """Register an account into the pool with encrypted credentials.

    Returns 409 when the account already exists. Operators who genuinely want
    to replace a record (e.g. rotating credentials) should call DELETE on the
    underlying account first; we don't expose ``overwrite=true`` over the API
    so an accidentally-re-run onboarding script can't silently un-cooldown a
    suspended account.
    """
    platform = _ensure_platform_configured(body.platform)

    def _do_register() -> None:
        _new_pool_manager().register_account(
            account_id=body.account_id,
            platform=platform,
            credentials=body.credentials,
            proxy=body.proxy,
        )

    try:
        await asyncio.to_thread(_do_register)
    except ValueError as exc:
        # register_account raises ValueError on duplicate (overwrite=False).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    logger.info(
        "Admin registered account %s for %s",
        body.account_id,
        platform,
        extra={"account_id": body.account_id, "platform": platform},
    )
    return AccountActionResponse(
        account_id=body.account_id, platform=platform, status="registered"
    )


@router.get(
    "/accounts/{platform}",
    response_model=PoolStatusResponse,
)
async def pool_status(platform: str) -> PoolStatusResponse:
    """Return aggregate counts for the platform's account pool."""
    platform = _ensure_platform_configured(platform)
    counts = await asyncio.to_thread(
        lambda: _new_pool_manager().get_pool_status(platform)
    )
    return PoolStatusResponse(platform=platform, **counts)


@router.post(
    "/accounts/{platform}/{account_id}/invalidate",
    response_model=AccountActionResponse,
)
async def invalidate_account(
    platform: str,
    account_id: str,
    body: AccountInvalidateRequest,
) -> AccountActionResponse:
    """Mark an account permanently invalid and remove it from the active pool."""
    platform = _ensure_platform_configured(platform)
    r = await get_async_redis()
    if not await r.exists(f"account:{platform}:{account_id}"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id!r} not found for {platform}",
        )
    await asyncio.to_thread(
        lambda: _new_pool_manager().mark_invalid(account_id, platform, body.reason)
    )
    return AccountActionResponse(
        account_id=account_id, platform=platform, status="invalid"
    )


# ── Dead-letter queue ────────────────────────────────────────────


@router.get("/dlq", response_model=DLQListResponse)
async def list_dlq(
    platform: Optional[str] = None,
    start: int = 0,
    count: int = 50,
) -> DLQListResponse:
    """List failed jobs from the DLQ.

    Aggregation across platforms (when ``platform`` is omitted) merges every
    per-platform list and sorts by ``failed_at`` descending so the global
    view stays time-ordered. Both the read and length probe go through
    :class:`DeadLetterQueue` so the API never embeds DLQ key layout.
    """
    if start < 0 or count <= 0 or count > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid pagination (0 <= start, 0 < count <= 500)",
        )

    if platform is not None:
        platform = _ensure_platform_configured(platform)

    def _fetch():
        dlq = _new_dlq()
        return dlq.length(platform), dlq.list_failed(platform, start, count)

    total, raw_entries = await asyncio.to_thread(_fetch)

    entries: list[DLQEntry] = []
    for payload in raw_entries:
        try:
            entries.append(DLQEntry(**payload))
        except (TypeError, ValueError) as exc:
            # The reader already drops unparseable JSON; this guards against
            # entries whose schema drifted (e.g. missing ``attempts``).
            logger.warning("Skipping schema-drifted DLQ entry: %s", exc)
            continue

    return DLQListResponse(platform=platform, total=int(total), entries=entries)


# ── Jobs ─────────────────────────────────────────────────────────


@router.delete("/jobs/{job_id}", response_model=JobCancelResponse)
async def cancel_job(job_id: str) -> JobCancelResponse:
    """Cancel a pending/processing job and clear its dedup guard.

    Does not interrupt a running worker (RQ ``SimpleWorker`` does not support
    in-flight cancellation); the job's terminal-state writer in
    :mod:`workers.executor` will simply find no state hash and skip writing.
    The dedup key is cleared so callers can immediately re-queue the same
    target.
    """
    if not job_id or len(job_id) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid job_id"
        )
    r = await get_async_redis()
    state = await r.hgetall(_job_key(job_id))
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )

    platform = state.get("platform")
    username = state.get("username")

    pipe = r.pipeline(transaction=True)
    pipe.delete(_job_key(job_id))
    if platform and username:
        pipe.delete(_dedup_key(platform, username))
    results = await pipe.execute()
    cancelled = bool(results[0])
    dedup_cleared = bool(results[1]) if (platform and username) else False

    logger.info(
        "Admin cancelled job %s (platform=%s username=%s)",
        job_id,
        platform,
        username,
    )
    return JobCancelResponse(
        job_id=job_id, cancelled=cancelled, dedup_cleared=dedup_cleared
    )
