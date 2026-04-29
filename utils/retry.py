import random
import time
from functools import wraps
from typing import Tuple, Type

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import ParsingError

logger = get_logger(__name__)


def exponential_backoff(
    max_retries: int = None,
    base_delay: float = None,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    non_retryable_exceptions: Tuple[Type[Exception], ...] = (ParsingError,),
):
    """Decorator for retry with exponential backoff and jitter."""
    if max_retries is None:
        max_retries = Config.MAX_RETRIES
    if base_delay is None:
        base_delay = Config.RETRY_BASE_DELAY

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except non_retryable_exceptions as e:
                    logger.error(
                        f"Non-retryable error in {func.__name__}: {e}",
                        exc_info=True,
                    )
                    raise
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for "
                            f"{func.__name__}: {e}. Retrying in {delay:.1f}s"
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"All {max_retries + 1} attempts exhausted for "
                            f"{func.__name__}: {e}"
                        )
            raise last_exception

        return wrapper

    return decorator


class RetryContext:
    """Stateful retry controller for the executor retry loop.

    Failure categories:
        - ParsingError           → never retry (data structure issue)
        - AccountBlockedError    → retryable (try different account)
        - SessionExpiredError    → retryable (try different account)
        - BrowserLimitError      → retryable (wait for slot)
        - ScrapingError / other  → retryable with backoff
    """

    def __init__(self, max_retries: int = None, base_delay: float = None):
        self.max_retries: int = max_retries or Config.MAX_RETRIES
        self.base_delay: float = base_delay or Config.RETRY_BASE_DELAY
        self.attempt: int = 0

    def should_retry(self, exception: Exception) -> bool:
        """Returns True if another attempt should be made."""
        if isinstance(exception, ParsingError):
            return False
        self.attempt += 1
        return self.attempt <= self.max_retries

    def wait(self) -> None:
        """Sleep with exponential backoff + jitter before the next attempt."""
        delay = self.base_delay * (2 ** (self.attempt - 1)) + random.uniform(0, 1)
        logger.info(f"Retry wait: {delay:.1f}s (attempt {self.attempt})")
        time.sleep(delay)
        time.sleep(delay)
