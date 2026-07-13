import os

from core.platforms import Platform


def _int_env(name: str, default: str, *, min_value: int = None) -> int:
    """Read an int env var, failing fast if it violates ``min_value``.

    Mirrors the fail-fast validation ``platform_config.py`` already applies
    to its own numeric fields — a mistyped ``0`` or negative value should
    raise a clear error at import time, not surface as a confusing runtime
    failure deep inside RQ/the rate limiter/the circuit breaker.
    """
    value = int(os.getenv(name, default))
    if min_value is not None and value < min_value:
        raise ValueError(
            f"{name}={value} is invalid: must be >= {min_value}"
        )
    return value


def _float_env(name: str, default: str, *, min_value: float = None) -> float:
    value = float(os.getenv(name, default))
    if min_value is not None and value < min_value:
        raise ValueError(
            f"{name}={value} is invalid: must be >= {min_value}"
        )
    return value


class Config:
    """Central configuration loaded from environment variables."""

    # Environment
    # "production" enables stricter startup checks (e.g. requiring
    # CURSOR_SIGNING_KEY to be set) that would be too disruptive for local
    # dev/test, where a per-process random key is an acceptable fallback.
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_MAX_CONNECTIONS: int = _int_env("REDIS_MAX_CONNECTIONS", "100", min_value=1)
    REDIS_SOCKET_TIMEOUT: float = _float_env("REDIS_SOCKET_TIMEOUT", "5.0", min_value=0.001)
    REDIS_SOCKET_CONNECT_TIMEOUT: float = _float_env("REDIS_SOCKET_CONNECT_TIMEOUT", "5.0", min_value=0.001)

    # Workers
    MAX_CONCURRENT_BROWSERS: int = _int_env("MAX_CONCURRENT_BROWSERS", "8", min_value=1)
    JOB_TIMEOUT_SECONDS: int = _int_env("JOB_TIMEOUT_SECONDS", "120", min_value=1)

    # Cache
    # Default raised from 600s → 3600s to match CURSOR_TTL_SECONDS so a
    # client walking signed cursors keeps hitting the cache for as long as
    # those cursors stay valid (see core.security). Setting this lower than
    # CURSOR_TTL_SECONDS is allowed but emits a startup warning because it
    # means page-2 requests can re-scrape with shifted underlying data.
    CACHE_TTL_SECONDS: int = _int_env("CACHE_TTL_SECONDS", "3600", min_value=1)
    CACHE_LOCK_TIMEOUT: int = _int_env("CACHE_LOCK_TIMEOUT", "10", min_value=1)

    # Anti-detection
    MIN_DELAY_SECONDS: float = _float_env("MIN_DELAY_SECONDS", "1.0", min_value=0.0)
    MAX_DELAY_SECONDS: float = _float_env("MAX_DELAY_SECONDS", "3.0", min_value=0.0)
    ACCOUNT_COOLDOWN_SECONDS: int = _int_env("ACCOUNT_COOLDOWN_SECONDS", "300", min_value=0)

    # Retry
    MAX_RETRIES: int = _int_env("MAX_RETRIES", "3", min_value=0)
    RETRY_BASE_DELAY: float = _float_env("RETRY_BASE_DELAY", "1.0", min_value=0.0)

    # Rate limiting
    RATE_LIMIT_REQUESTS: int = _int_env("RATE_LIMIT_REQUESTS", "10", min_value=1)
    RATE_LIMIT_WINDOW_SECONDS: int = _int_env("RATE_LIMIT_WINDOW_SECONDS", "60", min_value=1)

    # Sessions
    SESSION_STORAGE_DIR: str = os.getenv("SESSION_STORAGE_DIR", "sessions/storage")

    # Queues — derived from the Platform enum so adding a platform updates this list.
    SUPPORTED_PLATFORMS: list = Platform.values()
    QUEUE_MAX_LENGTH: int = _int_env("QUEUE_MAX_LENGTH", "1000", min_value=1)

    # Circuit breaker
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = _int_env("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "10", min_value=1)
    CIRCUIT_BREAKER_WINDOW_SECONDS: int = _int_env("CIRCUIT_BREAKER_WINDOW_SECONDS", "60", min_value=1)
    CIRCUIT_BREAKER_RECOVERY_SECONDS: int = _int_env("CIRCUIT_BREAKER_RECOVERY_SECONDS", "30", min_value=1)

    # Job state
    JOB_RESULT_TTL_SECONDS: int = _int_env("JOB_RESULT_TTL_SECONDS", "3600", min_value=1)
    JOB_DEDUP_TTL_SECONDS: int = _int_env("JOB_DEDUP_TTL_SECONDS", "300", min_value=1)

    # Dead-letter queue
    # Hard cap on the per-platform DLQ list. Older entries are trimmed away
    # in the same pipeline as the push so we never silently grow unbounded.
    # Total dropped events are tracked via metrics:dlq:{platform}:dropped.
    DLQ_MAX_LENGTH: int = _int_env("DLQ_MAX_LENGTH", "10000", min_value=1)

    # Platform config
    PLATFORM_CONFIG_PATH: str = os.getenv("PLATFORM_CONFIG_PATH", "platform_config.yml")

    # Security
    # HMAC key for signing pagination cursors. REQUIRED in production; if
    # unset a per-process random key is generated (see core.security). When
    # ENVIRONMENT=production this is enforced at startup (api/main.py).
    CURSOR_SIGNING_KEY: str = os.getenv("CURSOR_SIGNING_KEY", "")
    # Bearer token for /metrics. If unset, /metrics is denied to all callers
    # (fail-closed). Set to a long random secret in production.
    METRICS_AUTH_TOKEN: str = os.getenv("METRICS_AUTH_TOKEN", "")
    # Bearer token for /admin/*. If unset, admin endpoints are denied to all
    # callers (fail-closed). MUST be distinct from METRICS_AUTH_TOKEN so a
    # leaked metrics token cannot mutate accounts/jobs.
    ADMIN_AUTH_TOKEN: str = os.getenv("ADMIN_AUTH_TOKEN", "")
    # TTL for signed cursor tokens (seconds). After this clients must re-issue
    # the original search to obtain fresh pagination tokens.
    CURSOR_TTL_SECONDS: int = _int_env("CURSOR_TTL_SECONDS", "3600", min_value=1)

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


if Config.MAX_DELAY_SECONDS < Config.MIN_DELAY_SECONDS:
    raise ValueError(
        f"MAX_DELAY_SECONDS ({Config.MAX_DELAY_SECONDS}) must be >= "
        f"MIN_DELAY_SECONDS ({Config.MIN_DELAY_SECONDS})"
    )
