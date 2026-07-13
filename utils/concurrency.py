"""Browser concurrency control using Redis atomic counters.

Enforces both a global browser limit and per-platform limits
to prevent resource starvation. Uses Lua scripts for atomicity.
All counters have TTLs as a safety net against process crashes.
"""
import time

import redis

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import BrowserLimitError

logger = get_logger(__name__)

GLOBAL_BROWSER_KEY = "active:browsers"
PLATFORM_BROWSER_KEY = "active:browsers:{platform}"

# TTL for counter keys — safety net against leaked counters from crashed workers.
# Tied to the job timeout (plus a graceful-shutdown buffer) so a slot can never
# be held longer than the job that holds it could legitimately run.
_COUNTER_TTL_BUFFER_SECONDS = 60
_COUNTER_TTL = max(Config.JOB_TIMEOUT_SECONDS + _COUNTER_TTL_BUFFER_SECONDS, 120)

# Lua: atomic check-and-increment with global + platform limits
_ACQUIRE_LUA = """
local global_key    = KEYS[1]
local platform_key  = KEYS[2]
local max_global    = tonumber(ARGV[1])
local max_platform  = tonumber(ARGV[2])
local ttl           = tonumber(ARGV[3])

local g = tonumber(redis.call('GET', global_key) or '0')
local p = tonumber(redis.call('GET', platform_key) or '0')

if g >= max_global then return -1 end
if p >= max_platform then return -2 end

redis.call('INCR', global_key)
redis.call('EXPIRE', global_key, ttl)
redis.call('INCR', platform_key)
redis.call('EXPIRE', platform_key, ttl)
return 1
"""

# Lua: atomic decrement (floor at zero)
_RELEASE_LUA = """
local global_key   = KEYS[1]
local platform_key = KEYS[2]

local g = tonumber(redis.call('GET', global_key) or '0')
if g > 0 then redis.call('DECR', global_key) end

local p = tonumber(redis.call('GET', platform_key) or '0')
if p > 0 then redis.call('DECR', platform_key) end
return 1
"""


class BrowserConcurrencyGuard:
    """Context manager that enforces global + per-platform browser limits.

    Usage::

        guard = BrowserConcurrencyGuard(redis_client, "instagram", max_platform=3)
        with guard:
            # browser is running — slot reserved
            scraper.execute(...)
        # slot released automatically
    """

    def __init__(
        self,
        sync_redis: redis.Redis,
        platform: str,
        max_platform: int,
    ) -> None:
        self._redis = sync_redis
        self._platform = platform
        self._max_global = Config.MAX_CONCURRENT_BROWSERS
        self._max_platform = max_platform
        self._acquired = False

    @property
    def _platform_key(self) -> str:
        return PLATFORM_BROWSER_KEY.format(platform=self._platform)

    def acquire(self, max_wait_seconds: int = 60, poll_interval: int = 3) -> None:
        """Acquire a browser slot, polling until available or timeout."""
        deadline = time.monotonic() + max_wait_seconds
        while True:
            result = self._redis.eval(
                _ACQUIRE_LUA, 2,
                GLOBAL_BROWSER_KEY, self._platform_key,
                self._max_global, self._max_platform, _COUNTER_TTL,
            )
            if result == 1:
                self._acquired = True
                logger.info(f"Browser slot acquired for {self._platform}")
                return

            if time.monotonic() >= deadline:
                if result == -1:
                    raise BrowserLimitError(
                        f"Global browser limit ({self._max_global}) reached — "
                        f"timed out after {max_wait_seconds}s"
                    )
                raise BrowserLimitError(
                    f"Platform {self._platform} browser limit ({self._max_platform}) "
                    f"reached — timed out after {max_wait_seconds}s"
                )
            time.sleep(poll_interval)

    def release(self) -> None:
        """Release the browser slot (safe to call multiple times)."""
        if not self._acquired:
            return
        self._redis.eval(
            _RELEASE_LUA, 2,
            GLOBAL_BROWSER_KEY, self._platform_key,
        )
        self._acquired = False
        logger.info(f"Browser slot released for {self._platform}")

    def __enter__(self) -> "BrowserConcurrencyGuard":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
