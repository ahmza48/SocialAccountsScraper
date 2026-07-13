"""Async job dispatcher with deduplication, backpressure, and circuit breaker.

Called from the FastAPI API layer. Uses async Redis for all checks
and ``asyncio.to_thread`` for the blocking RQ enqueue operation.
"""
import asyncio
import threading
import uuid
from typing import Optional

import redis
import redis.asyncio as aioredis
from rq import Queue

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import QueueFullError, DuplicateJobError, CircuitOpenError
from core.job_state import AsyncJobStateManager
from core.platform_config import get_platform_config
from utils.circuit_breaker import AsyncCircuitBreaker
from utils.metrics import async_increment_counter

logger = get_logger(__name__)

# ── Sync RQ plumbing (reused across to_thread calls) ──────────────
#
# ``asyncio.to_thread`` dispatches each call to an arbitrary worker in the
# default thread pool, so the connection and per-queue ``Queue`` instances
# below are shared across threads. ``redis-py``'s client is internally
# thread-safe (its connection pool checks connections out per call), but the
# module-level dict mutation and one-time initialization are not — we guard
# them with a lock to avoid races on first-time queue creation.

_rq_conn: Optional[redis.Redis] = None
_rq_queues: dict = {}
_rq_lock = threading.Lock()


def _get_rq_conn() -> redis.Redis:
    global _rq_conn
    if _rq_conn is None:
        with _rq_lock:
            if _rq_conn is None:
                from core.redis import get_rq_connection
                _rq_conn = get_rq_connection()
    return _rq_conn


def _get_rq_queue(queue_name: str) -> Queue:
    queue = _rq_queues.get(queue_name)
    if queue is not None:
        return queue
    with _rq_lock:
        queue = _rq_queues.get(queue_name)
        if queue is None:
            conn = _get_rq_conn()
            queue = Queue(
                queue_name,
                connection=conn,
                default_timeout=Config.JOB_TIMEOUT_SECONDS,
            )
            _rq_queues[queue_name] = queue
    return queue


def _sync_enqueue(
    queue_name: str,
    job_id: str,
    username: str,
    platform: str,
    cursor: Optional[str],
) -> None:
    """Sync RQ enqueue — called from async via asyncio.to_thread."""
    queue = _get_rq_queue(queue_name)
    queue.enqueue(
        "workers.executor.execute_scrape_job",
        job_id=job_id,
        kwargs={
            "job_id": job_id,
            "username": username,
            "platform": platform,
            "cursor": cursor,
        },
        job_timeout=Config.JOB_TIMEOUT_SECONDS,
        result_ttl=Config.JOB_RESULT_TTL_SECONDS,
        failure_ttl=86400,
    )


# ── Public async API ──────────────────────────────────────────────


async def dispatch_job(
    ar: aioredis.Redis,
    username: str,
    platform: str,
    cursor: Optional[str] = None,
) -> str:
    """Dispatch a scraping job with dedup, backpressure, and circuit breaker.

    Args:
        ar: Async Redis connection (from the API layer).
        username: Target social media username.
        platform: Platform identifier (instagram, tiktok, facebook).
        cursor: Optional pagination cursor.

    Returns:
        The new job_id (UUID string).

    Raises:
        CircuitOpenError: Platform is temporarily unavailable.
        DuplicateJobError: An active job already exists for this target.
        QueueFullError: Queue has exceeded the backpressure threshold.
    """
    pc = get_platform_config()
    queue_name = pc.queue_name(platform)

    # ── Circuit breaker ────────────────────────────────────────
    cb = AsyncCircuitBreaker(ar)
    if not await cb.is_available(platform):
        raise CircuitOpenError(f"Platform {platform} temporarily unavailable")

    # ── Deduplication (atomic claim) ──────────────────────────
    jsm = AsyncJobStateManager(ar)
    job_id = str(uuid.uuid4())
    existing = await jsm.acquire_dedup(platform, username, job_id)
    if existing:
        logger.info(
            f"Duplicate request — active job {existing} for {username}@{platform}"
        )
        raise DuplicateJobError(f"Active job already exists: {existing}")

    # ── Backpressure ───────────────────────────────────────────
    queue_len = await ar.llen(f"rq:queue:{queue_name}")
    if queue_len >= Config.QUEUE_MAX_LENGTH:
        # Release the dedup slot we just claimed so the next caller can
        # re-attempt; otherwise a one-time backpressure event would block
        # this (platform, username) for JOB_DEDUP_TTL_SECONDS.
        await ar.delete(f"job:active:{platform}:{username}")
        await async_increment_counter(ar, "queue_rejected", platform)
        raise QueueFullError(
            f"Queue {queue_name} at capacity ({queue_len})"
        )

    # ── Create job ─────────────────────────────────────────────
    await jsm.create_pending(job_id, username, platform)

    # ── Enqueue via RQ (blocking call in thread) ───────────────
    # If RQ enqueue fails, roll back the dedup slot and the pending job
    # state so the caller can retry immediately. Otherwise a transient
    # Redis blip during enqueue would lock this (platform, username) out
    # for JOB_DEDUP_TTL_SECONDS without an actual job in flight.
    try:
        await asyncio.to_thread(
            _sync_enqueue, queue_name, job_id, username, platform, cursor
        )
    except Exception:
        logger.exception(
            "RQ enqueue failed for job %s; rolling back dedup + job state",
            job_id,
        )
        rollback = ar.pipeline(transaction=True)
        rollback.delete(f"job:active:{platform}:{username}")
        rollback.delete(f"job:{job_id}")
        await rollback.execute()
        raise

    await async_increment_counter(ar, "jobs_dispatched", platform)
    logger.info(
        f"Dispatched job {job_id} → {queue_name} (queue: {queue_len + 1})",
        extra={"job_id": job_id, "platform": platform, "username": username},
    )
    return job_id