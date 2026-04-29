import json
import logging
import sys
from datetime import datetime, timezone

from core.config import Config


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter with request tracing support."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach trace fields if present
        for field in ("job_id", "platform", "username", "account_id"):
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value

        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with structured JSON output."""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
        logger.propagate = False

    return logger
