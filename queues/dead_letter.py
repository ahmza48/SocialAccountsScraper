import json
import time
from typing import Optional

import redis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)

DLQ_KEY = "dlq:failed_jobs"


class DeadLetterQueue:
    """Dead-letter queue for permanently failed jobs."""

    def __init__(self, redis_client: Optional[redis.Redis] = None) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            from core.redis import get_sync_redis
            self._redis = get_sync_redis()

    def push(
        self,
        job_id: str,
        platform: str,
        username: str,
        error: str,
        attempts: int,
    ):
        """Push a failed job to the dead-letter queue."""
        entry = json.dumps({
            "job_id": job_id,
            "platform": platform,
            "username": username,
            "error": error,
            "attempts": attempts,
            "failed_at": time.time(),
        })
        self._redis.lpush(f"{DLQ_KEY}:{platform}", entry)
        self._redis.lpush(DLQ_KEY, entry)
        # Keep DLQ bounded
        self._redis.ltrim(DLQ_KEY, 0, 9999)
        self._redis.ltrim(f"{DLQ_KEY}:{platform}", 0, 9999)

        logger.error(
            f"Job {job_id} moved to DLQ after {attempts} attempts: {error}",
            extra={"job_id": job_id, "platform": platform},
        )

    def list_failed(
        self, platform: str = None, start: int = 0, count: int = 50
    ) -> list:
        """List failed jobs from the DLQ."""
        key = f"{DLQ_KEY}:{platform}" if platform else DLQ_KEY
        raw = self._redis.lrange(key, start, start + count - 1)
        return [json.loads(r) for r in raw]

    def length(self, platform: str = None) -> int:
        """Get number of jobs in the DLQ."""
        key = f"{DLQ_KEY}:{platform}" if platform else DLQ_KEY
        return self._redis.llen(key)
