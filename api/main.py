"""Fully async FastAPI application.

- All endpoints use ``async def`` with non-blocking Redis operations.
- Redis connection pool configured for high concurrency (100 connections).
- Pydantic models validate every request and document the OpenAPI schema.
- Pagination cursors are HMAC-signed so the API only accepts cursors it
  issued (see :mod:`core.security`).
- ``/metrics`` is gated by a bearer token and the same per-IP rate limit.
"""
import json
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

import anyio
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import QueueFullError, DuplicateJobError, CircuitOpenError
from core.platforms import Platform
from core.redis import async_redis_health, get_async_redis, close_async_redis
from core.job_state import AsyncJobStateManager
from core.platform_config import get_platform_config
from core.security import (
    CursorError,
    get_cursor_signer,
    verify_metrics_token,
)
from queues.dispatcher import dispatch_job
from utils.metrics import async_increment_counter
from api.schemas import (
    HealthResponse,
    JobStatusResponse,
    ScrapeCachedResponse,
    ScrapeQueuedResponse,
    ScrapeRequest,
)

logger = get_logger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: warm up Redis pool and increase threadpool.
    Shutdown: close async Redis pool.
    """
    # Allow more threads for the handful of sync RQ calls
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = 100
    # In production, CURSOR_SIGNING_KEY must be explicitly set. Unlike
    # METRICS_AUTH_TOKEN/ADMIN_AUTH_TOKEN (which fail closed by denying
    # requests when unset), an unset cursor key falls back to a per-process
    # random key — safe for a single dev instance, but with multiple API
    # replicas behind a load balancer it causes intermittent "cursor
    # signature mismatch" failures depending on which instance signed vs.
    # verified a given cursor. Refuse to boot rather than serve that
    # instance-dependent behavior in production.
    if Config.ENVIRONMENT == "production" and not Config.CURSOR_SIGNING_KEY:
        raise RuntimeError(
            "CURSOR_SIGNING_KEY must be set when ENVIRONMENT=production "
            "(unset falls back to a per-process random key, which breaks "
            "cursor verification across multiple API instances)."
        )
    # Initialise the cursor signer up-front so the env-var warning (if any)
    # appears at startup rather than on the first request.
    get_cursor_signer()
    # Touch platform config so validation errors surface at boot, not under load.
    get_platform_config()
    # TTL invariants — surface drift between cache, cursor, and job-state
    # lifetimes at startup rather than as silent "data shifted between
    # pages" bugs in production. See F11 in the design notes.
    if Config.CACHE_TTL_SECONDS < Config.CURSOR_TTL_SECONDS:
        logger.warning(
            "CACHE_TTL_SECONDS (%ds) < CURSOR_TTL_SECONDS (%ds): paginated "
            "clients may receive freshly-scraped (and potentially shifted) "
            "data when their cursor outlives the cache. Consider raising "
            "CACHE_TTL_SECONDS to match.",
            Config.CACHE_TTL_SECONDS,
            Config.CURSOR_TTL_SECONDS,
        )
    if Config.JOB_RESULT_TTL_SECONDS < Config.CACHE_TTL_SECONDS:
        logger.warning(
            "JOB_RESULT_TTL_SECONDS (%ds) < CACHE_TTL_SECONDS (%ds): /job/{id} "
            "polls may 404 while /scrape still serves cached results.",
            Config.JOB_RESULT_TTL_SECONDS,
            Config.CACHE_TTL_SECONDS,
        )
    yield
    await close_async_redis()


app = FastAPI(
    title="Social Followers Scraper",
    version="2.0.0",
    lifespan=lifespan,
)

# Admin endpoints live behind their own bearer token (ADMIN_AUTH_TOKEN).
from api.admin import router as admin_router  # noqa: E402  (after app init)
app.include_router(admin_router)


# ── Health ─────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness + Redis read/write probe.

    Always returns 200 so liveness probes don't restart the pod just because
    Redis is briefly unreachable; the body's ``status`` (``ok`` /
    ``degraded``) is what callers should branch on. For traffic-routing
    decisions use ``/readyz`` instead.
    """
    r = await get_async_redis()
    probe = await async_redis_health(r)
    return HealthResponse(
        status="ok" if probe.ok else "degraded",
        redis="connected" if probe.ping_ok else "disconnected",
        redis_writable=probe.write_ok,
    )


