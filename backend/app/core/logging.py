"""Structured JSON logging with defensive secret masking.

Safety rules (spec section 19):
- The RailCore API key is never logged deliberately.
- As defense in depth, a regex filter masks any ``rk_live_...`` token that
  might accidentally reach a log line from a third-party logger.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_SECRET_PATTERN = re.compile(r"rk_live_[A-Za-z0-9]+")


class JsonFormatter(logging.Formatter):
    """Minimal JSON line formatter for structured logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return _SECRET_PATTERN.sub("rk_live_[REDACTED]", line)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with the JSON formatter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Third-party HTTP loggers are noisy and never contain information we need.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
