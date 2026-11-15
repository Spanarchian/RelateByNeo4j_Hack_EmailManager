from __future__ import annotations

import json
import logging

import pytest

from email_service.security.redaction import RedactingJsonFormatter, configure_logging

pytestmark = pytest.mark.unit


def test_formatter_emits_json_and_redacts() -> None:
    formatter = RedactingJsonFormatter()
    record = logging.LogRecord(
        "email_service",
        logging.INFO,
        __file__,
        1,
        "send to %s with Bearer %s",
        ("a.b@example.org", "ya29.SECRET"),
        None,
    )
    record.provider = "gmail"
    record.password = "should-not-appear"  # not allow-listed
    line = json.loads(formatter.format(record))
    assert line["level"] == "INFO"
    assert line["provider"] == "gmail"
    assert "ya29.SECRET" not in line["message"]
    assert "a.b@" not in line["message"] and "***@example.org" in line["message"]
    assert "password" not in line


def test_formatter_redacts_exceptions() -> None:
    formatter = RedactingJsonFormatter()
    try:
        raise ValueError('{"key": "md-supersecret"}')
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    line = json.loads(formatter.format(record))
    assert "md-supersecret" not in line["exception"]


def test_configure_logging() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("WARNING")
        assert isinstance(root.handlers[0].formatter, RedactingJsonFormatter)
        assert root.level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:], _ = saved[0], root.setLevel(saved[1])
