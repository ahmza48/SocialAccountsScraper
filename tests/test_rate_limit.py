"""Tests for the rate-limit fixed-window TTL fix."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.main import _rate_limit_check
from core.config import Config


@pytest.mark.asyncio
async def test_first_request_sets_ttl_within_window(fake_async_redis, monkeypatch):
    monkeypatch.setattr(Config, "RATE_LIMIT_REQUESTS", 5)
    monkeypatch.setattr(Config, "RATE_LIMIT_WINDOW_SECONDS", 60)
    await _rate_limit_check(fake_async_redis, "1.2.3.4")
    ttl = await fake_async_redis.ttl("ratelimit:1.2.3.4")
    assert 0 < ttl <= 60


@pytest.mark.asyncio
async def test_subsequent_requests_do_not_extend_ttl(fake_async_redis, monkeypatch):
    """The TTL must reflect the start of the window, not the latest request."""
    monkeypatch.setattr(Config, "RATE_LIMIT_REQUESTS", 100)
    monkeypatch.setattr(Config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    await _rate_limit_check(fake_async_redis, "1.2.3.4")
    ttl1 = await fake_async_redis.ttl("ratelimit:1.2.3.4")

    # Simulate time passing so a TTL refresh would visibly bump us back to 60.
    await fake_async_redis.expire("ratelimit:1.2.3.4", 30)
    await _rate_limit_check(fake_async_redis, "1.2.3.4")
    ttl2 = await fake_async_redis.ttl("ratelimit:1.2.3.4")

    assert ttl2 <= 30, (
        f"TTL was refreshed on a subsequent request (was 30, now {ttl2}); "
        "this regression brings back the indefinite-lockout bug"
    )


@pytest.mark.asyncio
async def test_over_limit_raises_429(fake_async_redis, monkeypatch):
    monkeypatch.setattr(Config, "RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(Config, "RATE_LIMIT_WINDOW_SECONDS", 60)
    await _rate_limit_check(fake_async_redis, "ip")
    await _rate_limit_check(fake_async_redis, "ip")
    with pytest.raises(HTTPException) as exc:
        await _rate_limit_check(fake_async_redis, "ip")
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_per_scope_isolation(fake_async_redis, monkeypatch):
    monkeypatch.setattr(Config, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(Config, "RATE_LIMIT_WINDOW_SECONDS", 60)
    await _rate_limit_check(fake_async_redis, "ip-a")
    # ip-b is a different bucket; should not be affected by ip-a.
    await _rate_limit_check(fake_async_redis, "ip-b")
