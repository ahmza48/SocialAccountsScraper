"""Tests for /health and /readyz, including the write/read probe."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import redis as core_redis


@pytest.fixture
def client(fake_async_redis, monkeypatch):
    """FastAPI test client with the async Redis pool replaced by fakeredis."""
    async def _get_async():
        return fake_async_redis

    monkeypatch.setattr(core_redis, "get_async_redis", _get_async)
    # Some modules already imported the symbol — patch their module-level ref too.
    import api.main as api_main

    monkeypatch.setattr(api_main, "get_async_redis", _get_async)
    return TestClient(app)


def test_health_reports_ok_when_redis_responds(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["redis"] == "connected"
    assert body["redis_writable"] is True


def test_readyz_returns_ok_when_redis_writable(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_returns_503_when_ping_fails(client, fake_async_redis, monkeypatch):
    async def _boom(*_a, **_kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_async_redis, "ping", _boom)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["redis_ping"] is False
    assert detail["redis_writable"] is False


def test_health_degraded_when_writes_fail(client, fake_async_redis, monkeypatch):
    """Ping succeeds but SET/GET/DEL round-trip fails → status=degraded, 200."""
    async def _set_boom(*_a, **_kw):
        raise ConnectionError("readonly replica")

    monkeypatch.setattr(fake_async_redis, "set", _set_boom)
    resp = client.get("/health")
    # /health is liveness — never 5xx.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["redis"] == "connected"
    assert body["redis_writable"] is False
