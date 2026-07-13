"""Tests for ``core.platform_config`` and ``core.platforms``."""
from __future__ import annotations

import pytest

from core.platform_config import (
    PlatformConfig,
    PlatformConfigError,
    PlatformSpec,
)
from core.platforms import Platform, parse_platform


# ── Platform enum ────────────────────────────────────────────────


class TestPlatform:
    def test_str_equality(self) -> None:
        assert Platform.INSTAGRAM == "instagram"

    def test_queue_name_default(self) -> None:
        assert Platform.INSTAGRAM.queue_name == "scrape_instagram"
        assert Platform.TIKTOK.queue_name == "scrape_tiktok"
        assert Platform.FACEBOOK.queue_name == "scrape_facebook"

    def test_values_in_declaration_order(self) -> None:
        assert Platform.values() == ["instagram", "tiktok", "facebook"]

    def test_parse_accepts_enum(self) -> None:
        assert parse_platform(Platform.INSTAGRAM) is Platform.INSTAGRAM

    def test_parse_accepts_lowercase_str(self) -> None:
        assert parse_platform("tiktok") is Platform.TIKTOK

    def test_parse_is_case_insensitive_and_strips(self) -> None:
        assert parse_platform("  Instagram  ") is Platform.INSTAGRAM

    def test_parse_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="myspace"):
            parse_platform("myspace")

    def test_parse_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            parse_platform(42)  # type: ignore[arg-type]


# ── PlatformSpec ─────────────────────────────────────────────────


class TestPlatformSpec:
    def test_valid(self) -> None:
        spec = PlatformSpec(
            workers=3, accounts=5, ips=5,
            max_concurrent_browsers=2, queue_name="scrape_instagram",
        )
        assert spec.workers == 3

    def test_rejects_bad_queue_name(self) -> None:
        with pytest.raises(Exception):  # pydantic ValidationError
            PlatformSpec(
                workers=1, accounts=1, ips=1,
                max_concurrent_browsers=1, queue_name="UPPER_CASE",
            )

    def test_rejects_zero_workers(self) -> None:
        with pytest.raises(Exception):
            PlatformSpec(
                workers=0, accounts=1, ips=1,
                max_concurrent_browsers=1, queue_name="ok",
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            PlatformSpec(
                workers=1, accounts=1, ips=1,
                max_concurrent_browsers=1, queue_name="ok",
                bogus=True,  # type: ignore[call-arg]
            )

    def test_is_immutable(self) -> None:
        spec = PlatformSpec(
            workers=1, accounts=1, ips=1,
            max_concurrent_browsers=1, queue_name="ok",
        )
        with pytest.raises(Exception):
            spec.workers = 2  # type: ignore[misc]


# ── PlatformConfig (with YAML file) ─────────────────────────────


class TestPlatformConfig:
    def test_uses_defaults_when_no_file(self, monkeypatch, tmp_path) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "PLATFORM_CONFIG_PATH", str(tmp_path / "missing.yml"))
        # Defaults sum to 5+3+2 = 10 browsers, so make sure global cap is high enough.
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 100)
        pc = PlatformConfig()
        assert set(pc.platforms) == {"instagram", "tiktok", "facebook"}
        assert pc.queue_name("instagram") == "scrape_instagram"

    def test_partial_yaml_merges_defaults(self, reset_platform_config, monkeypatch) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 100)
        reset_platform_config(
            "platforms:\n"
            "  instagram:\n"
            "    workers: 9\n"
        )
        pc = PlatformConfig()
        assert pc.worker_count("instagram") == 9
        # other keys filled from defaults
        assert pc.queue_name("instagram") == "scrape_instagram"
        # other platforms still present
        assert "tiktok" in pc.platforms

    def test_unknown_platform_rejected(self, reset_platform_config, monkeypatch) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 100)
        reset_platform_config(
            "platforms:\n"
            "  myspace:\n"
            "    workers: 1\n"
            "    accounts: 1\n"
            "    ips: 1\n"
            "    max_concurrent_browsers: 1\n"
            "    queue_name: scrape_myspace\n"
        )
        with pytest.raises(PlatformConfigError, match="Unknown platform"):
            PlatformConfig()

    def test_browser_sum_exceeds_global_rejected(
        self, reset_platform_config, monkeypatch
    ) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 5)
        reset_platform_config(
            "platforms:\n"
            "  instagram: { workers: 1, accounts: 1, ips: 1, "
            "max_concurrent_browsers: 4, queue_name: scrape_instagram }\n"
            "  tiktok:    { workers: 1, accounts: 1, ips: 1, "
            "max_concurrent_browsers: 4, queue_name: scrape_tiktok }\n"
            "  facebook:  { workers: 1, accounts: 1, ips: 1, "
            "max_concurrent_browsers: 4, queue_name: scrape_facebook }\n"
        )
        with pytest.raises(PlatformConfigError, match="exceeds"):
            PlatformConfig()

    def test_duplicate_queue_name_rejected(
        self, reset_platform_config, monkeypatch
    ) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 100)
        reset_platform_config(
            "platforms:\n"
            "  instagram: { workers: 1, accounts: 1, ips: 1, "
            "max_concurrent_browsers: 1, queue_name: shared_queue }\n"
            "  tiktok:    { workers: 1, accounts: 1, ips: 1, "
            "max_concurrent_browsers: 1, queue_name: shared_queue }\n"
        )
        with pytest.raises(PlatformConfigError, match="Duplicate queue_name"):
            PlatformConfig()

    def test_malformed_yaml_rejected(
        self, reset_platform_config, monkeypatch
    ) -> None:
        from core.config import Config
        monkeypatch.setattr(Config, "MAX_CONCURRENT_BROWSERS", 100)
        reset_platform_config("platforms:\n  instagram: [unclosed")
        with pytest.raises(PlatformConfigError):
            PlatformConfig()
