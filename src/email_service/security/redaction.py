"""Safe logging: structured JSON lines with secrets and personal data redacted."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[REDACTED_GOOGLE_TOKEN]"),
    (re.compile(r"\b1//[A-Za-z0-9._-]{20,}"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"\bgoa_[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (
        re.compile(
            r"(?i)(\"?(?:key|api_key|password|secret|token|refresh_token|access_token|client_secret)\"?"
            r"\s*[:=]\s*)\"?[^\s\",}]+"
        ),
        r"\1[REDACTED]",
    ),
    # Email addresses are personal data: keep only the domain.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"), r"***@\1"),
)

_SAFE_EXTRA = frozenset(
    {"draft_id", "delivery_id", "provider", "outcome", "error_code", "store", "path", "status"}
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingJsonFormatter(logging.Formatter):
    """One JSON object per line; only allow-listed ``extra`` fields are emitted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in _SAFE_EXTRA:
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = redact(str(value))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
