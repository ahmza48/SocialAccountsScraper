"""Worker entry point with per-platform support.

Usage:
    # Listen on a specific platform (recommended for production):
    python -m workers.runner --platform instagram
    WORKER_PLATFORM=instagram python -m workers.runner

    # Listen on all platform queues (dev mode):
    python -m workers.runner

    # Or using rq directly:
    rq worker scrape_instagram scrape_tiktok scrape_facebook
"""
import sys
import os
import argparse

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis
from rq import SimpleWorker, Queue

from core.config import Config
from core.redis import get_rq_connection
from core.logging_config import get_logger
from core.platform_config import get_platform_config

logger = get_logger(__name__)


def start_worker(platform: str = None) -> None:
    """Start an RQ worker listening on platform-specific queues."""
    pc = get_platform_config()
    conn = get_rq_connection()

    # Resolve platform from arg → env var → all
    platform = platform or os.getenv("WORKER_PLATFORM")

    if platform:
        queue_names = [pc.queue_name(platform)]
    else:
        queue_names = [pc.queue_name(p) for p in pc.platforms]

    queues = [Queue(name, connection=conn) for name in queue_names]

    logger.info(f"Starting worker — listening on queues: {queue_names}")

    worker = SimpleWorker(queues, connection=conn)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    pc = get_platform_config()
    parser = argparse.ArgumentParser(description="Start scraper worker")
    parser.add_argument(
        "--platform",
        default=None,
        choices=pc.platforms,
        help="Platform to process (default: all, or set WORKER_PLATFORM env)",
    )
    args = parser.parse_args()
    start_worker(args.platform)
