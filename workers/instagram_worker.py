"""Legacy compatibility shim — all execution is now handled by workers.executor."""
from workers.executor import execute_scrape_job


def scrape_instagram(username):
    return execute_scrape_job(
        job_id="legacy",
        username=username,
        platform="instagram",
    )
    retries = 3

    for attempt in range(retries):
        try:
            return asyncio.run(_scrape_instagram(username))
        except Exception as e:
            logger.warning(f"Retry {attempt+1} failed for {username}")

    logger.error(f"All retries failed for {username}")
    return {"error": "scraping_failed"}