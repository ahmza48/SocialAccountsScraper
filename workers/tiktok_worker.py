"""Legacy compatibility shim — all execution is now handled by workers.executor."""
from workers.executor import execute_scrape_job


def scrape_tiktok(username):
    return execute_scrape_job(
        job_id="legacy",
        username=username,
        platform="tiktok",
    )