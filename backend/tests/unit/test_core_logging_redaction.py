"""Tests for the sensitive-data log filter.

The filter redacts values that look like credentials. A redacted record must
still be formattable, because a formatter that raises turns one log line
into a failed request.
"""

from __future__ import annotations

import logging

from app.core.logging import SensitiveDataFilter


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def test_a_sensitive_message_is_redacted() -> None:
    record = _record("the provider token was rejected")

    SensitiveDataFilter().filter(record)

    assert record.msg == "[redacted]"


def test_a_redacted_message_with_arguments_still_formats() -> None:
    record = _record("Standard OAuth setup not available for %s", "server-1")

    SensitiveDataFilter().filter(record)

    # The message is redacted, and the arguments are dropped with it, so the
    # formatter interpolates nothing into a message that has no placeholders.
    assert record.msg == "[redacted]"
    assert record.args == ()
    assert record.getMessage() == "[redacted]"


def test_an_ordinary_message_keeps_its_arguments() -> None:
    record = _record("The queue resumed %s", "server-1")

    SensitiveDataFilter().filter(record)

    assert record.getMessage() == "The queue resumed server-1"


def test_a_sensitive_argument_is_redacted_and_the_message_still_formats() -> None:
    record = _record("connecting with %s", "super-secret-value")

    SensitiveDataFilter().filter(record)

    assert record.getMessage() == "connecting with [redacted]"
