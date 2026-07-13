"""Tests for /admin/* endpoints — auth, accounts, DLQ, job cancel."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import redis as core_redis
from core.platform_config import reset_platform_config


PLATFORM_YAML = """
version: 1
platforms:
  instagram:
    workers: 2
    accounts: 4
    ips: 4
    max_concurrent_browsers: 2
    queue_name: scrape_instagram
"""


@pytest.fixture
def admin_token(monkeypatch):
    token = "test-admin-token"
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", token)
    return token


@pytest.fixture
def configured_platforms(reset_platform_config):
    reset_platform_config(PLATFORM_YAML)
    yield
    # reset_platform_config fixture handles teardown.


@pytest.fixture
def shared_redis():
    """Sync + async fakeredis instances backed by a single in-memory server.

    The admin router uses sync managers (Account pool, DLQ) but reads via the
    async client; tests must observe both writes and reads on the same store.
    """
    import fakeredis
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    async_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        yield sync, async_client
    finally:
        sync.flushall()
        sync.close()


@pytest.fixture
def client(shared_redis, monkeypatch, fernet_key, configured_platforms):
    """TestClient wired so sync handlers and async reads share one fake store."""
    sync, async_client = shared_redis

    async def _get_async():
        return async_client

    def _get_sync(*_a, **_kw):
        return sync

    monkeypatch.setattr(core_redis, "get_async_redis", _get_async)
    monkeypatch.setattr(core_redis, "get_sync_redis", _get_sync)
    import api.main as api_main
    import api.admin as api_admin

    monkeypatch.setattr(api_main, "get_async_redis", _get_async)
    monkeypatch.setattr(api_admin, "get_async_redis", _get_async)
    monkeypatch.setattr(api_admin, "get_sync_redis", _get_sync)
    return TestClient(app)


@pytest.fixture
def fake_redis_shared(shared_redis):
    """Convenience handle to the sync side of the shared fake store."""
    sync, _ = shared_redis
    return sync


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Auth gate ────────────────────────────────────────────────────


def test_admin_routes_require_token(client):
    resp = client.get("/admin/accounts/instagram")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_admin_rejects_wrong_token(client, admin_token):
    resp = client.get(
        "/admin/accounts/instagram", headers=_auth("not-the-real-token")
    )
    assert resp.status_code == 401


def test_admin_rejects_when_token_env_unset(client, monkeypatch):
    """Fail-closed: no env var → every request denied even with a token header."""
    monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
    resp = client.get("/admin/accounts/instagram", headers=_auth("anything"))
    assert resp.status_code == 401


# ── Account registration ────────────────────────────────────────


def test_register_account_encrypts_credentials(client, admin_token, fake_redis_shared):
    body = {
        "account_id": "acct-1",
        "platform": "instagram",
        "credentials": {"username": "u", "password": "p"},
        "proxy": "http://proxy.local:8080",
    }
    resp = client.post("/admin/accounts", json=body, headers=_auth(admin_token))
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "registered"

    # Hash exists and credentials are not stored as plaintext.
    stored = fake_redis_shared.hgetall("account:instagram:acct-1")
    assert stored["account_id"] == "acct-1"
    assert stored["status"] == "idle"
    assert stored["credentials"].startswith("ENC:v1:")
    assert "password" not in stored["credentials"]
    # Pool set updated.
    assert fake_redis_shared.sismember("accounts:instagram", "acct-1")


def test_register_rejects_unknown_platform(client, admin_token):
    body = {
        "account_id": "x",
        "platform": "myspace",
        "credentials": {"u": "v"},
    }
    resp = client.post("/admin/accounts", json=body, headers=_auth(admin_token))
    assert resp.status_code == 422  # pydantic validation error before reaching handler


def test_register_rejects_oversize_credentials(client, admin_token):
    body = {
        "account_id": "big",
        "platform": "instagram",
        "credentials": {"k": "x" * 5000},
    }
    resp = client.post("/admin/accounts", json=body, headers=_auth(admin_token))
    assert resp.status_code == 422


def test_register_rejects_bad_account_id(client, admin_token):
    body = {
        "account_id": "bad id with spaces!",
        "platform": "instagram",
        "credentials": {"u": "v"},
    }
    resp = client.post("/admin/accounts", json=body, headers=_auth(admin_token))
    assert resp.status_code == 422


# ── Pool status ─────────────────────────────────────────────────


def test_pool_status_returns_zero_when_empty(client, admin_token):
    resp = client.get("/admin/accounts/instagram", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "platform": "instagram",
        "total": 0,
        "idle": 0,
        "in_use": 0,
        "cooldown": 0,
        "invalid": 0,
    }


def test_pool_status_after_registration(client, admin_token):
    client.post(
        "/admin/accounts",
        json={
            "account_id": "a1",
            "platform": "instagram",
            "credentials": {"u": "v"},
        },
        headers=_auth(admin_token),
    )
    resp = client.get("/admin/accounts/instagram", headers=_auth(admin_token))
    body = resp.json()
    assert body["total"] == 1
    assert body["idle"] == 1


def test_pool_status_unknown_platform_404(client, admin_token):
    resp = client.get("/admin/accounts/myspace", headers=_auth(admin_token))
    assert resp.status_code == 400  # parse_platform fails first


# ── Invalidate ─────────────────────────────────────────────────


def test_invalidate_account_removes_from_pool_set(client, admin_token, fake_redis_shared):
    client.post(
        "/admin/accounts",
        json={
            "account_id": "to-kill",
            "platform": "instagram",
            "credentials": {"u": "v"},
        },
        headers=_auth(admin_token),
    )
    resp = client.post(
        "/admin/accounts/instagram/to-kill/invalidate",
        json={"reason": "blocked"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"
    assert not fake_redis_shared.sismember("accounts:instagram", "to-kill")
    stored = fake_redis_shared.hgetall("account:instagram:to-kill")
    assert stored["status"] == "invalid"
    assert stored["invalid_reason"] == "blocked"


def test_invalidate_unknown_account_404(client, admin_token):
    resp = client.post(
        "/admin/accounts/instagram/missing/invalidate",
        json={"reason": "x"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 404


# ── DLQ ────────────────────────────────────────────────────────


def _push_dlq(fake_redis_shared, platform: str, n: int = 3) -> list:
    """Seed DLQ via the production API path so tests cover the real write code."""
    from queues.dead_letter import DeadLetterQueue

    dlq = DeadLetterQueue(redis_client=fake_redis_shared)
    entries = []
    for i in range(n):
        dlq.push(
            job_id=f"job-{i}",
            platform=platform,
            username=f"user{i}",
            error="boom",
            attempts=3,
        )
        entries.append({"job_id": f"job-{i}", "platform": platform})
    return entries


def test_dlq_list_global(client, admin_token, fake_redis_shared):
    _push_dlq(fake_redis_shared, "instagram", n=3)
    resp = client.get("/admin/dlq", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["entries"]) == 3
    assert body["platform"] is None


def test_dlq_list_per_platform(client, admin_token, fake_redis_shared):
    _push_dlq(fake_redis_shared, "instagram", n=2)
    resp = client.get(
        "/admin/dlq?platform=instagram&count=10",
        headers=_auth(admin_token),
    )
    body = resp.json()
    assert body["platform"] == "instagram"
    assert body["total"] == 2


def test_dlq_skips_corrupt_entries(client, admin_token, fake_redis_shared):
    # One unparseable entry, one valid entry — pushed via the raw key to
    # simulate corruption that bypassed the normal push() path.
    fake_redis_shared.lpush("dlq:failed_jobs:instagram", "{not json")
    fake_redis_shared.lpush(
        "dlq:failed_jobs:instagram",
        json.dumps({
            "job_id": "ok",
            "platform": "instagram",
            "username": "u",
            "error": "e",
            "attempts": 1,
            "failed_at": 0.0,
        }),
    )
    resp = client.get(
        "/admin/dlq?platform=instagram", headers=_auth(admin_token)
    )
    body = resp.json()
    assert body["total"] == 2
    # Only the parseable entry comes through.
    assert len(body["entries"]) == 1
    assert body["entries"][0]["job_id"] == "ok"


def test_dlq_pagination_bounds(client, admin_token):
    resp = client.get("/admin/dlq?count=999", headers=_auth(admin_token))
    assert resp.status_code == 400


# ── Job cancel ─────────────────────────────────────────────────


def _seed_job(fake_redis_shared, job_id: str, platform: str, username: str) -> None:
    fake_redis_shared.hset(
        f"job:{job_id}",
        mapping={
            "status": "pending",
            "platform": platform,
            "username": username,
            "result": "",
            "error": "",
        },
    )
    fake_redis_shared.set(f"job:active:{platform}:{username}", job_id)


def test_cancel_job_clears_state_and_dedup(client, admin_token, fake_redis_shared):
    _seed_job(fake_redis_shared, "j1", "instagram", "alice")
    resp = client.delete("/admin/jobs/j1", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"job_id": "j1", "cancelled": True, "dedup_cleared": True}
    assert not fake_redis_shared.exists("job:j1")
    assert not fake_redis_shared.exists("job:active:instagram:alice")


def test_cancel_unknown_job_404(client, admin_token):
    resp = client.delete("/admin/jobs/does-not-exist", headers=_auth(admin_token))
    assert resp.status_code == 404


def test_cancel_rejects_oversize_id(client, admin_token):
    resp = client.delete(
        "/admin/jobs/" + ("x" * 200), headers=_auth(admin_token)
    )
    assert resp.status_code == 400
