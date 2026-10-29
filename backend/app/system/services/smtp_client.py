"""The SMTP client for outbound notification email.

The notification worker needs a real SMTP conversation, and it needs the
client to fail in a way the worker can distinguish: a refused connection is
a delivery failure the outbox retries, while a misconfigured deployment
should not spin.

The helper is a context manager, so the connection is always closed, and
the transport security follows the deployment's configuration: implicit TLS
for a secured port, STARTTLS for a plain port, and no transport security
for a local relay.
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Any

from app.core.config import Settings


class SmtpDeliveryError(RuntimeError):
    """An email could not be delivered."""


@contextmanager
def smtp_connection(
    settings: Settings,
    *,
    timeout: int = 15,
) -> Iterator[Any]:
    """Open an SMTP connection to the configured relay.

    Args:
        settings: The deployment settings that name the relay.
        timeout: The socket timeout for the connection, in seconds.

    Yields:
        The connected SMTP client.

    Raises:
        SmtpDeliveryError: When the deployment has no relay configured, or
            the connection could not be established.
    """
    host = str(getattr(settings, "notification_smtp_host", "") or "").strip()
    if not host:
        raise SmtpDeliveryError("No SMTP relay is configured for this deployment.")
    port = int(getattr(settings, "notification_smtp_port", 587) or 587)
    starttls = bool(getattr(settings, "notification_smtp_starttls", True))
    implicit_tls = bool(getattr(settings, "notification_smtp_implicit_tls", False))
    try:
        if implicit_tls:
            client: Any = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        raise SmtpDeliveryError(f"The SMTP relay could not be reached: {exc}") from exc
    try:
        if starttls and not implicit_tls:
            try:
                client.starttls()
            except (OSError, smtplib.SMTPException) as exc:
                raise SmtpDeliveryError(f"The SMTP relay refused STARTTLS: {exc}") from exc
        yield client
    except smtplib.SMTPException as exc:
        raise SmtpDeliveryError(f"The email could not be delivered: {exc}") from exc
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            try:
                client.close()
            except (OSError, smtplib.SMTPException):
                pass


def build_message(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> EmailMessage:
    """Build one plain-text notification email.

    Args:
        sender: The address the mail is sent from.
        recipient: The address the mail is sent to.
        subject: The mail's subject.
        body: The mail's plain-text body.

    Returns:
        The message.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return message


__all__ = ["SmtpDeliveryError", "build_message", "smtp_connection"]
