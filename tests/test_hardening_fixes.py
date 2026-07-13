"""Tests for hardenings: path traversal, account re-registration, async circuit probe."""
from __future__ import annotations

import pytest

from account_pool.manager import AccountPoolManager
from sessions.manager import SessionManager, _validate_account_id
from utils.circuit_breaker import AsyncCircuitBreaker


# ── Session path traversal ──────────────────────────────────────


def test_validate_account_id_rejects_traversal():
    with pytest.raises(ValueError):
        _validate_account_id("../../etc/passwd")
    with pytest.raises(ValueError):
        _validate_account_id("a/b")
    with pytest.raises(ValueError):
        _validate_account_id("")


def test_validate_account_id_accepts_safe_ids():
    for ok in ("acct1", "acct.1", "acct_1", "acct-1", "ACCT-1"):
        assert _validate_account_id(ok) == ok


def test_storage_path_rejects_traversal(fake_redis, tmp_path):
    sm = SessionManager(redis_client=fake_redis, storage_dir=str(tmp_path))
    with pytest.raises(ValueError):
        sm._storage_path("instagram", "../../escape")


def test_storage_path_stays_within_storage_dir(fake_redis, tmp_path):
    sm = SessionManager(redis_client=fake_redis, storage_dir=str(tmp_path))
    path = sm._storage_path("instagram", "acct1")
    assert path.startswith(str(tmp_path.resolve()))


# ── Account pool: silent overwrite blocked ───────────────────────


def test_register_account_refuses_duplicate_by_default(fake_redis, fernet_key):
    pool = AccountPoolManager(redis_client=fake_redis)
    pool.register_account("a1", "instagram", {"u": "v"})
    with pytest.raises(ValueError, match="already registered"):
        pool.register_account("a1", "instagram", {"u": "v"})


def test_register_account_overwrite_replaces_record(fake_redis, fernet_key):
    pool = AccountPoolManager(redis_client=fake_redis)
    pool.register_account("a1", "instagram", {"u": "v1"})
    # Manually bump use_count so we can prove the overwrite reset it.
    fake_redis.hset("account:instagram:a1", "use_count", "42")
    pool.register_account("a1", "instagram", {"u": "v2"}, overwrite=True)
    assert fake_redis.hget("account:instagram:a1", "use_count") == "0"


def test_register_account_pipelined_atomic(fake_redis, fernet_key):
    """Hash + pool-set membership land together (transactional)."""
    pool = AccountPoolManager(redis_client=fake_redis)
    pool.register_account("a1", "instagram", {"u": "v"})
    assert fake_redis.exists("account:instagram:a1")
    assert fake_redis.sismember("accounts:instagram", "a1")


# ── Account pool: release pipeline ──────────────────────────────


def test_release_account_clears_lock_and_sets_cooldown_atomically(
    fake_redis, fernet_key
):
    pool = AccountPoolManager(redis_client=fake_redis)
    pool.register_account("a1", "instagram", {"u": "v"})
    pool.acquire_account("instagram", job_id="j1")
    # Lock present, status in_use.
    assert fake_redis.exists("account_lock:instagram:a1")

    pool.release_account("a1", "instagram", apply_cooldown=True)
    assert not fake_redis.exists("account_lock:instagram:a1")
    assert fake_redis.exists("account_cooldown:instagram:a1")
    assert fake_redis.hget("account:instagram:a1", "status") == "cooldown"


def test_release_account_idle_path(fake_redis, fernet_key):
    pool = AccountPoolManager(redis_client=fake_redis)
    pool.register_account("a1", "instagram", {"u": "v"})
    pool.acquire_account("instagram", job_id="j1")
    pool.release_account("a1", "instagram", apply_cooldown=False)
    assert not fake_redis.exists("account_lock:instagram:a1")
    assert not fake_redis.exists("account_cooldown:instagram:a1")
    assert fake_redis.hget("account:instagram:a1", "status") == "idle"


# ── Async circuit breaker: only one probe per recovery window ───


@pytest.mark.asyncio
async def test_half_open_only_grants_one_probe(fake_async_redis):
    cb = AsyncCircuitBreaker(fake_async_redis)
    # Set up: circuit is in HALF_OPEN.
    await fake_async_redis.set("circuit:instagram:state", "half_open")

    first = await cb.is_available("instagram")
    second = await cb.is_available("instagram")
    third = await cb.is_available("instagram")

    # Exactly one of the three concurrent checks gets the probe permit.
    assert sum([first, second, third]) == 1


@pytest.mark.asyncio
async def test_open_recovery_atomic_transition(fake_async_redis, monkeypatch):
    """During OPEN→HALF_OPEN recovery, only one caller is let through."""
    import time as _time

    from core.config import Config

    monkeypatch.setattr(Config, "CIRCUIT_BREAKER_RECOVERY_SECONDS", 30)
    cb = AsyncCircuitBreaker(fake_async_redis)
    await fake_async_redis.set("circuit:instagram:state", "open")
    # opened_at far enough in the past that recovery has elapsed.
    await fake_async_redis.set(
        "circuit:instagram:opened_at", str(_time.time() - 999)
    )

    results = [await cb.is_available("instagram") for _ in range(5)]
    assert sum(results) == 1
    # State advanced to half_open after the winning probe.
    assert await fake_async_redis.get("circuit:instagram:state") == "half_open"


@pytest.mark.asyncio
async def test_open_no_recovery_yet_blocks_all(fake_async_redis, monkeypatch):
    import time as _time

    from core.config import Config

    monkeypatch.setattr(Config, "CIRCUIT_BREAKER_RECOVERY_SECONDS", 999)
    cb = AsyncCircuitBreaker(fake_async_redis)
    await fake_async_redis.set("circuit:instagram:state", "open")
    await fake_async_redis.set(
        "circuit:instagram:opened_at", str(_time.time())
    )
    assert await cb.is_available("instagram") is False
