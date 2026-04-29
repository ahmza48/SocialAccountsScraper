import json
import random
import time
from contextlib import contextmanager
from typing import Optional

import redis

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import AccountUnavailableError

logger = get_logger(__name__)


class AccountPoolManager:
    """Manages account allocation with distributed locking, cooldown, and IP binding.

    Redis data model per account:
        HASH  account:{platform}:{account_id}  → metadata + status
        KEY   account_lock:{platform}:{account_id}  → SETNX lock
        KEY   account_cooldown:{platform}:{account_id}  → TTL-based cooldown
        SET   accounts:{platform}  → set of all account IDs
    """

    ACCOUNT_KEY = "account:"
    LOCK_KEY = "account_lock:"
    COOLDOWN_KEY = "account_cooldown:"

    STATUS_IDLE = "idle"
    STATUS_IN_USE = "in_use"
    STATUS_COOLDOWN = "cooldown"
    STATUS_INVALID = "invalid"

    def __init__(self, redis_client: Optional[redis.Redis] = None) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            from core.redis import get_sync_redis
            self._redis = get_sync_redis()

    # ── Registration ───────────────────────────────────────────────

    def register_account(
        self,
        account_id: str,
        platform: str,
        credentials: dict,
        proxy: str = None,
    ):
        """Register an account in the pool."""
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        account_data = {
            "account_id": account_id,
            "platform": platform,
            "credentials": json.dumps(credentials),
            "proxy": proxy or "",
            "status": self.STATUS_IDLE,
            "last_used": "0",
            "use_count": "0",
            "registered_at": str(time.time()),
        }
        self._redis.hset(key, mapping=account_data)
        self._redis.sadd(f"accounts:{platform}", account_id)
        logger.info(f"Registered account {account_id} for {platform}")

    # ── Acquisition (distributed lock) ─────────────────────────────

    def acquire_account(self, platform: str, job_id: str = None) -> dict:
        """Acquire a free account with SETNX-based distributed lock.

        Selection is randomized and avoids accounts that are in cooldown,
        in use, or marked invalid.

        Returns the full account dict (credentials deserialized).
        Raises AccountUnavailableError when nothing is available.
        """
        account_ids = list(self._redis.smembers(f"accounts:{platform}"))
        if not account_ids:
            raise AccountUnavailableError(f"No accounts registered for {platform}")

        random.shuffle(account_ids)

        for account_id in account_ids:
            key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
            lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"
            cooldown_key = f"{self.COOLDOWN_KEY}{platform}:{account_id}"

            # Skip invalid accounts
            account = self._redis.hgetall(key)
            if not account or account.get("status") == self.STATUS_INVALID:
                continue

            # Skip accounts in cooldown
            if self._redis.exists(cooldown_key):
                continue

            # Try to acquire lock (atomic SETNX)
            lock_ttl = Config.JOB_TIMEOUT_SECONDS + 30
            acquired = self._redis.set(
                lock_key, job_id or "1", nx=True, ex=lock_ttl
            )
            if not acquired:
                continue

            # Mark as in-use
            self._redis.hset(key, mapping={
                "status": self.STATUS_IN_USE,
                "last_used": str(time.time()),
            })
            self._redis.hincrby(key, "use_count", 1)

            account = self._redis.hgetall(key)
            account["credentials"] = json.loads(account["credentials"])

            logger.info(
                f"Acquired account {account_id} for {platform}",
                extra={"account_id": account_id, "platform": platform},
            )
            return account

        raise AccountUnavailableError(
            f"No available accounts for {platform} — all locked, in cooldown, or invalid"
        )

    # ── Release ────────────────────────────────────────────────────

    def release_account(
        self, account_id: str, platform: str, apply_cooldown: bool = True
    ):
        """Release an account back to the pool."""
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"
        cooldown_key = f"{self.COOLDOWN_KEY}{platform}:{account_id}"

        self._redis.delete(lock_key)

        if apply_cooldown:
            self._redis.setex(cooldown_key, Config.ACCOUNT_COOLDOWN_SECONDS, "1")
            self._redis.hset(key, "status", self.STATUS_COOLDOWN)
        else:
            self._redis.hset(key, "status", self.STATUS_IDLE)

        logger.info(f"Released account {account_id} for {platform}")

    # ── Invalidation ───────────────────────────────────────────────

    def mark_invalid(self, account_id: str, platform: str, reason: str = ""):
        """Permanently mark an account as blocked/banned."""
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"

        self._redis.hset(key, mapping={
            "status": self.STATUS_INVALID,
            "invalid_reason": reason,
        })
        self._redis.delete(lock_key)
        logger.warning(
            f"Account {account_id} marked INVALID for {platform}: {reason}"
        )

    # ── Monitoring ─────────────────────────────────────────────────

    def get_pool_status(self, platform: str) -> dict:
        """Get current pool status for observability."""
        account_ids = self._redis.smembers(f"accounts:{platform}")
        status = {
            "total": len(account_ids),
            "idle": 0,
            "in_use": 0,
            "cooldown": 0,
            "invalid": 0,
        }
        for account_id in account_ids:
            key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
            s = self._redis.hget(key, "status") or "unknown"
            if s in status:
                status[s] += 1
        return status

    # ── Context manager ────────────────────────────────────────────

    @contextmanager
    def use_account(self, platform: str, job_id: str = None):
        """Context manager for safe account acquire/release."""
        account = self.acquire_account(platform, job_id)
        try:
            yield account
        finally:
            self.release_account(account["account_id"], platform)