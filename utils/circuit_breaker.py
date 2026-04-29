"""Redis-backed circuit breaker per platform.

States:
    CLOSED   — normal operation, requests pass through
    OPEN     — failures exceeded threshold, requests rejected
    HALF_OPEN — recovery period elapsed, one probe request allowed

Failure tracking uses a Redis ZSET with timestamps so stale
failures naturally leave the window.
"""
import time

import redis
import redis.asyncio as aioredis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    """Sync circuit breaker for the worker layer."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, sync_redis: redis.Redis) -> None:
        self._redis = sync_redis

    @staticmethod
    def _state_key(platform: str) -> str:
        return f"circuit:{platform}:state"

    @staticmethod
    def _failures_key(platform: str) -> str:
        return f"circuit:{platform}:failures"

    @staticmethod
    def _opened_at_key(platform: str) -> str:
        return f"circuit:{platform}:opened_at"

    def record_failure(self, platform: str) -> None:
        now = time.time()
        fk = self._failures_key(platform)
        window = Config.CIRCUIT_BREAKER_WINDOW_SECONDS

        pipe = self._redis.pipeline(True)
        pipe.zadd(fk, {str(now): now})
        pipe.zremrangebyscore(fk, 0, now - window)
        pipe.zcard(fk)
        pipe.expire(fk, window * 2)
        results = pipe.execute()
        count = results[2]

        if count >= Config.CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            self._open(platform)

    def record_success(self, platform: str) -> None:
        state = self._redis.get(self._state_key(platform))
        if state in (self.OPEN, self.HALF_OPEN):
            self._close(platform)

    def is_available(self, platform: str) -> bool:
        state = self._redis.get(self._state_key(platform))
        if state is None or state == self.CLOSED:
            return True
        if state == self.OPEN:
            opened_at = self._redis.get(self._opened_at_key(platform))
            if opened_at and (time.time() - float(opened_at)) > Config.CIRCUIT_BREAKER_RECOVERY_SECONDS:
                self._redis.set(self._state_key(platform), self.HALF_OPEN)
                return True
            return False
        # HALF_OPEN — allow one probe
        return True

    def _open(self, platform: str) -> None:
        pipe = self._redis.pipeline(True)
        pipe.set(self._state_key(platform), self.OPEN)
        pipe.set(self._opened_at_key(platform), str(time.time()))
        pipe.execute()
        logger.warning(f"Circuit OPENED for {platform}")

    def _close(self, platform: str) -> None:
        pipe = self._redis.pipeline(True)
        pipe.set(self._state_key(platform), self.CLOSED)
        pipe.delete(self._failures_key(platform))
        pipe.delete(self._opened_at_key(platform))
        pipe.execute()
        logger.info(f"Circuit CLOSED for {platform}")


class AsyncCircuitBreaker:
    """Async circuit breaker for the API layer (read-only checks)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, async_redis: aioredis.Redis) -> None:
        self._redis = async_redis

    @staticmethod
    def _state_key(platform: str) -> str:
        return f"circuit:{platform}:state"

    @staticmethod
    def _opened_at_key(platform: str) -> str:
        return f"circuit:{platform}:opened_at"

    async def is_available(self, platform: str) -> bool:
        state = await self._redis.get(self._state_key(platform))
        if state is None or state == self.CLOSED:
            return True
        if state == self.OPEN:
            opened_at = await self._redis.get(self._opened_at_key(platform))
            if opened_at and (time.time() - float(opened_at)) > Config.CIRCUIT_BREAKER_RECOVERY_SECONDS:
                await self._redis.set(self._state_key(platform), self.HALF_OPEN)
                return True
            return False
        return True
