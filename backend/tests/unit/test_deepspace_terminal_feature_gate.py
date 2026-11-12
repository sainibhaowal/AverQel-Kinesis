"""Tests for the deployment terminal-session API gate."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.deepspace.api import workspace as workspace_api
from app.deepspace.api.workspace import _require_terminal_enabled
from app.deepspace.services.sandbox_backend import SandboxUnavailableError

pytestmark = pytest.mark.unit_no_db


def test_disabled_terminal_setting_blocks_workspace_terminal_routes() -> None:
    with pytest.raises(ApiError) as raised:
        _require_terminal_enabled(SimpleNamespace(deepspace_terminal_enabled=False))

    assert raised.value.code == "TERMINAL_DISABLED"
    assert raised.value.status_code == 503


def test_enabled_terminal_setting_allows_workspace_terminal_routes() -> None:
    _require_terminal_enabled(SimpleNamespace(deepspace_terminal_enabled=True))


@pytest.mark.asyncio
async def test_terminal_api_exposes_sandbox_unavailability_as_actionable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableManager:
        async def open(self, **kwargs: object) -> None:
            del kwargs
            raise SandboxUnavailableError(platform="linux", options=("bubblewrap", "Landlock"))

    monkeypatch.setattr(workspace_api, "terminal_manager", lambda: UnavailableManager())
    monkeypatch.setattr(workspace_api, "_conversation_workspace", lambda *_args: None)

    with pytest.raises(ApiError) as raised:
        await workspace_api.open_terminal_session(
            conversation_id=uuid.uuid4(),
            payload={"shell": "bash"},
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
            db=object(),
            settings=SimpleNamespace(deepspace_terminal_enabled=True),
        )

    assert raised.value.code == "SANDBOX_UNAVAILABLE"
    assert raised.value.status_code == 503
    assert raised.value.details == {"runner_options": ["bubblewrap", "Landlock"]}
