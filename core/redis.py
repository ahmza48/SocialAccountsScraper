"""Centralized Redis connection management.

Provides async pool (API layer) and sync pool (worker layer)
with configurable connection limits and timeouts.
"""
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import redis
import redis.asyncio as aioredis

from core.config import Config

_async_pool: Optional[aioredis.Redis] = None
_sync_pool: Optional[redis.Redis] = None
_rq_pool: Optional[redis.Redis] = None


@dataclass(frozen=True)
class RedisHealth:
    """Result of an end-to-end Redis health probe.

    ``ok`` is the overall verdict (PING + write/read round-trip succeeded).
    ``ping_ok`` and ``write_ok`` allow callers (e.g. /readyz) to distinguish
    a hard-down Redis from a read-only replica or quota-throttled cluster.
    ``error`` carries the last exception class name for log/metric tagging
    without leaking server internals to clients.
    """

    ok: bool
    ping_ok: bool
    write_ok: bool
    error: Optional[str] = None


async def get_async_redis() -> aioredis.Redis:
    """Get async Redis client with connection pool for the API layer."""
    global _async_pool
    if _async_pool is None:
        _async_pool = aioredis.from_url(
            Config.REDIS_URL,
            decode_responses=True,
            max_connections=Config.REDIS_MAX_CONNECTIONS,
            socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
        )
    return _async_pool


async def close_async_redis() -> None:
    """Close async Redis pool gracefully."""
    global _async_pool
    if _async_pool is not None:
        await _async_pool.aclose()
        _async_pool = None


def get_sync_redis(decode_responses: bool = True) -> redis.Redis:
    """Get sync Redis client with connection pool for the worker layer."""
    global _sync_pool
    if not decode_responses:
        return redis.from_url(
            Config.REDIS_URL,
            decode_responses=False,
            max_connections=Config.REDIS_MAX_CONNECTIONS,
            socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
        )
    if _sync_pool is None:
        _sync_pool = redis.from_url(
            Config.REDIS_URL,
            decode_responses=True,
            max_connections=Config.REDIS_MAX_CONNECTIONS,
            socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
        )
    return _sync_pool


def get_rq_connection() -> redis.Redis:
    """Get the shared raw Redis connection pool for RQ (no decode_responses).

    Cached as a singleton and bounded by ``REDIS_MAX_CONNECTIONS``, matching
    ``get_sync_redis``/``get_async_redis``. Previously this constructed a
    brand-new, unbounded connection pool on every call; that was safe only
    because every call site happened to cache the result itself, with no
    guarantee enforced here — a future per-request caller would have opened
    an unbounded pool per call, risking exhausting Redis's ``maxclients``
    under load.
    """
    global _rq_pool
    if _rq_pool is None:
        _rq_pool = redis.from_url(
            Config.REDIS_URL,
            max_connections=Config.REDIS_MAX_CONNECTIONS,
            socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
        )
    return _rq_pool


# ── Health probe ──────────────────────────────────────────────────


# Short TTL on the probe key so an unhealthy DEL still reaps the value
# without leaving debris behind.
_HEALTH_PROBE_TTL_SECONDS = 5


async def async_redis_health(client: aioredis.Redis) -> RedisHealth:
    """Probe an async Redis client with PING + write/read round-trip.

    A bare ``PING`` only proves the socket is alive; it does not catch
    read-only replicas, write-quota throttling, or eviction storms that
    silently drop SET commands. We follow up with a SETEX/GET/DEL on a
    namespaced random key so /readyz reflects the state callers actually
    need.
    """
    ping_ok = False
    write_ok = False
    error: Optional[str] = None
    try:
        ping_ok = bool(await client.ping())
    except Exception as exc:  # pragma: no cover - exercised via fakeredis injection
        error = type(exc).__name__
        return RedisHealth(ok=False, ping_ok=False, write_ok=False, error=error)

    probe_key = f"health:probe:{uuid4().hex}"
    expected = "1"
    try:
        await client.set(probe_key, expected, ex=_HEALTH_PROBE_TTL_SECONDS)
        observed = await client.get(probe_key)
        await client.delete(probe_key)
        write_ok = observed == expected
    except Exception as exc:
        error = type(exc).__name__

    return RedisHealth(
        ok=ping_ok and write_ok,
        ping_ok=ping_ok,
        write_ok=write_ok,
        error=error,
    )
