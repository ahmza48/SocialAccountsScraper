import json
from typing import Optional

import redis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)


class CacheManager:
    """Redis cache with TTL, stampede protection, and cursor-based pagination support.

    Used by workers (sync). The API layer does cache reads directly via async Redis.
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            from core.redis import get_sync_redis
            self._redis = get_sync_redis()

    def _profile_key(self, platform: str, username: str) -> str:
        return f"profile:{platform}:{username}"

    def _cursor_key(self, platform: str, username: str, cursor: str) -> str:
        return f"profile:{platform}:{username}:cursor:{cursor}"

    def _lock_key(self, key: str) -> str:
        return f"cache_lock:{key}"

    # ── Profile cache ──────────────────────────────────────────────

    def get_profile(self, platform: str, username: str) -> Optional[dict]:
        """Get cached profile data. Returns None on miss."""
        key = self._profile_key(platform, username)
        data = self._redis.get(key)
        if data:
            logger.info(f"Cache HIT: {key}")
            return json.loads(data)
        return None

    def set_profile(self, platform: str, username: str, data: dict, ttl: int = None):
        """Cache profile data with configurable TTL."""
        key = self._profile_key(platform, username)
        ttl = ttl or Config.CACHE_TTL_SECONDS
        self._redis.setex(key, ttl, json.dumps(data))
        logger.info(f"Cache SET: {key} (TTL={ttl}s)")

    # ── Cursor-based pagination cache ──────────────────────────────

    def get_page(self, platform: str, username: str, cursor: str) -> Optional[dict]:
        """Get cached paginated data by cursor."""
        key = self._cursor_key(platform, username, cursor)
        data = self._redis.get(key)
        if data:
            return json.loads(data)
        return None

    def set_page(
        self, platform: str, username: str, cursor: str, data: dict, ttl: int = None
    ):
        """Cache a page of paginated data."""
        key = self._cursor_key(platform, username, cursor)
        ttl = ttl or Config.CACHE_TTL_SECONDS
        self._redis.setex(key, ttl, json.dumps(data))

    # ── Stampede protection ────────────────────────────────────────

    def acquire_populate_lock(
        self, platform: str, username: str, timeout: int = None
    ) -> bool:
        """Acquire lock to prevent cache stampede — only one worker populates cache."""
        timeout = timeout or Config.CACHE_LOCK_TIMEOUT
        lock_key = self._lock_key(self._profile_key(platform, username))
        acquired = self._redis.set(lock_key, "1", nx=True, ex=timeout)
        return bool(acquired)

    def release_populate_lock(self, platform: str, username: str):
        """Release cache populate lock."""
        lock_key = self._lock_key(self._profile_key(platform, username))
        self._redis.delete(lock_key)

    # ── Invalidation ───────────────────────────────────────────────

    def invalidate(self, platform: str, username: str):
        """Invalidate all cached data for a user on a platform."""
        pattern = f"profile:{platform}:{username}*"
        cursor_pos = 0
        while True:
            cursor_pos, keys = self._redis.scan(cursor_pos, match=pattern, count=100)
            if keys:
                self._redis.delete(*keys)
            if cursor_pos == 0:
                break
        logger.info(f"Cache INVALIDATED: {pattern}")
