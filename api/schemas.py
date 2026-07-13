"""Pydantic request/response models for the public API.

Centralising these here keeps validation rules in one place and gives FastAPI
a real OpenAPI surface (callers see required fields, types, and examples).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from core.platforms import Platform, parse_platform


# ── Per-platform username rules ───────────────────────────────────
#
# Sources:
#   instagram \u2014 1-30 chars, letters/digits/underscore/period.
#   tiktok    \u2014 2-24 chars, letters/digits/underscore/period (an optional
#              leading "@" is stripped before validation).
#   facebook  \u2014 either a numeric user/page id (5-20 digits) or a vanity URL
#              segment (5-50 chars, letters/digits/period).
#
# These are deliberately a touch tighter than the platforms themselves to
# block obvious junk early; the scrapers will still surface
# ``UserNotFoundError`` for inputs that pass the regex but don't resolve.

_USERNAME_PATTERNS: dict = {
    Platform.INSTAGRAM.value: re.compile(r"^[a-zA-Z0-9._]{1,30}$"),
    Platform.TIKTOK.value: re.compile(r"^[a-zA-Z0-9._]{2,24}$"),
    Platform.FACEBOOK.value: re.compile(
        r"^(?:\d{5,20}|[a-zA-Z0-9.]{5,50})$"
    ),
}

_MAX_USERNAME_LEN = 100  # absolute upper bound (matches old behaviour)


def _normalize_username(value: str, platform: Platform) -> str:
    """Strip whitespace and an optional ``@`` prefix on TikTok handles."""
    cleaned = value.strip()
    if platform is Platform.TIKTOK and cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned


def _validate_username(username: str, platform: Platform) -> str:
    if not username:
        raise ValueError("username is required")
    if len(username) > _MAX_USERNAME_LEN:
        raise ValueError(
            f"username exceeds {_MAX_USERNAME_LEN} chars"
        )
    pattern = _USERNAME_PATTERNS.get(platform.value)
    if pattern is None:
        # Defensive: should never happen because platform comes from the enum.
        raise ValueError(f"no validation pattern registered for {platform.value}")
    if not pattern.match(username):
        raise ValueError(
            f"username {username!r} is not a valid {platform.value} handle"
        )
    return username


# ── Request models ───────────────────────────────────────────────


class ScrapeRequest(BaseModel):
    """Body of ``POST /scrape``.

    ``cursor`` is a signed token issued by a previous response; raw cursors
    are never accepted from the client (verified in :func:`api.main.scrape`).
    """

    username: str = Field(min_length=1, max_length=_MAX_USERNAME_LEN)
    platform: str
    cursor: Optional[str] = Field(default=None, max_length=2048)

    model_config = {"extra": "forbid"}

    @field_validator("platform")
    @classmethod
    def _coerce_platform(cls, v: str) -> str:
        return parse_platform(v).value

    @model_validator(mode="after")
    def _check_username(self) -> "ScrapeRequest":
        platform = Platform(self.platform)
        username = _normalize_username(self.username, platform)
        # ``object.__setattr__`` because the model is otherwise immutable
        # to outside callers via FastAPI; we want the cleaned value to stick.
        object.__setattr__(self, "username", _validate_username(username, platform))
        return self


# ── Response models ──────────────────────────────────────────────


class ScrapeQueuedResponse(BaseModel):
    status: str = Field(pattern=r"^(queued|processing)$")
    job_id: str


class ScrapeCachedResponse(BaseModel):
    status: str = Field(pattern=r"^cached$")
    data: dict


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    data: Optional[Any] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    redis: str
    redis_writable: bool = False


# ── Admin models ─────────────────────────────────────────────────
#
# Admin endpoints are gated by ADMIN_AUTH_TOKEN (see core/security). These
# models keep payload validation strict so a leaked admin token still has a
# small attack surface (no oversize fields, no unknown keys).


_ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_PROXY_RE = re.compile(r"^[a-zA-Z0-9+:/.@_\-]{1,256}$")


class AccountRegisterRequest(BaseModel):
    """Body of ``POST /admin/accounts``.

    ``credentials`` is encrypted at rest by the account pool; the field is
    excluded from any string repr to avoid leaking secrets in logs and traces.
    """

    account_id: str = Field(min_length=1, max_length=64)
    platform: str
    credentials: dict = Field(repr=False)
    proxy: Optional[str] = Field(default=None, max_length=256)

    model_config = {"extra": "forbid"}

    @field_validator("platform")
    @classmethod
    def _coerce_platform(cls, v: str) -> str:
        return parse_platform(v).value

    @field_validator("account_id")
    @classmethod
    def _check_account_id(cls, v: str) -> str:
        if not _ACCOUNT_ID_RE.match(v):
            raise ValueError("account_id must match [A-Za-z0-9._-]{1,64}")
        return v

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _PROXY_RE.match(v):
            raise ValueError("proxy contains invalid characters")
        return v

    @field_validator("credentials")
    @classmethod
    def _check_credentials(cls, v: dict) -> dict:
        if not isinstance(v, dict) or not v:
            raise ValueError("credentials must be a non-empty object")
        # Cap nested payload size so a leaked admin token can't be used to
        # blow up Redis memory by registering huge encrypted blobs.
        if len(json.dumps(v, separators=(",", ":"))) > 4096:
            raise ValueError("credentials payload exceeds 4KB")
        return v


class AccountInvalidateRequest(BaseModel):
    reason: str = Field(default="manual", min_length=1, max_length=256)
    model_config = {"extra": "forbid"}


class AccountActionResponse(BaseModel):
    account_id: str
    platform: str
    status: str


class PoolStatusResponse(BaseModel):
    platform: str
    total: int
    idle: int
    in_use: int
    cooldown: int
    invalid: int


class DLQEntry(BaseModel):
    job_id: str
    platform: str
    username: str
    error: str
    attempts: int
    failed_at: float


class DLQListResponse(BaseModel):
    platform: Optional[str] = None
    total: int
    entries: list[DLQEntry]


class JobCancelResponse(BaseModel):
    job_id: str
    cancelled: bool
    dedup_cleared: bool
