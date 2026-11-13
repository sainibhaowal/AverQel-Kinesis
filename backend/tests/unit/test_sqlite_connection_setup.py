from __future__ import annotations

from typing import Any

import pytest

from app.platform.database.session import _set_sqlite_pragma

pytestmark = pytest.mark.unit_no_db


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True


class _FakeConnectionWithoutExtensionApi:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.functions: dict[str, Any] = {}

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def create_function(self, name: str, arity: int, function: Any) -> None:
        self.functions[name] = (arity, function)


class _FakeConnectionWithExtensionApi(_FakeConnectionWithoutExtensionApi):
    def __init__(self) -> None:
        super().__init__()
        self.extension_loading: list[bool] = []

    def enable_load_extension(self, enabled: bool) -> None:
        self.extension_loading.append(enabled)


def test_sqlite_connection_setup_tolerates_missing_extension_loading_api() -> None:
    connection = _FakeConnectionWithoutExtensionApi()

    _set_sqlite_pragma(connection, None)

    assert connection.cursor_instance.statements == [
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=30000",
        "PRAGMA synchronous=NORMAL",
    ]
    assert connection.cursor_instance.closed
    assert set(connection.functions) == {"greatest", "least"}
    assert connection.functions["greatest"][1](None, 2, 1) == 2
    assert connection.functions["least"][1](None, 2, 1) == 1


def test_sqlite_connection_setup_keeps_extension_loading_when_available() -> None:
    connection = _FakeConnectionWithExtensionApi()

    _set_sqlite_pragma(connection, None)

    assert connection.extension_loading == [True]
    assert connection.cursor_instance.closed
