"""Tests for the dispatcher's enqueue-failure rollback path."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import queues.dispatcher as dispatcher_module
from core.exceptions import DuplicateJobError
from core.job_state import _dedup_key, _job_key
from core.platforms import Platform


@pytest.fixture
def dispatch_env(fake_async_redis, monkeypatch, reset_platform_config):
    reset_platform_config(
        """
version: 1
platforms:
  instagram:
    workers: 1
    accounts: 1
    ips: 1
    max_concurrent_browsers: 1
    queue_name: scrape_instagram
"""
    )
    return fake_async_redis


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_dedup_and_pending_state(
    dispatch_env, monkeypatch
):
    """If RQ enqueue raises, the dedup slot must be released so retries work."""
    fake_async_redis = dispatch_env

    def _boom(*args, **kwargs):
        raise RuntimeError("rq down")

    monkeypatch.setattr(dispatcher_module, "_sync_enqueue", _boom)

    with pytest.raises(RuntimeError, match="rq down"):
        await dispatcher_module.dispatch_job(
            fake_async_redis, "alice", Platform.INSTAGRAM.value
        )

    # Dedup slot freed; pending job hash cleared.
    assert (
        await fake_async_redis.get(_dedup_key("instagram", "alice")) is None
    )
    # The exact job_id is internal but the dedup release ensures a retry can
    # claim a fresh slot:
    second = await fake_async_redis.get(_dedup_key("instagram", "alice"))
    assert second is None


@pytest.mark.asyncio
async def test_successful_dispatch_keeps_dedup_held(dispatch_env, monkeypatch):
    fake_async_redis = dispatch_env
    monkeypatch.setattr(
        dispatcher_module, "_sync_enqueue", lambda *a, **kw: None
    )
    job_id = await dispatcher_module.dispatch_job(
        fake_async_redis, "bob", Platform.INSTAGRAM.value
    )
    held = await fake_async_redis.get(_dedup_key("instagram", "bob"))
    assert held == job_id


@pytest.mark.asyncio
async def test_duplicate_request_returns_existing_id(dispatch_env, monkeypatch):
    fake_async_redis = dispatch_env
    monkeypatch.setattr(
        dispatcher_module, "_sync_enqueue", lambda *a, **kw: None
    )
    first = await dispatcher_module.dispatch_job(
        fake_async_redis, "carol", Platform.INSTAGRAM.value
    )
    with pytest.raises(DuplicateJobError, match=first):
        await dispatcher_module.dispatch_job(
            fake_async_redis, "carol", Platform.INSTAGRAM.value
        )
