import time
from typing import Optional

import redis
import redis.asyncio as aioredis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)

_redis: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        from core.redis import get_sync_redis
        _redis = get_sync_redis()
    return _redis


# ── Sync (workers) ────────────────────────────────────────────────


def increment_counter(metric: str, platform: str = None, amount: int = 1) -> None:
    """Increment a metric counter in Redis."""
    try:
        r = _get_redis()
        key = f"metrics:{platform}:{metric}" if platform else f"metrics:{metric}"
        r.incrby(key, amount)
    except Exception as e:
        logger.warning(f"Failed to record metric {metric}: {e}")


def record_timing(metric: str, duration_ms: float, platform: str = None) -> None:
    """Record a timing metric (keeps last 1000 entries)."""
    try:
        r = _get_redis()
        key = f"metrics:timing:{platform}:{metric}" if platform else f"metrics:timing:{metric}"
        pipe = r.pipeline(False)
        pipe.lpush(key, f"{duration_ms:.2f}")
        pipe.ltrim(key, 0, 999)
        pipe.execute()
    except Exception as e:
        logger.warning(f"Failed to record timing {metric}: {e}")


# ── Async (API) ───────────────────────────────────────────────────


async def async_increment_counter(
    r: aioredis.Redis, metric: str, platform: str = None, amount: int = 1
) -> None:
    """Async increment a metric counter in Redis."""
    try:
        key = f"metrics:{platform}:{metric}" if platform else f"metrics:{metric}"
        await r.incrby(key, amount)
    except Exception as e:
        logger.warning(f"Failed to record async metric {metric}: {e}")


# ── Shared ─────────────────────────────────────────────────────────


def get_queue_length(platform: str) -> int:
    """Get current queue length for a platform."""
    try:
        r = _get_redis()
        return r.llen(f"rq:queue:scrape_{platform}")
    except Exception:
        return -1


class TimingContext:
    """Context manager to measure and record execution time."""

    def __init__(self, metric: str, platform: str = None):
        self.metric = metric
        self.platform = platform
        self.start: Optional[float] = None

    def __enter__(self) -> "TimingContext":
        self.start = time.monotonic()
        return self

    def __exit__(self, *args: object) -> None:
        if self.start is not None:
            duration_ms = (time.monotonic() - self.start) * 1000
            record_timing(self.metric, duration_ms, self.platform)