@app.get(
    "/readyz",
    response_model=HealthResponse,
    responses={503: {"description": "Dependency unavailable"}},
)
async def readyz() -> HealthResponse:
    """Readiness probe — 503 when Redis cannot serve a write/read round-trip.

    Distinct from ``/health`` so orchestrators (k8s readinessProbe, load
    balancers) can pull traffic when the API would otherwise queue jobs that
    have no way to be persisted.
    """
    r = await get_async_redis()
    probe = await async_redis_health(r)
    if not probe.ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "redis_ping": probe.ping_ok,
                "redis_writable": probe.write_ok,
            },
        )
    return HealthResponse(
        status="ok",
        redis="connected",
        redis_writable=True,
    )


# ── Rate limiting (atomic pipeline) ───────────────────────────────


async def _rate_limit_check(r: aioredis.Redis, scope: str) -> None:
    """Atomic fixed-window rate limiter.

    Uses INCR + conditional EXPIRE so the window TTL is set exactly once per
    window (when the counter transitions 0→1). The earlier version refreshed
    the TTL on every request, which extended a one-second-late 11th request
    into another full ``RATE_LIMIT_WINDOW_SECONDS`` of denial — effectively
    locking abusive clients out indefinitely as long as they kept retrying.
    """
    key = f"ratelimit:{scope}"
    pipe = r.pipeline(transaction=True)
    pipe.incr(key)
    # ``EXPIRE ... NX`` (Redis 7+) sets the TTL only when none is set, which
    # collapses the "is this the first hit?" round-trip into the same
    # pipeline. Older Redis falls back to TTL-based check below.
    pipe.expire(key, Config.RATE_LIMIT_WINDOW_SECONDS, nx=True)
    results = await pipe.execute()
    current = results[0]
    # Defensive: if the EXPIRE somehow didn't take (older Redis returning 0
    # on a key that already has a TTL), only set TTL on the first INCR.
    if current == 1 and not results[1]:
        await r.expire(key, Config.RATE_LIMIT_WINDOW_SECONDS)
    if current > Config.RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )


def _client_ip(request: Optional[Request]) -> str:
    if request is None or request.client is None:
        return "unknown"
    return request.client.host


# ── Cursor wrapping helpers ──────────────────────────────────────


def _sign_outgoing_cursor(payload: Any, platform: str, username: str) -> Any:
    """Replace ``next_cursor`` in a result payload with a signed token.

    Idempotent: if there is no cursor (None or empty) the payload is returned
    untouched. If the payload is not a dict (cached responses sometimes wrap
    the original dict) the structure is preserved.
    """
    if not isinstance(payload, dict):
        return payload
    raw_cursor = payload.get("next_cursor")
    if not raw_cursor:
        return payload
    signed = get_cursor_signer().sign(platform, username, str(raw_cursor))
    # Return a copy so we never mutate cached/shared dicts.
    out = dict(payload)
    out["next_cursor"] = signed
    return out


def _verify_incoming_cursor(
    token: Optional[str], platform: str, username: str
) -> Optional[str]:
    """Unwrap a client-supplied signed cursor; raise 400 on failure."""
    if not token:
        return None
    try:
        return get_cursor_signer().verify(token, platform, username) or None
    except CursorError as exc:
        # Don't leak verification details (signature mismatch vs expiry vs
        # binding mismatch) \u2014 callers only need to know it's invalid.
        logger.info(
            "Rejected cursor for %s@%s: %s", username, platform, exc
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired cursor",
        )


# ── Scrape endpoint ───────────────────────────────────────────────


