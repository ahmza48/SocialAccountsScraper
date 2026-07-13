import os

from core.platforms import Platform


class Config:
    """Central configuration loaded from environment variables."""

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_MAX_CONNECTIONS: int = int(os.getenv("REDIS_MAX_CONNECTIONS", "100"))
    REDIS_SOCKET_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", "5.0"))
    REDIS_SOCKET_CONNECT_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "5.0"))

    # Workers
    MAX_CONCURRENT_BROWSERS: int = int(os.getenv("MAX_CONCURRENT_BROWSERS", "8"))
    JOB_TIMEOUT_SECONDS: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "120"))

    # Cache
    # Default raised from 600s → 3600s to match CURSOR_TTL_SECONDS so a
    # client walking signed cursors keeps hitting the cache for as long as
    # those cursors stay valid (see core.security). Setting this lower than
    # CURSOR_TTL_SECONDS is allowed but emits a startup warning because it
    # means page-2 requests can re-scrape with shifted underlying data.
    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
    CACHE_LOCK_TIMEOUT: int = int(os.getenv("CACHE_LOCK_TIMEOUT", "10"))

    # Anti-detection
    MIN_DELAY_SECONDS: float = float(os.getenv("MIN_DELAY_SECONDS", "1.0"))
    MAX_DELAY_SECONDS: float = float(os.getenv("MAX_DELAY_SECONDS", "3.0"))
    ACCOUNT_COOLDOWN_SECONDS: int = int(os.getenv("ACCOUNT_COOLDOWN_SECONDS", "300"))

    # Retry
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

    # Rate limiting
    RATE_LIMIT_REQUESTS: int = int(os.getenv("RATE_LIMIT_REQUESTS", "10"))
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

    # Sessions
    SESSION_STORAGE_DIR: str = os.getenv("SESSION_STORAGE_DIR", "sessions/storage")

    # Queues — derived from the Platform enum so adding a platform updates this list.
    SUPPORTED_PLATFORMS: list = Platform.values()
    QUEUE_MAX_LENGTH: int = int(os.getenv("QUEUE_MAX_LENGTH", "1000"))

    # Circuit breaker
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "10"))
    CIRCUIT_BREAKER_WINDOW_SECONDS: int = int(os.getenv("CIRCUIT_BREAKER_WINDOW_SECONDS", "60"))
    CIRCUIT_BREAKER_RECOVERY_SECONDS: int = int(os.getenv("CIRCUIT_BREAKER_RECOVERY_SECONDS", "30"))

    # Job state
    JOB_RESULT_TTL_SECONDS: int = int(os.getenv("JOB_RESULT_TTL_SECONDS", "3600"))
    JOB_DEDUP_TTL_SECONDS: int = int(os.getenv("JOB_DEDUP_TTL_SECONDS", "300"))

    # Dead-letter queue
    # Hard cap on the per-platform DLQ list. Older entries are trimmed away
    # in the same pipeline as the push so we never silently grow unbounded.
    # Total dropped events are tracked via metrics:dlq:{platform}:dropped.
    DLQ_MAX_LENGTH: int = int(os.getenv("DLQ_MAX_LENGTH", "10000"))

    # Platform config
    PLATFORM_CONFIG_PATH: str = os.getenv("PLATFORM_CONFIG_PATH", "platform_config.yml")

    # Security
    # HMAC key for signing pagination cursors. REQUIRED in production; if
    # unset a per-process random key is generated (see core.security).
    CURSOR_SIGNING_KEY: str = os.getenv("CURSOR_SIGNING_KEY", "")
    # Bearer token for /metrics. If unset, /metrics is denied to all callers
    # (fail-closed). Set to a long random secret in production.
    METRICS_AUTH_TOKEN: str = os.getenv("METRICS_AUTH_TOKEN", "")
    # Bearer token for /admin/*. If unset, admin endpoints are denied to all
    # callers (fail-closed). MUST be distinct from METRICS_AUTH_TOKEN so a
    # leaked metrics token cannot mutate accounts or jobs.
    ADMIN_AUTH_TOKEN: str = os.getenv("ADMIN_AUTH_TOKEN", "")
    # TTL for signed cursor tokens (seconds). After this clients must re-issue
    # the original search to obtain fresh pagination tokens.
    CURSOR_TTL_SECONDS: int = int(os.getenv("CURSOR_TTL_SECONDS", "3600"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
