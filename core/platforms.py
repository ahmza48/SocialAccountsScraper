"""Canonical platform identifiers.

A single source of truth for the set of supported social platforms. Using a
``str, Enum`` lets us keep the existing string surface (Redis keys, YAML keys,
env vars, RQ queue names) while gaining type-safety, IDE completion, and a
single place to add or rename a platform.

Usage::

    from core.platforms import Platform, parse_platform

    Platform.INSTAGRAM == "instagram"      # True
    Platform("instagram") is Platform.INSTAGRAM
    parse_platform("Instagram")            # case-insensitive, returns Platform.INSTAGRAM
    parse_platform("myspace")              # raises ValueError
"""
from __future__ import annotations

from enum import Enum
from typing import List, Union


class Platform(str, Enum):
    """Supported scraping platforms.

    Inheriting from ``str`` means instances compare equal to their value, so
    code that still uses bare strings (Redis HSET fields, log extras, etc.)
    keeps working. New code should prefer the enum.
    """

    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    FACEBOOK = "facebook"

    @property
    def queue_name(self) -> str:
        """Default RQ queue name for this platform.

        ``platform_config.yml`` may override this via the ``queue_name`` field;
        always read the effective value through :class:`PlatformConfig` rather
        than this property in production code paths.
        """
        return f"scrape_{self.value}"

    @classmethod
    def values(cls) -> List[str]:
        """All platform string values, in declaration order."""
        return [p.value for p in cls]


def parse_platform(value: Union[str, Platform]) -> Platform:
    """Coerce a user-supplied value into a :class:`Platform`.

    Accepts case-insensitive strings and existing ``Platform`` instances.
    Raises ``ValueError`` with the list of supported platforms on failure so
    callers can surface a useful API error message.
    """
    if isinstance(value, Platform):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"Platform must be a string, got {type(value).__name__}"
        )
    normalized = value.strip().lower()
    try:
        return Platform(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported platform {value!r}. Supported: {Platform.values()}"
        ) from exc
