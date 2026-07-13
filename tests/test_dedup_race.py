"""Tests for the atomic dedup acquire and dispatcher race protection."""
from __future__ import annotations

import asyncio

import pytest

from core.config import Config
from core.job_state import AsyncJobStateManager, _dedup_key


@pytest.mark.asyncio
async def test_acquire_dedup_returns_none_on_first_claim(fake_async_redis):
    jsm = AsyncJobStateManager(fake_async_redis)
    result = await jsm.acquire_dedup("instagram", "alice", "job-1")
    assert result is None
    assert await fake_async_redis.get(_dedup_key("instagram", "alice")) == "job-1"


@pytest.mark.asyncio
async def test_acquire_dedup_returns_existing_holder_on_conflict(fake_async_redis):
    jsm = AsyncJobStateManager(fake_async_redis)
    first = await jsm.acquire_dedup("instagram", "alice", "job-1")
    second = await jsm.acquire_dedup("instagram", "alice", "job-2")
    assert first is None
    assert second == "job-1"
    # Stored value untouched by the losing claim.
    assert await fake_async_redis.get(_dedup_key("instagram", "alice")) == "job-1"


@pytest.mark.asyncio
async def test_acquire_dedup_sets_ttl(fake_async_redis):
    jsm = AsyncJobStateManager(fake_async_redis)
    await jsm.acquire_dedup("instagram", "alice", "job-1")
    ttl = await fake_async_redis.ttl(_dedup_key("instagram", "alice"))
    assert 0 < ttl <= Config.JOB_DEDUP_TTL_SECONDS


@pytest.mark.asyncio
async def test_acquire_dedup_concurrent_only_one_winner(fake_async_redis):
    """The race that the SETNX fix exists to close: 50 concurrent claims for
    the same target should leave exactly one holder, and every loser should
    learn the winner's job id."""
    jsm = AsyncJobStateManager(fake_async_redis)

    async def claim(i: int):
        return await jsm.acquire_dedup("instagram", "alice", f"job-{i}")

    results = await asyncio.gather(*(claim(i) for i in range(50)))
    winners = [r for r in results if r is None]
    losers = [r for r in results if r is not None]
    assert len(winners) == 1
    assert len(losers) == 49
    # Every loser saw the same winner.
    assert len(set(losers)) == 1
    held = await fake_async_redis.get(_dedup_key("instagram", "alice"))
    assert losers[0] == held


@pytest.mark.asyncio
async def test_acquire_dedup_per_target_isolation(fake_async_redis):
    """Different (platform, username) tuples don't interfere with each other."""
    jsm = AsyncJobStateManager(fake_async_redis)
    a = await jsm.acquire_dedup("instagram", "alice", "j-a")
    b = await jsm.acquire_dedup("instagram", "bob", "j-b")
    c = await jsm.acquire_dedup("tiktok", "alice", "j-c")
    assert a is None and b is None and c is None
