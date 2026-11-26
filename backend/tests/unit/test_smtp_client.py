"""Tests for the SMTP client."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from app.system.services.smtp_client import (
    SmtpDeliveryError,
    build_message,
)


class TestBuildMessage:
    """Tests for the build_message function."""

    def test_build_message_basic(self) -> None:
        message = build_message(
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Test Subject",
            body="Test body content",
        )

        assert isinstance(message, EmailMessage)
        assert message["From"] == "sender@example.com"
        assert message["To"] == "recipient@example.com"
        assert message["Subject"] == "Test Subject"
        assert message.get_content().strip() == "Test body content"

    def test_build_message_empty_body(self) -> None:
        message = build_message(
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Test",
            body="",
        )

        assert message.get_content().strip() == ""

    def test_build_message_unicode(self) -> None:
        message = build_message(
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Test 中文",
            body="Body with unicode: 你好世界 🌍",
        )

        assert message["Subject"] == "Test 中文"
        assert message.get_content().strip() == "Body with unicode: 你好世界 🌍"


class TestSmtpDeliveryError:
    """Tests for the SmtpDeliveryError exception."""

    def test_exception_creation(self) -> None:
        error = SmtpDeliveryError("Test error message")
        assert str(error) == "Test error message"
        assert isinstance(error, RuntimeError)

    def test_exception_chaining(self) -> None:
        original = ValueError("Original error")
        error = SmtpDeliveryError("Wrapped error")
        error.__cause__ = original
        assert error.__cause__ is original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
