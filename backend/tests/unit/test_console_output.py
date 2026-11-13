from __future__ import annotations

import io
import sys

import pytest

from app.core.console_output import configure_console_output

pytestmark = pytest.mark.unit_no_db


def test_console_output_escapes_characters_missing_from_windows_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_buffer, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    configure_console_output()
    stdout.write("✓ AverQel home: C:\\Users\\Renée")
    stderr.write("✓ startup diagnostic")
    stdout.flush()
    stderr.flush()

    assert stdout.errors == "backslashreplace"
    assert stderr.errors == "backslashreplace"
    assert stdout_buffer.getvalue() == "✓ AverQel home: C:\\Users\\Renée".encode(
        "cp1252", errors="backslashreplace"
    )
    assert stderr_buffer.getvalue() == "✓ startup diagnostic".encode(
        "cp1252", errors="backslashreplace"
    )


def test_console_output_accepts_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    configure_console_output()
    stdout.write("✓ startup")
    stderr.write("✓ diagnostic")

    assert stdout.getvalue() == "✓ startup"
    assert stderr.getvalue() == "✓ diagnostic"


def test_console_output_tolerates_missing_or_unconfigurable_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnconfigurableStream:
        def reconfigure(self, *, errors: str) -> None:
            raise ValueError(errors)

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", UnconfigurableStream())

    configure_console_output()
