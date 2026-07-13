import json
import random
import time
from contextlib import contextmanager
from typing import Optional

import redis

from core.config import Config
from core.crypto import CredentialCipherError, get_credential_cipher
from core.logging_config import get_logger
from core.exceptions import AccountUnavailableError

logger = get_logger(__name__)


# Lua: atomically validate account state and acquire the per-account lock.
#
# KEYS[1] = account hash key            (account:{platform}:{account_id})
# KEYS[2] = account lock key            (account_lock:{platform}:{account_id})
# KEYS[3] = account cooldown key        (account_cooldown:{platform}:{account_id})
# ARGV[1] = lock owner (job_id)
# ARGV[2] = lock TTL seconds
# ARGV[3] = current unix timestamp (string)
# ARGV[4] = STATUS_INVALID sentinel
# ARGV[5] = STATUS_IN_USE sentinel
#
# Return codes:
#    1  acquired
#   -1  account hash missing
#   -2  account marked invalid
#   -3  account in cooldown
#   -4  account already locked by another job
_ACQUIRE_ACCOUNT_LUA = """
local account_key  = KEYS[1]
local lock_key     = KEYS[2]
local cooldown_key = KEYS[3]
local owner        = ARGV[1]
local ttl          = tonumber(ARGV[2])
local now          = ARGV[3]
local status_invalid = ARGV[4]
local status_in_use  = ARGV[5]

if redis.call('EXISTS', account_key) == 0 then
    return -1
end

local status = redis.call('HGET', account_key, 'status')
if status == status_invalid then
    return -2
end

if redis.call('EXISTS', cooldown_key) == 1 then
    return -3
end

local locked = redis.call('SET', lock_key, owner, 'NX', 'EX', ttl)
if not locked then
    return -4
end

redis.call('HSET', account_key,
    'status', status_in_use,
    'last_used', now)
redis.call('HINCRBY', account_key, 'use_count', 1)
return 1
"""


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
        # Register the Lua script once per manager so subsequent calls reuse the SHA.
        self._acquire_script = self._redis.register_script(_ACQUIRE_ACCOUNT_LUA)

    # ── Registration ───────────────────────────────────────────────

    def register_account(
        self,
        account_id: str,
        platform: str,
        credentials: dict,
        proxy: str = None,
        overwrite: bool = False,
    ):
        """Register an account in the pool.

        Credentials are encrypted at rest with the configured Fernet key
        (see :mod:`core.crypto`); the only place plaintext lives is in
        memory inside the worker process while a job is running.

        Refuses to overwrite an existing account hash unless ``overwrite=True``
        is passed explicitly. The previous behaviour silently reset
        ``use_count``/``cooldown``/``status`` on every re-register, which
        meant accidentally re-running an onboarding script could un-cooldown
        an account that was supposed to be resting.
        """
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        if not overwrite and self._redis.exists(key):
            raise ValueError(
                f"Account {account_id!r} already registered for {platform}; "
                f"pass overwrite=True to replace it"
            )
        encrypted_credentials = get_credential_cipher().encrypt(credentials)
        account_data = {
            "account_id": account_id,
            "platform": platform,
            "credentials": encrypted_credentials,
            "proxy": proxy or "",
            "status": self.STATUS_IDLE,
            "last_used": "0",
            "use_count": "0",
            "registered_at": str(time.time()),
        }
        # Single transactional pipeline so a crash between HSET and SADD
        # cannot leave the account hash present without its pool-set entry.
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(key, mapping=account_data)
        pipe.sadd(f"accounts:{platform}", account_id)
        pipe.execute()
        logger.info(f"Registered account {account_id} for {platform}")

    # ── Acquisition (distributed lock) ─────────────────────────────

    def acquire_account(self, platform: str, job_id: str = None) -> dict:
        """Acquire a free account using an atomic check-and-lock Lua script.

        Selection is randomized over the registered pool. For each candidate the
        script atomically verifies the account is not invalid, not in cooldown,
        and not already locked, then sets the lock and flips status to
        ``in_use`` in a single Redis round-trip \u2014 closing the TOCTOU window
        between status/cooldown checks and lock acquisition.

        Returns the full account dict (credentials deserialized).
        Raises :class:`AccountUnavailableError` when no account can be acquired.
        """
        account_ids = list(self._redis.smembers(f"accounts:{platform}"))
        if not account_ids:
            raise AccountUnavailableError(f"No accounts registered for {platform}")

        random.shuffle(account_ids)

        owner = job_id or "1"
        lock_ttl = Config.JOB_TIMEOUT_SECONDS + 30
        now = str(time.time())

        # Track stale set entries to clean up after the loop (avoid mutating the
        # set while iterating, and keep the hot path lock-free).
        stale_account_ids: list = []

        for account_id in account_ids:
            account_key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
            lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"
            cooldown_key = f"{self.COOLDOWN_KEY}{platform}:{account_id}"

            result = self._acquire_script(
                keys=[account_key, lock_key, cooldown_key],
                args=[
                    owner,
                    lock_ttl,
                    now,
                    self.STATUS_INVALID,
                    self.STATUS_IN_USE,
                ],
            )

            try:
                code = int(result)
            except (TypeError, ValueError):
                code = -99

            if code == 1:
                # Lock acquired atomically with status flip; safe to read state now.
                if stale_account_ids:
                    self._cleanup_stale(platform, stale_account_ids)

                account = self._redis.hgetall(account_key)
                try:
                    account["credentials"] = self._decode_credentials(
                        account.get("credentials", "")
                    )
                except CredentialCipherError as exc:
                    # Corrupt or unrecoverable record — release lock and skip.
                    self._redis.delete(lock_key)
                    logger.error(
                        f"Account {account_id} for {platform} has unreadable "
                        f"credentials ({exc}); marking invalid"
                    )
                    self.mark_invalid(account_id, platform, "credential_cipher_error")
                    continue

                logger.info(
                    f"Acquired account {account_id} for {platform}",
                    extra={"account_id": account_id, "platform": platform},
                )
                return account

            if code == -1:
                # Account hash gone but ID still in pool set \u2014 schedule cleanup.
                stale_account_ids.append(account_id)
            elif code in (-2, -3, -4):
                # Invalid / cooldown / locked \u2014 try the next candidate.
                continue
            else:
                logger.warning(
                    f"Unexpected acquire script result {result!r} for "
                    f"{account_id}@{platform}; skipping"
                )

        if stale_account_ids:
            self._cleanup_stale(platform, stale_account_ids)

        raise AccountUnavailableError(
            f"No available accounts for {platform} \u2014 all locked, in cooldown, or invalid"
        )

    def _cleanup_stale(self, platform: str, account_ids: list) -> None:
        """Remove pool-set entries whose backing hash has been deleted."""
        try:
            self._redis.srem(f"accounts:{platform}", *account_ids)
            logger.info(
                f"Pruned {len(account_ids)} stale account id(s) from {platform} pool set"
            )
        except Exception:  # pragma: no cover \u2014 best-effort cleanup
            logger.warning(
                f"Failed to prune stale account ids from {platform} pool set",
                exc_info=True,
            )
    @staticmethod
    def _decode_credentials(blob: str) -> dict:
        """Decode a credentials field from the account hash.

        Only encrypted blobs (carrying the ``ENC:v1:`` prefix from
        :mod:`core.crypto`) are accepted. Any value missing the prefix is
        treated as unrecoverable: this codebase has never deployed with
        plaintext credentials, so a missing prefix indicates corruption
        rather than a legacy record. ``register_account`` is the only
        sanctioned write path and always encrypts.
        """
        cipher = get_credential_cipher()
        if not cipher.is_encrypted(blob):
            raise CredentialCipherError(
                "credentials field is not encrypted; refusing to load"
            )
        return cipher.decrypt(blob)
    # ── Release ────────────────────────────────────────────────────

    def release_account(
        self, account_id: str, platform: str, apply_cooldown: bool = True
    ):
        """Release an account back to the pool.

        Atomic via a single transactional pipeline so a worker crash mid-
        release can never leave the lock dropped without the cooldown set
        (which would let another worker pick up an account that was meant
        to be resting).
        """
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"
        cooldown_key = f"{self.COOLDOWN_KEY}{platform}:{account_id}"

        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(lock_key)
        if apply_cooldown:
            pipe.setex(cooldown_key, Config.ACCOUNT_COOLDOWN_SECONDS, "1")
            pipe.hset(key, "status", self.STATUS_COOLDOWN)
        else:
            pipe.hset(key, "status", self.STATUS_IDLE)
        pipe.execute()

        logger.info(f"Released account {account_id} for {platform}")

    # ── Invalidation ───────────────────────────────────────────────

    def mark_invalid(self, account_id: str, platform: str, reason: str = ""):
        """Permanently mark an account as blocked/banned.

        Removes the account from the active pool set so future ``acquire_account``
        calls don't waste a Redis round-trip on it, while preserving the hash
        record (and ``invalid_reason``) for forensic inspection.
        """
        key = f"{self.ACCOUNT_KEY}{platform}:{account_id}"
        lock_key = f"{self.LOCK_KEY}{platform}:{account_id}"

        pipe = self._redis.pipeline()
        pipe.hset(key, mapping={
            "status": self.STATUS_INVALID,
            "invalid_reason": reason,
            "invalidated_at": str(time.time()),
        })
        pipe.delete(lock_key)
        pipe.srem(f"accounts:{platform}", account_id)
        pipe.execute()

        logger.warning(
            f"Account {account_id} marked INVALID for {platform}: {reason}",
            extra={"account_id": account_id, "platform": platform, "reason": reason},
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