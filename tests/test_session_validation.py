"""Tests for DOM-aware session validation in :mod:`sessions.manager`."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sessions.manager import SessionManager


@pytest.fixture
def manager(fake_redis, tmp_path):
    return SessionManager(redis_client=fake_redis, storage_dir=str(tmp_path))


def _fake_page(url: str, present=None):
    """Build a Playwright-ish page mock.

    ``present`` is the set of selector strings that should report count>0;
    everything else returns 0. The locator chain mirrors Playwright's
    ``page.locator(sel).first.count()``.
    """
    page = MagicMock()
    page.url = url
    present = present or set()

    def _locator(selector: str):
        loc = MagicMock()
        loc.first = loc
        loc.count.return_value = 1 if selector in present else 0
        return loc

    page.locator.side_effect = _locator
    return page


# ── URL deny list ────────────────────────────────────────────────


def test_url_deny_pattern_invalidates_session(manager):
    page = _fake_page("https://www.instagram.com/accounts/login/?next=/")
    assert manager.validate_session_page(page, "instagram") is False


def test_challenge_url_invalidates_session(manager):
    page = _fake_page("https://www.instagram.com/challenge/12345/")
    assert manager.validate_session_page(page, "instagram") is False


# ── Required selector ───────────────────────────────────────────


def test_missing_required_selector_invalidates_session(manager):
    # URL looks fine but the required home-icon SVG is missing.
    page = _fake_page("https://www.instagram.com/", present=set())
    assert manager.validate_session_page(page, "instagram") is False


def test_required_selector_present_keeps_session_valid(manager):
    page = _fake_page(
        "https://www.instagram.com/",
        present={"svg[aria-label='Home'], a[href='/direct/inbox/']"},
    )
    assert manager.validate_session_page(page, "instagram") is True


# ── Forbidden selector ──────────────────────────────────────────


def test_forbidden_selector_invalidates_even_when_url_clean(manager):
    """IG sometimes overlays a challenge dialog without changing the URL."""
    page = _fake_page(
        "https://www.instagram.com/",
        present={
            # Forbidden present AND required present → forbidden wins.
            "input[name='username'], "
            "div[role='dialog'][aria-label*='challenge' i]",
            "svg[aria-label='Home'], a[href='/direct/inbox/']",
        },
    )
    assert manager.validate_session_page(page, "instagram") is False


# ── Cross-platform sanity ────────────────────────────────────────


def test_tiktok_required_selector_check(manager):
    page = _fake_page(
        "https://www.tiktok.com/",
        present={"div[data-e2e='profile-icon'], a[data-e2e='nav-profile']"},
    )
    assert manager.validate_session_page(page, "tiktok") is True


def test_tiktok_login_url_invalidates(manager):
    page = _fake_page("https://www.tiktok.com/login")
    assert manager.validate_session_page(page, "tiktok") is False


def test_facebook_checkpoint_url_invalidates(manager):
    page = _fake_page("https://www.facebook.com/checkpoint/?u=1")
    assert manager.validate_session_page(page, "facebook") is False


# ── Defensive paths ─────────────────────────────────────────────


def test_unknown_platform_falls_back_to_permissive_verdict(manager):
    page = _fake_page("https://example.com/")
    # No indicators registered for "myspace" — defensive default returns True.
    # The accompanying WARNING log is intentional but not asserted on (the
    # project logger uses a non-propagating handler so caplog can't see it).
    assert manager.validate_session_page(page, "myspace") is True


def test_selector_probe_swallows_playwright_errors(manager):
    """A locator that raises (e.g. detached frame) should be treated as absent."""
    page = MagicMock()
    page.url = "https://www.instagram.com/"

    def _boom(_selector):
        raise RuntimeError("frame detached")

    page.locator.side_effect = _boom
    # required_selector probe raises → treated as missing → invalid.
    assert manager.validate_session_page(page, "instagram") is False
