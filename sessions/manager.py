import json
import os
import time
from typing import Optional

import redis

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import SessionExpiredError, SessionInvalidError

logger = get_logger(__name__)


class SessionManager:
    """Manages Playwright browser sessions.

    Storage model:
        Disk  → full Playwright storage_state JSON (cookies, localStorage)
        Redis → lightweight metadata + status + locks
    """

    SESSION_META_PREFIX = "session:"

    STATUS_IDLE = "idle"
    STATUS_IN_USE = "in_use"
    STATUS_INVALID = "invalid"

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        storage_dir: Optional[str] = None,
    ) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            from core.redis import get_sync_redis
            self._redis = get_sync_redis()
        self._storage_dir = storage_dir or Config.SESSION_STORAGE_DIR
        os.makedirs(self._storage_dir, exist_ok=True)

    # ── Internal helpers ───────────────────────────────────────────

    def _meta_key(self, platform: str, account_id: str) -> str:
        return f"{self.SESSION_META_PREFIX}{platform}:{account_id}"

    def _storage_path(self, platform: str, account_id: str) -> str:
        platform_dir = os.path.join(self._storage_dir, platform)
        os.makedirs(platform_dir, exist_ok=True)
        return os.path.join(platform_dir, f"{account_id}.json")

    # ── Session lifecycle ──────────────────────────────────────────

    def has_session(self, platform: str, account_id: str) -> bool:
        """Check if a valid session file exists for this account."""
        meta_key = self._meta_key(platform, account_id)
        status = self._redis.hget(meta_key, "status")
        if status == self.STATUS_INVALID:
            return False
        storage_path = self._storage_path(platform, account_id)
        return os.path.exists(storage_path)

    def load_session(self, platform: str, account_id: str) -> Optional[dict]:
        """Load session storage state from disk."""
        storage_path = self._storage_path(platform, account_id)
        if not os.path.exists(storage_path):
            return None

        with open(storage_path, "r") as f:
            state = json.load(f)

        self._redis.hset(self._meta_key(platform, account_id), mapping={
            "status": self.STATUS_IN_USE,
            "last_loaded": str(time.time()),
        })
        logger.info(f"Loaded session for {account_id} on {platform}")
        return state

    def save_session(self, platform: str, account_id: str, storage_state: dict):
        """Persist Playwright storage_state to disk and update Redis metadata."""
        storage_path = self._storage_path(platform, account_id)

        with open(storage_path, "w") as f:
            json.dump(storage_state, f)

        # Restrict file permissions to owner only
        os.chmod(storage_path, 0o600)

        self._redis.hset(self._meta_key(platform, account_id), mapping={
            "status": self.STATUS_IDLE,
            "last_saved": str(time.time()),
        })
        logger.info(f"Saved session for {account_id} on {platform}")

    def mark_invalid(self, platform: str, account_id: str, reason: str = ""):
        """Mark session as invalid — triggers re-login on next use."""
        self._redis.hset(self._meta_key(platform, account_id), mapping={
            "status": self.STATUS_INVALID,
            "invalid_reason": reason,
            "invalidated_at": str(time.time()),
        })
        logger.warning(
            f"Session marked INVALID for {account_id} on {platform}: {reason}"
        )

    def delete_session(self, platform: str, account_id: str):
        """Delete session file and Redis metadata."""
        storage_path = self._storage_path(platform, account_id)
        if os.path.exists(storage_path):
            os.remove(storage_path)
        self._redis.delete(self._meta_key(platform, account_id))
        logger.info(f"Deleted session for {account_id} on {platform}")

    # ── Validation ─────────────────────────────────────────────────

    def validate_session_page(self, page, platform: str) -> bool:
        """Check if the current page indicates a valid session (no login redirect)."""
        login_indicators = {
            "instagram": ["/accounts/login", "/challenge/"],
            "tiktok": ["/login", "/signup"],
            "facebook": ["/login", "/checkpoint/"],
        }
        current_url = page.url
        indicators = login_indicators.get(platform, [])
        for indicator in indicators:
            if indicator in current_url:
                logger.warning(
                    f"Session invalid — login redirect detected: {current_url}"
                )
                return False
        return True
