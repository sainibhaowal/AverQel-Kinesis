"""Tests for the deployment terminal-session API gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.deepspace.api.workspace import _require_terminal_enabled

pytestmark = pytest.mark.unit_no_db


def test_disabled_terminal_setting_blocks_workspace_terminal_routes() -> None:
    with pytest.raises(ApiError) as raised:
        _require_terminal_enabled(SimpleNamespace(deepspace_terminal_enabled=False))

    assert raised.value.code == "TERMINAL_DISABLED"
    assert raised.value.status_code == 503


def test_enabled_terminal_setting_allows_workspace_terminal_routes() -> None:
    _require_terminal_enabled(SimpleNamespace(deepspace_terminal_enabled=True))
