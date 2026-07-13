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
import signal

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis
from rq import SimpleWorker, Queue

from core.config import Config
from core.redis import get_rq_connection
from core.logging_config import get_logger
from core.platform_config import get_platform_config

logger = get_logger(__name__)


def _install_signal_handlers(worker: SimpleWorker) -> None:
    """Install graceful-shutdown handlers for SIGTERM and SIGINT.

    On the first signal we ask RQ to stop after the current job finishes so
    Playwright contexts, account locks, and browser-slot counters get released
    cleanly. A second signal triggers an immediate exit (covers stuck jobs).
    """
    shutdown_requested = {"count": 0}

    def _handle(signum: int, _frame: object) -> None:
        shutdown_requested["count"] += 1
        if shutdown_requested["count"] == 1:
            logger.info(
                f"Received signal {signum}; finishing current job then exiting. "
                f"Send the signal again to force-exit."
            )
            try:
                worker.request_stop(signum, _frame)
            except Exception:
                logger.exception("request_stop() failed; falling back to sys.exit")
                sys.exit(1)
        else:
            logger.warning(
                f"Received signal {signum} again; forcing immediate exit."
            )
            sys.exit(1)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


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
    _install_signal_handlers(worker)
    # Disable RQ's own signal handlers so ours stay in effect; we still want
    # the worker's normal job-execution loop, just not its abrupt shutdown.
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
