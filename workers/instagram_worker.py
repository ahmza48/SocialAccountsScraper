"""Legacy compatibility shim — all execution is now handled by workers.executor.

Deprecated. New code should call ``workers.executor.execute_scrape_job`` directly
with a real ``job_id`` produced by the dispatcher.
"""
import uuid

from workers.executor import execute_scrape_job


def scrape_instagram(username: str, cursor: str = None) -> dict:
    """Legacy entry point. Generates a one-off job id and dispatches synchronously."""
    return execute_scrape_job(
        job_id=f"legacy-{uuid.uuid4()}",
        username=username,
        platform="instagram",
        cursor=cursor,
    )