@app.post(
    "/scrape",
    response_model=Union[ScrapeQueuedResponse, ScrapeCachedResponse],
    responses={
        400: {"description": "Invalid request"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Backpressure or circuit-breaker open"},
    },
)
async def scrape(
    body: ScrapeRequest,
    request: Request,
) -> Union[ScrapeQueuedResponse, ScrapeCachedResponse]:
    """Queue a scrape job (cache-first, with dedup, backpressure, and signed cursors)."""
    pc = get_platform_config()
    if not pc.has(body.platform):
        # Reachable when platform_config.yml omits a Platform enum value.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Platform {body.platform!r} is not configured on this server",
        )

    r = await get_async_redis()
    await _rate_limit_check(r, _client_ip(request))
    await async_increment_counter(r, "requests_received", body.platform)

    raw_cursor = _verify_incoming_cursor(body.cursor, body.platform, body.username)

    # ── Cache-first ────────────────────────────────────────────
    if not raw_cursor:
        cache_key = f"profile:{body.platform}:{body.username}"
    else:
        cache_key = f"profile:{body.platform}:{body.username}:cursor:{raw_cursor}"
    cached_blob = await r.get(cache_key)
    if cached_blob:
        try:
            cached_data = json.loads(cached_blob)
        except json.JSONDecodeError:
            # Don't count corrupt entries as cache hits — they're misses that
            # happen to incur a Redis round-trip. Drop the bad entry so the
            # next request can repopulate cleanly.
            logger.warning("Discarding corrupt cache entry at %s", cache_key)
            await r.delete(cache_key)
        else:
            await async_increment_counter(r, "cache_hits", body.platform)
            return ScrapeCachedResponse(
                status="cached",
                data=_sign_outgoing_cursor(cached_data, body.platform, body.username),
            )

    # ── Dispatch ───────────────────────────────────────────────
    try:
        job_id = await dispatch_job(
            r, body.username, body.platform, cursor=raw_cursor
        )
    except DuplicateJobError as exc:
        # Surface the existing job id from the exception's structured form
        # rather than parsing the message string.
        existing_id = str(exc).split(": ")[-1]
        return ScrapeQueuedResponse(status="processing", job_id=existing_id)
    except QueueFullError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System busy — try again later",
        )
    except CircuitOpenError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Platform {body.platform} temporarily unavailable — "
                f"too many recent failures"
            ),
        )

    return ScrapeQueuedResponse(status="queued", job_id=job_id)


# ── Job status polling ────────────────────────────────────────────


@app.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Poll job status and retrieve result when complete."""
    r = await get_async_redis()
    jsm = AsyncJobStateManager(r)
    state = await jsm.get_state(job_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    data = state.get("result")
    platform = state.get("platform")
    username = state.get("username")
    # Sign the next_cursor on the way out so clients can only re-use cursors
    # that we issued and that are bound to this exact (platform, username).
    if data and platform and username:
        data = _sign_outgoing_cursor(data, platform, username)

    return JobStatusResponse(
        job_id=job_id,
        status=state.get("status", "unknown"),
        data=data,
        error=state.get("error") or None,
        created_at=state.get("created_at"),
        updated_at=state.get("updated_at"),
    )


# ── Metrics (gated) ───────────────────────────────────────────────


async def _require_metrics_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Bearer-token gate + per-IP rate limit for ``/metrics``.

    Fails closed: if ``METRICS_AUTH_TOKEN`` is unset, every request is denied.
    """
    if not verify_metrics_token(authorization):
        # 401 (rather than 403) so curl/clients know to retry with credentials.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    r = await get_async_redis()
    await _rate_limit_check(r, f"metrics:{_client_ip(request)}")


@app.get("/metrics", dependencies=[Depends(_require_metrics_token)])
async def metrics() -> dict:
    """Observability endpoint — per-platform stats, circuit state, browser usage."""
    r = await get_async_redis()
    pc = get_platform_config()

    data: dict = {}
    for platform in pc.platforms:
        queue_key = f"rq:queue:{pc.queue_name(platform)}"
        pipe = r.pipeline(transaction=False)
        pipe.get(f"metrics:{platform}:jobs_dispatched")
        pipe.get(f"metrics:{platform}:jobs_completed")
        pipe.get(f"metrics:{platform}:jobs_failed")
        pipe.get(f"metrics:{platform}:cache_hits")
        pipe.llen(queue_key)
        pipe.get(f"active:browsers:{platform}")
        pipe.get(f"circuit:{platform}:state")
        results = await pipe.execute()

        data[platform] = {
            "dispatched": int(results[0] or 0),
            "completed": int(results[1] or 0),
            "failed": int(results[2] or 0),
            "cache_hits": int(results[3] or 0),
            "queue_length": results[4],
            "active_browsers": int(results[5] or 0),
            "circuit_state": results[6] or "closed",
        }

    # Global browser counters
    global_browsers = await r.get("active:browsers")
    data["_global"] = {
        "total_active_browsers": int(global_browsers or 0),
        "max_browsers": Config.MAX_CONCURRENT_BROWSERS,
    }

    return data
