# src/core/logging_config.py
"""
Structured JSON logging for production observability.

Replaces the scattered print() calls with proper log levels and
machine-parseable JSON output so logs can be ingested by any log aggregator
(Datadog, Loki, CloudWatch, etc.) without extra parsing.

Usage:
    from src.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("request_completed", extra={"request_id": "...", "latency_ms": 42})
"""
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    # Fields from LogRecord we exclude (they're redundant or internal)
    _SKIP = frozenset({
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs",
        "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    })

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach extra structured fields set by the caller
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                obj[key] = value
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def setup_logging(level: str = "INFO") -> None:
    """
    Configure structured JSON logging for the entire application.
    Call once at startup (in main.py lifespan).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JSONFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers = [handler]

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("supabase").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (shorthand for logging.getLogger)."""
    return logging.getLogger(name)
