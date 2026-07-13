"""Per-platform configuration loaded from YAML and validated with Pydantic.

Provides worker counts, account pool sizes, concurrency limits, and queue
names per platform. Falls back to sensible defaults when ``platform_config.yml``
is missing. Validation happens at load time so a malformed config fails fast
on startup rather than blowing up inside a worker.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from core.config import Config
from core.logging_config import get_logger
from core.platforms import Platform

logger = get_logger(__name__)


# Reasonable upper bounds; tuned to flag obvious typos rather than to enforce
# operational policy. Operators can lift these by editing the model.
_MAX_WORKERS = 50
_MAX_ACCOUNTS = 200
_MAX_IPS = 200
_MAX_BROWSERS_PER_PLATFORM = 100
_QUEUE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class PlatformSpec(BaseModel):
    """Validated per-platform configuration block."""

    workers: int = Field(gt=0, le=_MAX_WORKERS)
    accounts: int = Field(gt=0, le=_MAX_ACCOUNTS)
    ips: int = Field(gt=0, le=_MAX_IPS)
    max_concurrent_browsers: int = Field(gt=0, le=_MAX_BROWSERS_PER_PLATFORM)
    queue_name: str

    model_config = {"extra": "forbid", "frozen": True}

    @field_validator("queue_name")
    @classmethod
    def _validate_queue_name(cls, v: str) -> str:
        if not _QUEUE_NAME_RE.match(v):
            raise ValueError(
                f"queue_name {v!r} must match {_QUEUE_NAME_RE.pattern} "
                f"(lowercase alphanumeric with underscores)"
            )
        return v


_DEFAULT_PLATFORM_CONFIG: Dict[str, dict] = {
    Platform.INSTAGRAM.value: {
        "workers": 5,
        "accounts": 10,
        "ips": 10,
        "max_concurrent_browsers": 5,
        "queue_name": Platform.INSTAGRAM.queue_name,
    },
    Platform.TIKTOK.value: {
        "workers": 3,
        "accounts": 3,
        "ips": 3,
        "max_concurrent_browsers": 3,
        "queue_name": Platform.TIKTOK.queue_name,
    },
    Platform.FACEBOOK.value: {
        "workers": 2,
        "accounts": 2,
        "ips": 2,
        "max_concurrent_browsers": 2,
        "queue_name": Platform.FACEBOOK.queue_name,
    },
}


class PlatformConfigError(ValueError):
    """Raised when ``platform_config.yml`` fails validation."""


class PlatformConfig:
    """Per-platform configuration with YAML file backing and Pydantic validation.

    On load:
        - Reads ``platform_config.yml`` if present, else uses defaults.
        - Merges defaults for any missing keys so partial configs are accepted.
        - Validates every block against :class:`PlatformSpec`.
        - Asserts the sum of ``max_concurrent_browsers`` does not exceed the
          global ``Config.MAX_CONCURRENT_BROWSERS`` ceiling.
        - Rejects platform keys that are not declared in :class:`Platform`.
    """

    def __init__(self) -> None:
        self._config: Dict[str, PlatformSpec] = {}
        self._load()

    def _load(self) -> None:
        config_path = Config.PLATFORM_CONFIG_PATH
        raw_platforms: Dict[str, dict]

        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    raw = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                raise PlatformConfigError(
                    f"Failed to parse {config_path}: {exc}"
                ) from exc
            raw_platforms = dict(raw.get("platforms") or {})
            logger.info(f"Loaded platform config from {config_path}")
        else:
            raw_platforms = {}
            logger.info("Using default platform config (no YAML found)")

        # Reject unknown platform keys early — typos here silently disable a
        # whole platform otherwise (e.g. "insta" → never starts a worker).
        known = set(Platform.values())
        unknown = set(raw_platforms) - known
        if unknown:
            raise PlatformConfigError(
                f"Unknown platform(s) in {config_path}: {sorted(unknown)}. "
                f"Supported: {sorted(known)}"
            )

        # Merge defaults for any missing platform or missing key, so partial
        # YAML files remain valid (operators only override what they care about).
        merged: Dict[str, dict] = {}
        for platform, defaults in _DEFAULT_PLATFORM_CONFIG.items():
            block = dict(defaults)
            block.update(raw_platforms.get(platform) or {})
            merged[platform] = block

        # Validate each block. Collect all errors before raising so operators
        # see every problem in one shot rather than fixing one at a time.
        validated: Dict[str, PlatformSpec] = {}
        errors: List[str] = []
        for platform, block in merged.items():
            try:
                validated[platform] = PlatformSpec(**block)
            except ValidationError as exc:
                errors.append(f"[{platform}] {exc}")
        if errors:
            raise PlatformConfigError(
                "Invalid platform configuration:\n  " + "\n  ".join(errors)
            )

        # Cross-block invariant: per-platform browser caps must fit under the
        # global cap, otherwise the Lua slot script will reject jobs that
        # platform_config.yml claims are allowed.
        total = sum(spec.max_concurrent_browsers for spec in validated.values())
        if total > Config.MAX_CONCURRENT_BROWSERS:
            raise PlatformConfigError(
                f"Sum of per-platform max_concurrent_browsers ({total}) exceeds "
                f"global MAX_CONCURRENT_BROWSERS ({Config.MAX_CONCURRENT_BROWSERS}). "
                f"Either lower the per-platform values or raise the env var."
            )

        # Cross-block invariant: queue names must be unique, otherwise two
        # platforms compete on the same RQ queue and dispatch is non-deterministic.
        seen_queues: Dict[str, str] = {}
        for platform, spec in validated.items():
            existing = seen_queues.get(spec.queue_name)
            if existing:
                raise PlatformConfigError(
                    f"Duplicate queue_name {spec.queue_name!r} used by "
                    f"both {existing!r} and {platform!r}"
                )
            seen_queues[spec.queue_name] = platform

        self._config = validated

    def reload(self) -> None:
        """Reload configuration from disk (re-runs validation)."""
        self._load()

    # ── Access helpers ─────────────────────────────────────────────

    @property
    def platforms(self) -> List[str]:
        """Configured platform string keys, in declaration order."""
        return list(self._config.keys())

    def has(self, platform: str) -> bool:
        return platform in self._config

    def get(self, platform: str) -> dict:
        if platform not in self._config:
            raise ValueError(f"Unknown platform: {platform}")
        return self._config[platform].model_dump()

    def spec(self, platform: str) -> PlatformSpec:
        """Return the validated :class:`PlatformSpec` for a platform."""
        if platform not in self._config:
            raise ValueError(f"Unknown platform: {platform}")
        return self._config[platform]

    def queue_name(self, platform: str) -> str:
        return self.spec(platform).queue_name

    def max_browsers(self, platform: str) -> int:
        return self.spec(platform).max_concurrent_browsers

    def worker_count(self, platform: str) -> int:
        return self.spec(platform).workers

    def account_count(self, platform: str) -> int:
        return self.spec(platform).accounts

    @property
    def total_max_browsers(self) -> int:
        return sum(s.max_concurrent_browsers for s in self._config.values())


_platform_config: Optional[PlatformConfig] = None


def get_platform_config() -> PlatformConfig:
    """Lazy singleton for platform configuration."""
    global _platform_config
    if _platform_config is None:
        _platform_config = PlatformConfig()
    return _platform_config


def reset_platform_config() -> None:
    """Drop the cached singleton (used by tests and reload hooks)."""
    global _platform_config
    _platform_config = None
