"""Per-platform configuration loaded from YAML.

Provides worker counts, account pool sizes, concurrency limits,
and queue names per platform. Falls back to sensible defaults
if platform_config.yml is missing.
"""
import os
from typing import Dict, List, Optional

import yaml

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT_PLATFORM_CONFIG: Dict[str, dict] = {
    "instagram": {
        "workers": 5,
        "accounts": 10,
        "ips": 10,
        "max_concurrent_browsers": 5,
        "queue_name": "scrape_instagram",
    },
    "tiktok": {
        "workers": 3,
        "accounts": 3,
        "ips": 3,
        "max_concurrent_browsers": 3,
        "queue_name": "scrape_tiktok",
    },
    "facebook": {
        "workers": 2,
        "accounts": 2,
        "ips": 2,
        "max_concurrent_browsers": 2,
        "queue_name": "scrape_facebook",
    },
}


class PlatformConfig:
    """Per-platform configuration with YAML file backing.

    Reads platform_config.yml if available, otherwise uses defaults.
    Merges defaults for any missing keys to ensure completeness.
    """

    def __init__(self) -> None:
        self._config: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        config_path = Config.PLATFORM_CONFIG_PATH
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            self._config = raw.get("platforms", {})
            logger.info(f"Loaded platform config from {config_path}")
        else:
            self._config = {k: dict(v) for k, v in _DEFAULT_PLATFORM_CONFIG.items()}
            logger.info("Using default platform config (no YAML found)")

        # Merge defaults for any missing keys
        for platform, defaults in _DEFAULT_PLATFORM_CONFIG.items():
            if platform not in self._config:
                self._config[platform] = dict(defaults)
            else:
                for key, value in defaults.items():
                    self._config[platform].setdefault(key, value)

    def reload(self) -> None:
        """Reload configuration from disk."""
        self._load()

    @property
    def platforms(self) -> List[str]:
        return list(self._config.keys())

    def get(self, platform: str) -> dict:
        if platform not in self._config:
            raise ValueError(f"Unknown platform: {platform}")
        return self._config[platform]

    def queue_name(self, platform: str) -> str:
        return self.get(platform)["queue_name"]

    def max_browsers(self, platform: str) -> int:
        return self.get(platform)["max_concurrent_browsers"]

    def worker_count(self, platform: str) -> int:
        return self.get(platform)["workers"]

    def account_count(self, platform: str) -> int:
        return self.get(platform)["accounts"]

    @property
    def total_max_browsers(self) -> int:
        return sum(c["max_concurrent_browsers"] for c in self._config.values())


_platform_config: Optional[PlatformConfig] = None


def get_platform_config() -> PlatformConfig:
    """Lazy singleton for platform configuration."""
    global _platform_config
    if _platform_config is None:
        _platform_config = PlatformConfig()
    return _platform_config
