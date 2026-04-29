import random
import time

from core.config import Config


def human_delay(min_seconds: float = None, max_seconds: float = None):
    """Sleep for a randomized human-like duration."""
    min_s = min_seconds or Config.MIN_DELAY_SECONDS
    max_s = max_seconds or Config.MAX_DELAY_SECONDS
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def jittered_delay(base_seconds: float, jitter_factor: float = 0.5):
    """Sleep with jitter around a base duration."""
    jitter = base_seconds * jitter_factor * random.uniform(-1, 1)
    delay = max(0.1, base_seconds + jitter)
    time.sleep(delay)


def random_viewport() -> dict:
    """Return a randomized but realistic viewport size."""
    viewports = [
        {"width": 1920, "height": 1080},
        {"width": 1366, "height": 768},
        {"width": 1440, "height": 900},
        {"width": 1536, "height": 864},
        {"width": 1280, "height": 720},
    ]
    return random.choice(viewports)


def random_user_agent() -> str:
    """Return a randomized modern user agent string."""
    agents = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    ]
    return random.choice(agents)
