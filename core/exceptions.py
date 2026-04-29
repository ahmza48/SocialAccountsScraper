class ScraperBaseError(Exception):
    """Base exception for the scraping system."""
    pass


class SessionError(ScraperBaseError):
    """Session-related errors (expired, invalid, etc.)."""
    pass


class SessionExpiredError(SessionError):
    """Session has expired and needs refresh."""
    pass


class SessionInvalidError(SessionError):
    """Session is permanently invalid."""
    pass


class AccountError(ScraperBaseError):
    """Account-related errors."""
    pass


class AccountBlockedError(AccountError):
    """Account has been blocked by the platform."""
    pass


class AccountCooldownError(AccountError):
    """Account is in cooldown period."""
    pass


class AccountUnavailableError(AccountError):
    """No accounts available for allocation."""
    pass


class ScrapingError(ScraperBaseError):
    """Errors during the actual scraping process."""
    pass


class ParsingError(ScrapingError):
    """Failed to parse scraped data."""
    pass


class QueueFullError(ScraperBaseError):
    """Queue has exceeded max capacity (backpressure)."""
    pass


class DuplicateJobError(ScraperBaseError):
    """A job for this username+platform is already active."""
    pass


class CircuitOpenError(ScraperBaseError):
    """Circuit breaker is open — platform temporarily unavailable."""
    pass


class BrowserLimitError(ScraperBaseError):
    """Browser concurrency limit reached."""
    pass


class PlatformError(ScrapingError):
    """Platform-specific transient errors."""
    pass


class RateLimitError(ScraperBaseError):
    """Rate limit exceeded."""
    pass


class QueueFullError(ScraperBaseError):
    """Queue at maximum capacity."""
    pass


class DuplicateJobError(ScraperBaseError):
    """Job already exists for this request."""
    pass
