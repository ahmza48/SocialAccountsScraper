"""Centralized Redis connection management.

Provides async pool (API layer) and sync pool (worker layer)
with configurable connection limits and timeouts.
"""
from typing import Optional

import redis
import redis.asyncio as aioredis

from core.config import Config

_async_pool: Optional[aioredis.Redis] = None
_sync_pool: Optional[redis.Redis] = None


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
    """Get raw Redis connection for RQ (no decode_responses)."""
    return redis.from_url(
        Config.REDIS_URL,
        socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
    )
