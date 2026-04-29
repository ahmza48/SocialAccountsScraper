import os


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
    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "600"))
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

    # Queues
    SUPPORTED_PLATFORMS: list = ["instagram", "tiktok", "facebook"]
    QUEUE_MAX_LENGTH: int = int(os.getenv("QUEUE_MAX_LENGTH", "1000"))

    # Circuit breaker
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "10"))
    CIRCUIT_BREAKER_WINDOW_SECONDS: int = int(os.getenv("CIRCUIT_BREAKER_WINDOW_SECONDS", "60"))
    CIRCUIT_BREAKER_RECOVERY_SECONDS: int = int(os.getenv("CIRCUIT_BREAKER_RECOVERY_SECONDS", "30"))

    # Job state
    JOB_RESULT_TTL_SECONDS: int = int(os.getenv("JOB_RESULT_TTL_SECONDS", "3600"))
    JOB_DEDUP_TTL_SECONDS: int = int(os.getenv("JOB_DEDUP_TTL_SECONDS", "300"))

    # Platform config
    PLATFORM_CONFIG_PATH: str = os.getenv("PLATFORM_CONFIG_PATH", "platform_config.yml")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
