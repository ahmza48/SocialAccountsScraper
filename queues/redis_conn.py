"""Shared Redis connection for queue system.

Prefer using core.redis module directly:
    from core.redis import get_sync_redis, get_rq_connection

This module is kept for backward compatibility.
"""
from core.redis import get_sync_redis, get_rq_connection

redis_conn = get_rq_connection()