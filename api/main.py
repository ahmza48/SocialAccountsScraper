"""Fully async FastAPI application.

- All endpoints use ``async def`` with non-blocking Redis operations.
- Redis connection pool configured for high concurrency (100 connections).
- Atomic rate limiter (pipeline), circuit breaker integration, structured job state.
"""
import json
from contextlib import asynccontextmanager

import anyio
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import QueueFullError, DuplicateJobError, CircuitOpenError
from core.redis import get_async_redis, close_async_redis
from core.job_state import AsyncJobStateManager
from core.platform_config import get_platform_config
from queues.dispatcher import dispatch_job
from utils.metrics import async_increment_counter

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
    yield
    await close_async_redis()


app = FastAPI(
    title="Social Followers Scraper",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Health ─────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    r = await get_async_redis()
    # yet to decide whether to restart the container if redis is not up
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": "connected" if redis_ok else "disconnected",
    }


# ── Rate limiting (atomic pipeline) ───────────────────────────────


async def _rate_limit_check(r: aioredis.Redis, client_ip: str) -> None:
    """Atomic rate limiter — INCR + EXPIRE in a single pipeline."""
    key = f"ratelimit:{client_ip}"
    pipe = r.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, Config.RATE_LIMIT_WINDOW_SECONDS)
    results = await pipe.execute()
    current = results[0]
    if current > Config.RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


# ── Scrape endpoint ───────────────────────────────────────────────


@app.post("/scrape")
async def scrape(
    username: str,
    platform: str,
    cursor: str = None,
    request: Request = None,
):
    """Queue a scrape job (cache-first, with dedup and backpressure)."""
    pc = get_platform_config()
    if platform not in pc.platforms:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform: {platform}. "
            f"Supported: {pc.platforms}",
        )
    if not username or len(username) > 100:
        raise HTTPException(status_code=400, detail="Invalid username")

    r = await get_async_redis()
    client_ip = request.client.host if request else "unknown"
    await _rate_limit_check(r, client_ip)
    await async_increment_counter(r, "requests_received", platform)

    # ── Cache-first ────────────────────────────────────────────
    if not cursor:
        cached = await r.get(f"profile:{platform}:{username}")
        if cached:
            await async_increment_counter(r, "cache_hits", platform)
            return {"status": "cached", "data": json.loads(cached)}
    else:
        cache_key = f"profile:{platform}:{username}:cursor:{cursor}"
        cached = await r.get(cache_key)
        if cached:
            await async_increment_counter(r, "cache_hits", platform)
            return {"status": "cached", "data": json.loads(cached)}

    # ── Dispatch ───────────────────────────────────────────────
    try:
        job_id = await dispatch_job(r, username, platform, cursor=cursor)
    except DuplicateJobError as e:
        existing_id = str(e).split(": ")[-1]
        return {"status": "processing", "job_id": existing_id}
    except QueueFullError:
        raise HTTPException(
            status_code=503, detail="System busy — try again later"
        )
    except CircuitOpenError:
        raise HTTPException(
            status_code=503,
            detail=f"Platform {platform} temporarily unavailable — "
            f"too many recent failures",
        )

    return {"status": "queued", "job_id": job_id}


# ── Job status polling ────────────────────────────────────────────


@app.get("/job/{job_id}")
async def get_job_status(job_id: str):
    """Poll job status and retrieve result when complete."""
    r = await get_async_redis()
    jsm = AsyncJobStateManager(r)
    state = await jsm.get_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job_id,
        "status": state.get("status"),
        "data": state.get("result"),
        "error": state.get("error") or None,
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
    }


# ── Metrics ────────────────────────────────────────────────────────


@app.get("/metrics")
async def metrics():
    """Observability endpoint — per-platform stats, circuit state, browser usage."""
    r = await get_async_redis()
    pc = get_platform_config()

    data = {}
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