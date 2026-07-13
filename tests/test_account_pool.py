"""Tests for ``account_pool.manager.AccountPoolManager``.

Covers the atomic Lua acquire script (F3), pool-set cleanup on invalidation
(F8), credential encryption integration (F5), and the corruption fall-back.
"""
from __future__ import annotations

import pytest

from account_pool.manager import AccountPoolManager
from core.exceptions import AccountUnavailableError


@pytest.fixture
def pool(fake_redis, fernet_key) -> AccountPoolManager:
    return AccountPoolManager(redis_client=fake_redis)


class TestRegister:
    def test_credentials_stored_encrypted(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "alice", "p": "pw"})
        raw = fake_redis.hget("account:instagram:acc1", "credentials")
        assert raw.startswith("ENC:v1:")
        assert "alice" not in raw and "pw" not in raw

    def test_added_to_pool_set(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "alice"})
        assert fake_redis.sismember("accounts:instagram", "acc1")


class TestAcquireRelease:
    def test_acquire_returns_decrypted_credentials(self, pool) -> None:
        pool.register_account("acc1", "instagram", {"u": "alice", "p": "pw"})
        account = pool.acquire_account("instagram", job_id="job1")
        assert account["account_id"] == "acc1"
        assert account["credentials"] == {"u": "alice", "p": "pw"}

    def test_acquire_locks_account_atomically(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "alice"})
        pool.acquire_account("instagram", job_id="job1")
        # Lock key set, status flipped, in one go.
        assert fake_redis.exists("account_lock:instagram:acc1")
        assert fake_redis.hget("account:instagram:acc1", "status") == "in_use"
        assert fake_redis.hget("account:instagram:acc1", "use_count") == "1"

    def test_acquire_skips_locked_account(self, pool) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.register_account("acc2", "instagram", {"u": "b"})
        first = pool.acquire_account("instagram", job_id="j1")
        second = pool.acquire_account("instagram", job_id="j2")
        assert first["account_id"] != second["account_id"]

    def test_acquire_fails_when_all_locked(self, pool) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.acquire_account("instagram", job_id="j1")
        with pytest.raises(AccountUnavailableError):
            pool.acquire_account("instagram", job_id="j2")

    def test_acquire_skips_cooldown(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.register_account("acc2", "instagram", {"u": "b"})
        # Manually place acc1 in cooldown.
        fake_redis.setex("account_cooldown:instagram:acc1", 60, "1")
        chosen = pool.acquire_account("instagram", job_id="j1")
        assert chosen["account_id"] == "acc2"

    def test_release_with_cooldown_sets_cooldown_key(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.acquire_account("instagram", job_id="j1")
        pool.release_account("acc1", "instagram", apply_cooldown=True)
        assert not fake_redis.exists("account_lock:instagram:acc1")
        assert fake_redis.exists("account_cooldown:instagram:acc1")

    def test_release_without_cooldown_returns_idle(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.acquire_account("instagram", job_id="j1")
        pool.release_account("acc1", "instagram", apply_cooldown=False)
        assert fake_redis.hget("account:instagram:acc1", "status") == "idle"
        assert not fake_redis.exists("account_cooldown:instagram:acc1")

    def test_acquire_no_accounts_registered(self, pool) -> None:
        with pytest.raises(AccountUnavailableError, match="No accounts registered"):
            pool.acquire_account("instagram", job_id="j1")


class TestMarkInvalid:
    def test_removes_from_pool_set(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.mark_invalid("acc1", "instagram", reason="banned")
        # Hash kept (forensics) but pool set cleaned.
        assert not fake_redis.sismember("accounts:instagram", "acc1")
        assert fake_redis.hget("account:instagram:acc1", "status") == "invalid"
        assert fake_redis.hget("account:instagram:acc1", "invalid_reason") == "banned"
        assert fake_redis.hexists("account:instagram:acc1", "invalidated_at")

    def test_releases_lock(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.acquire_account("instagram", job_id="j1")
        pool.mark_invalid("acc1", "instagram", reason="x")
        assert not fake_redis.exists("account_lock:instagram:acc1")

    def test_subsequent_acquire_skips_invalid(self, pool) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.register_account("acc2", "instagram", {"u": "b"})
        pool.mark_invalid("acc1", "instagram", reason="x")
        chosen = pool.acquire_account("instagram", job_id="j1")
        assert chosen["account_id"] == "acc2"


class TestStaleCleanup:
    def test_pool_set_pruned_when_hash_missing(
        self, pool, fake_redis, monkeypatch
    ) -> None:
        # Register two, then delete one's hash directly to simulate a stale id.
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.register_account("acc2", "instagram", {"u": "b"})
        fake_redis.delete("account:instagram:acc1")
        # Force acc1 to be tried first so the stale-id branch is exercised.
        monkeypatch.setattr(
            "account_pool.manager.random.shuffle",
            lambda ids: ids.sort(key=lambda x: 0 if x == "acc1" else 1),
        )

        # Acquire should still succeed via acc2 and prune acc1 from the set.
        chosen = pool.acquire_account("instagram", job_id="j1")
        assert chosen["account_id"] == "acc2"
        assert not fake_redis.sismember("accounts:instagram", "acc1")
        assert fake_redis.sismember("accounts:instagram", "acc2")


class TestCorruptCredentials:
    def test_unencrypted_credentials_marked_invalid(
        self, pool, fake_redis, monkeypatch
    ) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        pool.register_account("acc2", "instagram", {"u": "b"})
        # Overwrite acc1 with plaintext credentials (legacy / corrupt scenario).
        fake_redis.hset(
            "account:instagram:acc1", "credentials", '{"u":"plaintext"}'
        )
        # Force acc1 to be tried first so the corruption path is exercised.
        monkeypatch.setattr(
            "account_pool.manager.random.shuffle",
            lambda ids: ids.sort(key=lambda x: 0 if x == "acc1" else 1),
        )
        chosen = pool.acquire_account("instagram", job_id="j1")
        # acc2 should be selected; acc1 should now be marked invalid.
        assert chosen["account_id"] == "acc2"
        assert fake_redis.hget("account:instagram:acc1", "status") == "invalid"


class TestContextManager:
    def test_use_account_releases_on_exit(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        with pool.use_account("instagram", job_id="j1") as account:
            assert account["account_id"] == "acc1"
            assert fake_redis.exists("account_lock:instagram:acc1")
        assert not fake_redis.exists("account_lock:instagram:acc1")

    def test_use_account_releases_on_exception(self, pool, fake_redis) -> None:
        pool.register_account("acc1", "instagram", {"u": "a"})
        with pytest.raises(RuntimeError):
            with pool.use_account("instagram", job_id="j1"):
                raise RuntimeError("boom")
        assert not fake_redis.exists("account_lock:instagram:acc1")
