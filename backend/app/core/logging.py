from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pythonjsonlogger import jsonlogger

from app.core.context import get_tenant_id, get_trace_id, get_user_id

SENSITIVE_FIELD_NAMES: Final[set[str]] = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "provider_secret",
    "secret_ciphertext",
    "secret_nonce",
    "secret_kid",
    "oauth_access_token",
    "oauth_refresh_token",
    "session_token",
    "token",
    "refresh_token",
    "access_token",
    "api_key",
}

SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"authorization|token|password|secret|cookie|api[-_ ]?key|refresh[-_ ]?token|access[-_ ]?token|"
    r"oauth|session[-_ ]?token|secret[-_ ]?(ciphertext|nonce|kid)|provider[-_ ]?secret"
    r")\b"
)


def _sanitize_value(value: Any) -> Any:
    """Recursively sanitize values for structured logging."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.lower() in SENSITIVE_FIELD_NAMES:
                sanitized[key_str] = "[redacted]"
            else:
                sanitized[key_str] = _sanitize_value(item)
        return sanitized

    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_value(item) for item in value]

    if isinstance(value, bytes):
        return "[redacted-bytes]"

    if isinstance(value, str):
        if SENSITIVE_VALUE_PATTERN.search(value):
            return "[redacted]"
        return value

    return value


class RequestContextFilter(logging.Filter):
    """Inject request-scoped identity fields into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        record.tenant_id = get_tenant_id()
        record.user_id = get_user_id()
        return True


class SensitiveDataFilter(logging.Filter):
    """Redact sensitive fields and values from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field_name, field_value in list(record.__dict__.items()):
            lowered = field_name.lower()
            if lowered in SENSITIVE_FIELD_NAMES:
                setattr(record, field_name, "[redacted]")
                continue

            if field_name not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            }:
                setattr(record, field_name, _sanitize_value(field_value))

        if isinstance(record.msg, str):
            if SENSITIVE_VALUE_PATTERN.search(record.msg):
                record.msg = "[redacted]"
                # The redacted message no longer carries the placeholders its
                # arguments belong to. The arguments are dropped rather than
                # interpolated, because a message without placeholders cannot
                # hold them and the formatter would fail on the record.
                record.args = ()

        if isinstance(record.args, tuple):
            record.args = tuple(_sanitize_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = _sanitize_value(record.args)

        return True


from pathlib import Path


def _build_json_handler(target: Any = None) -> logging.Handler:
    """Create the shared JSON log handler for stream or rotating file."""
    if isinstance(target, (str, Path)):
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            str(target),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    elif target is not None:
        handler = logging.StreamHandler(target)
    else:
        handler = logging.StreamHandler()

    formatter = jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
        fmt=(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "%(trace_id)s %(tenant_id)s %(user_id)s %(pathname)s %(lineno)d"
        ),
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "name": "module",
            "pathname": "file",
            "lineno": "line",
        },
    )
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    handler.addFilter(SensitiveDataFilter())
    return handler


def configure_logging(level: str) -> None:
    """Configure application-wide structured JSON logging with local file persistence."""
    normalized_level = level.strip().upper()
    stream_handler = _build_json_handler()
    handlers: list[logging.Handler] = [stream_handler]

    try:
        from app.core.averqel_home import averqel_logs_path

        log_file = averqel_logs_path() / "backend.log"
        file_handler = _build_json_handler(log_file)
        handlers.append(file_handler)
    except Exception:  # noqa: BLE001
        pass

    root = logging.getLogger()
    root.setLevel(normalized_level)

    for existing_handler in list(root.handlers):
        root.removeHandler(existing_handler)
    for h in handlers:
        root.addHandler(h)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = handlers
        uvicorn_logger.setLevel(normalized_level)
        uvicorn_logger.propagate = False
