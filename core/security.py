"""Security primitives for the API layer.

Provides:

* ``CursorSigner`` \u2014 HMAC-SHA256 wrapping/unwrapping of pagination cursors so
  the API only ever accepts cursors it issued. Stops attackers from probing
  arbitrary internal cursor values for enumeration or DoS.
* ``verify_metrics_token`` \u2014 constant-time bearer-token check for ``/metrics``.

Configuration:

* ``CURSOR_SIGNING_KEY`` \u2014 hex/base64 secret. **Required in production.**
  If unset, a per-process random key is generated and a CRITICAL warning is
  logged; cursors will not be verifiable across process restarts (acceptable
  for local dev only).
* ``METRICS_AUTH_TOKEN`` \u2014 bearer token for ``/metrics``. If unset, the
  endpoint denies all access (fail-closed).
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Optional

from core.logging_config import get_logger

logger = get_logger(__name__)


# ── Cursor signing ────────────────────────────────────────────────


# Cursors are intentionally short-lived: clients are expected to walk
# pagination promptly, and a stale cursor is a UX nudge to re-issue the search.
_DEFAULT_CURSOR_TTL_SECONDS = 3600
# Hard ceiling on raw cursor size so the wire token cannot be used as an
# amplification vector (the API forwards raw cursors verbatim to the scraper).
_MAX_RAW_CURSOR_LEN = 512
# Hard ceiling on a signed token \u2014 generously over the expected size.
_MAX_SIGNED_TOKEN_LEN = 2048


class CursorError(ValueError):
    """Raised when a signed cursor fails parsing, signature, or expiry checks."""


@dataclass(frozen=True)
class CursorPayload:
    platform: str
    username: str
    raw: str
    exp: int


class CursorSigner:
    """HMAC-SHA256 signer/verifier for pagination cursors.

    Wire format::

        <urlsafe_b64(payload_json)>.<urlsafe_b64(hmac_digest)>

    Both halves are base64-urlsafe (no padding) so the token survives URL
    encoding without further escaping.
    """

    _SEP = "."

    def __init__(self, key: bytes, ttl_seconds: int = _DEFAULT_CURSOR_TTL_SECONDS) -> None:
        if not key:
            raise ValueError("CursorSigner requires a non-empty key")
        self._key = key
        self._ttl = ttl_seconds

    @classmethod
    def from_env(
        cls,
        env_var: str = "CURSOR_SIGNING_KEY",
        ttl_seconds: int = _DEFAULT_CURSOR_TTL_SECONDS,
    ) -> "CursorSigner":
        raw = os.getenv(env_var)
        if raw:
            # Accept hex, base64-urlsafe, or arbitrary bytes \u2014 we just need entropy.
            key = raw.encode("utf-8")
        else:
            key = secrets.token_bytes(32)
            logger.critical(
                "%s is not set; using a per-process random cursor signing key. "
                "Cursors will be invalidated on every restart. SET %s in production.",
                env_var,
                env_var,
            )
        return cls(key, ttl_seconds=ttl_seconds)

    # ── Public API ────────────────────────────────────────────────

    def sign(self, platform: str, username: str, raw_cursor: str) -> str:
        """Wrap a raw cursor in a signed token bound to (platform, username)."""
        if not raw_cursor:
            return ""
        if len(raw_cursor) > _MAX_RAW_CURSOR_LEN:
            raise CursorError(
                f"raw cursor exceeds {_MAX_RAW_CURSOR_LEN} chars"
            )
        payload = {
            "p": platform,
            "u": username,
            "c": raw_cursor,
            "e": int(time.time()) + self._ttl,
        }
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        body = _b64encode(payload_bytes)
        digest = _b64encode(self._mac(payload_bytes))
        return f"{body}{self._SEP}{digest}"

    def verify(self, token: str, platform: str, username: str) -> str:
        """Verify a signed token and return the underlying raw cursor.

        Raises :class:`CursorError` on any failure (bad format, bad signature,
        expired, mismatched platform/username).
        """
        if not token:
            return ""
        if len(token) > _MAX_SIGNED_TOKEN_LEN:
            raise CursorError("cursor token exceeds maximum length")
        try:
            body_b64, digest_b64 = token.split(self._SEP, 1)
        except ValueError as exc:
            raise CursorError("malformed cursor token") from exc

        try:
            payload_bytes = _b64decode(body_b64)
            provided_digest = _b64decode(digest_b64)
        except (ValueError, TypeError) as exc:
            raise CursorError("cursor token base64 invalid") from exc

        expected_digest = self._mac(payload_bytes)
        if not hmac.compare_digest(provided_digest, expected_digest):
            raise CursorError("cursor signature mismatch")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CursorError("cursor payload not valid JSON") from exc

        try:
            parsed = CursorPayload(
                platform=str(payload["p"]),
                username=str(payload["u"]),
                raw=str(payload["c"]),
                exp=int(payload["e"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorError("cursor payload missing fields") from exc

        if parsed.platform != platform:
            raise CursorError("cursor bound to a different platform")
        if parsed.username != username:
            raise CursorError("cursor bound to a different username")
        if parsed.exp < int(time.time()):
            raise CursorError("cursor expired")

        return parsed.raw

    # ── Internal ──────────────────────────────────────────────────

    def _mac(self, payload_bytes: bytes) -> bytes:
        return hmac.new(self._key, payload_bytes, sha256).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


_signer: Optional[CursorSigner] = None


def get_cursor_signer() -> CursorSigner:
    """Lazy singleton for :class:`CursorSigner`."""
    global _signer
    if _signer is None:
        _signer = CursorSigner.from_env()
    return _signer


def reset_cursor_signer() -> None:
    """Drop the cached signer (used by tests after env mutation)."""
    global _signer
    _signer = None


# ── Metrics token ────────────────────────────────────────────────


def _verify_bearer(authorization_header: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time bearer-token check shared by metrics and admin gates.

    Returns ``False`` whenever ``expected`` is empty so callers fail closed
    when the corresponding env var has not been provisioned.
    """
    if not expected:
        return False
    if not authorization_header:
        return False
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def verify_metrics_token(authorization_header: Optional[str]) -> bool:
    """Constant-time check of the ``Authorization: Bearer <token>`` header.

    Returns ``False`` (deny) when ``METRICS_AUTH_TOKEN`` is unset — fail-closed
    so a forgotten env var doesn't expose internal stats.
    """
    return _verify_bearer(authorization_header, os.getenv("METRICS_AUTH_TOKEN"))


def verify_admin_token(authorization_header: Optional[str]) -> bool:
    """Constant-time bearer check for ``/admin/*`` endpoints.

    Fails closed when ``ADMIN_AUTH_TOKEN`` is unset. The admin token is kept
    distinct from the metrics token so a read-only metrics leak cannot be
    escalated to mutating account state.
    """
    return _verify_bearer(authorization_header, os.getenv("ADMIN_AUTH_TOKEN"))
