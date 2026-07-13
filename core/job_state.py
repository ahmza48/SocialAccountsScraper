"""Job state management with full lifecycle tracking.

Provides sync (workers) and async (API) managers for
PENDING → PROCESSING → COMPLETED/FAILED state machine.

Redis storage: HASH at ``job:{job_id}`` with TTL.
Dedup key: ``job:active:{platform}:{username}`` with TTL.
"""
import json
import time
from typing import Optional

import redis
import redis.asyncio as aioredis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)

# ── State constants ────────────────────────────────────────────────

PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _dedup_key(platform: str, username: str) -> str:
    return f"job:active:{platform}:{username}"


# ── Sync (workers) ────────────────────────────────────────────────


class JobStateManager:
    """Sync job state manager used by RQ workers."""

    def __init__(self, r: redis.Redis) -> None:
        self._redis = r

    def set_processing(self, job_id: str, username: str, platform: str) -> None:
        key = _job_key(job_id)
        now = str(time.time())
        created = self._redis.hget(key, "created_at") or now
        self._redis.hset(key, mapping={
            "status": PROCESSING,
            "username": username,
            "platform": platform,
            "result": "",
            "error": "",
            "created_at": created,
            "updated_at": now,
        })
        self._redis.expire(key, Config.JOB_RESULT_TTL_SECONDS)
        logger.info(
            f"Job {job_id} → PROCESSING",
            extra={"job_id": job_id, "platform": platform, "username": username},
        )

    def set_completed(self, job_id: str, result: dict) -> None:
        key = _job_key(job_id)
        self._redis.hset(key, mapping={
            "status": COMPLETED,
            "result": json.dumps(result),
            "error": "",
            "updated_at": str(time.time()),
        })
        self._redis.expire(key, Config.JOB_RESULT_TTL_SECONDS)

    def set_failed(self, job_id: str, error: str) -> None:
        key = _job_key(job_id)
        self._redis.hset(key, mapping={
            "status": FAILED,
            "error": error,
            "updated_at": str(time.time()),
        })
        self._redis.expire(key, Config.JOB_RESULT_TTL_SECONDS)

    def clear_dedup(self, platform: str, username: str) -> None:
        self._redis.delete(_dedup_key(platform, username))

    def refresh_dedup(self, platform: str, username: str) -> None:
        """Extend the dedup key's TTL to the full ``JOB_DEDUP_TTL_SECONDS``.

        A job that hits retries (backoff + multiple attempts) can run long
        enough that the dedup key set at dispatch time expires while the job
        is still in flight, letting a second identical request slip past the
        dedup guard and dispatch a duplicate job for the same target. The
        executor calls this before each retry so the guard lasts as long as
        the job is actually running, not just its original TTL window.
        EXPIRE on an already-gone key is a harmless no-op.
        """
        self._redis.expire(_dedup_key(platform, username), Config.JOB_DEDUP_TTL_SECONDS)


# ── Async (API) ───────────────────────────────────────────────────


class AsyncJobStateManager:
    """Async job state manager used by the FastAPI layer."""

    def __init__(self, r: aioredis.Redis) -> None:
        self._redis = r

    async def create_pending(
        self, job_id: str, username: str, platform: str
    ) -> None:
        key = _job_key(job_id)
        now = str(time.time())
        await self._redis.hset(key, mapping={
            "status": PENDING,
            "username": username,
            "platform": platform,
            "result": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
        })
        await self._redis.expire(key, Config.JOB_RESULT_TTL_SECONDS)

    async def get_state(self, job_id: str) -> Optional[dict]:
        key = _job_key(job_id)
        data = await self._redis.hgetall(key)
        if not data:
            return None
        if data.get("result"):
            try:
                data["result"] = json.loads(data["result"])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Corrupt result JSON for job %s; returning result=None",
                    job_id,
                    extra={"job_id": job_id},
                )
                data["result"] = None
        else:
            data["result"] = None
        return data

    async def set_dedup(
        self, platform: str, username: str, job_id: str
    ) -> None:
        key = _dedup_key(platform, username)
        await self._redis.setex(key, Config.JOB_DEDUP_TTL_SECONDS, job_id)

    async def acquire_dedup(
        self, platform: str, username: str, job_id: str
    ) -> Optional[str]:
        """Atomically claim the dedup slot for ``(platform, username)``.

        Returns ``None`` when the caller successfully claimed the slot, or the
        existing job id when another in-flight request already holds it. Uses
        ``SET NX EX`` so the get/set sequence is collapsed into a single
        Redis round-trip; the previous get_dedup-then-set_dedup pattern had
        a TOCTOU window that allowed two concurrent /scrape requests for
        the same target to enqueue duplicate jobs.
        """
        key = _dedup_key(platform, username)
        # ``set(... nx=True, ex=...)`` returns True on success and None when
        # the key already exists; in the latter case we read the current
        # holder so the caller can return the existing job id to the client.
        acquired = await self._redis.set(
            key, job_id, nx=True, ex=Config.JOB_DEDUP_TTL_SECONDS
        )
        if acquired:
            return None
        existing = await self._redis.get(key)
        # An extremely tight TTL race could expire the key between the SET
        # and the GET. Treat that as success (no concurrent owner) so we
        # don't drop a legitimate dispatch on the floor.
        return existing if existing else None

    async def get_dedup(
        self, platform: str, username: str
    ) -> Optional[str]:
        return await self._redis.get(_dedup_key(platform, username))
