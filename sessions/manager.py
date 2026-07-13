import json
import os
import re
import time
from typing import Optional

import redis

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import SessionExpiredError, SessionInvalidError

logger = get_logger(__name__)

# Permitted characters for account_id when used as a filesystem component.
# Keeps load/save/delete from being passed a path-traversal payload from any
# call site that didn't go through the admin schema's regex.
_SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not _SAFE_ACCOUNT_ID.match(account_id):
        raise ValueError(
            f"unsafe account_id {account_id!r}; must match "
            f"{_SAFE_ACCOUNT_ID.pattern}"
        )
    return account_id

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
        # Belt-and-braces: validate the account_id and assert the resolved
        # path stays inside ``storage_dir``. The regex covers the common
        # path-traversal vector (``..``); the resolved-path check defends
        # against anything that slips through (e.g. a future relaxation of
        # the regex or an OS quirk).
        _validate_account_id(account_id)
        platform_dir = os.path.realpath(
            os.path.join(self._storage_dir, platform)
        )
        os.makedirs(platform_dir, exist_ok=True)
        candidate = os.path.realpath(
            os.path.join(platform_dir, f"{account_id}.json")
        )
        storage_root = os.path.realpath(self._storage_dir)
        if not candidate.startswith(storage_root + os.sep):
            raise ValueError(
                f"resolved session path escapes storage dir: {candidate}"
            )
        return candidate

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
        """Verify a Playwright page reflects an authenticated session.

        Combines URL-pattern, forbidden-selector, and required-selector probes
        so we don't accept a session that merely happens to be on a non-login
        URL. Returns ``True`` only when all three checks pass.

        Falls back to a permissive URL-only verdict when no indicators are
        registered for the platform; this is intentional so adding a new
        platform doesn't immediately break sign-in flows before its DOM
        selectors are tuned (the warning makes the gap visible in logs).
        """
        indicators = self._get_indicators().get(platform)
        current_url = getattr(page, "url", "") or ""

        if indicators is None:
            logger.warning(
                "No session indicators registered for %s; falling back to "
                "URL-only check",
                platform,
            )
            return True

        # 1. URL deny list — cheapest, fail fast.
        for needle in indicators["url_deny"]:
            if needle in current_url:
                logger.warning(
                    "Session invalid — URL matched deny pattern %r: %s",
                    needle,
                    current_url,
                )
                return False

        # 2. Forbidden selector — login form / challenge overlay present.
        forbidden = indicators.get("forbidden_selector")
        if forbidden and self._selector_present(page, forbidden):
            logger.warning(
                "Session invalid — forbidden selector %r present at %s",
                forbidden,
                current_url,
            )
            return False

        # 3. Required selector — authenticated chrome must be visible.
        required = indicators.get("required_selector")
        if required and not self._selector_present(page, required):
            logger.warning(
                "Session invalid — required selector %r missing at %s",
                required,
                current_url,
            )
            return False

        return True

    @classmethod
    def _get_indicators(cls) -> dict:
        """Lazy-init per-platform DOM indicators.

        Three orthogonal checks are combined per platform:

        - ``url_deny``: substrings that must NOT appear in ``page.url``.
          Cheap first-pass for the ``?next=/login`` style redirects platforms
          serve when the cookie expires.
        - ``forbidden_selector``: a DOM selector that, if present, indicates an
          unauthenticated state (login form, challenge dialog, captcha). URL
          alone is unreliable: IG sometimes serves the challenge as an overlay
          on the same path.
        - ``required_selector``: a DOM selector that MUST be present on an
          authenticated page (nav rail, profile avatar). Catches "soft"
          logged-out states where the URL still looks fine but the page
          rendered a sign-in CTA instead of the feed.
        """
        if getattr(cls, "_INDICATOR_CACHE", None):
            return cls._INDICATOR_CACHE
        from core.platforms import Platform

        cls._INDICATOR_CACHE = {
            Platform.INSTAGRAM.value: {
                "url_deny": ["/accounts/login", "/challenge/"],
                "forbidden_selector": (
                    "input[name='username'], "
                    "div[role='dialog'][aria-label*='challenge' i]"
                ),
                "required_selector": (
                    "svg[aria-label='Home'], a[href='/direct/inbox/']"
                ),
            },
            Platform.TIKTOK.value: {
                "url_deny": ["/login", "/signup"],
                "forbidden_selector": (
                    "div[id='loginContainer'], a[href*='/login']"
                ),
                "required_selector": (
                    "div[data-e2e='profile-icon'], a[data-e2e='nav-profile']"
                ),
            },
            Platform.FACEBOOK.value: {
                "url_deny": ["/login", "/checkpoint/", "/recover/"],
                "forbidden_selector": (
                    "input[name='email'][type='text'], form[id='login_form']"
                ),
                "required_selector": (
                    "div[role='navigation'] a[href*='/me/'], "
                    "div[aria-label='Account controls']"
                ),
            },
        }
        return cls._INDICATOR_CACHE

    def _selector_present(self, page, selector: str) -> bool:
        """Return True if any element matching ``selector`` is in the DOM.

        Wraps Playwright in a defensive try/except: a navigation error or
        detached frame should be treated as "indicator absent" rather than
        crashing the worker. ``locator.count()`` is preferred over
        ``wait_for_selector`` because it never raises on missing elements
        and respects the existing default timeout.
        """
        try:
            locator = page.locator(selector).first
            return locator.count() > 0
        except Exception as exc:  # pragma: no cover - exercised via mock
            logger.debug(
                "Selector probe %r failed at %s: %s",
                selector,
                getattr(page, "url", "?"),
                exc,
            )
            return False
