"""Regression checks for documented DeepSpace runtime behavior.

- Native tool schemas are supplied for the selected preset at the start of a
  model turn; native discovery is not a model-invoked prerequisite.
- A tool call was cut at thirty seconds, whatever the deployment's own
  tool budget said.
- The shell tool ran with the server's whole environment, so a command
  could read a deployment secret.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.deepspace.services.presets import resolve_preset
from app.deepspace.services.terminals import build_session_environment

pytestmark = pytest.mark.unit_no_db


@pytest.mark.parametrize("preset_id", ["standard", "ptc", "minimal", "creator", "custom"])
def test_native_tool_discovery_is_not_a_model_step(preset_id: str) -> None:
    """Every mode receives its permitted tool list without a discovery call."""
    resolution = resolve_preset(preset_id)
    allowlist = resolution.tool_allowlist
    if allowlist is None:
        return
    assert "discover_tools" not in allowlist


def test_a_preset_still_limits_the_tools_it_names() -> None:
    """A named preset stays limited and does not add native discovery."""
    resolution = resolve_preset("creator")
    allowlist = resolution.tool_allowlist
    assert allowlist is not None
    assert "bash" not in allowlist
    assert "discover_tools" not in allowlist


def test_the_tool_budget_follows_the_deployment() -> None:
    """A call runs under the deployment's budget, not a fixed thirty seconds."""
    import time

    from app.deepspace.services.chat_service import (
        DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS,
        DeepSpaceChatService,
    )

    service = DeepSpaceChatService.__new__(DeepSpaceChatService)
    service.settings = type("_Settings", (), {"deepspace_tool_timeout_seconds": 300.0})()

    budget = DeepSpaceChatService._tool_call_budget(service, loop_deadline=time.monotonic() + 600.0)

    assert budget == 300.0
    assert DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS == 120.0


def test_the_tool_budget_stays_inside_the_turn() -> None:
    """A call never runs past the turn's own deadline."""
    import time

    from app.deepspace.services.chat_service import DeepSpaceChatService

    service = DeepSpaceChatService.__new__(DeepSpaceChatService)
    service.settings = type("_Settings", (), {"deepspace_tool_timeout_seconds": 300.0})()

    budget = DeepSpaceChatService._tool_call_budget(service, loop_deadline=time.monotonic() + 1.0)

    assert budget <= 5.0


def test_a_shell_command_does_not_see_the_servers_environment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment a command receives carries no deployment secret."""
    monkeypatch.setenv("AKS_PROVIDER_SECRET_KEYRING_JSON", "a-secret")

    environment = build_session_environment(
        workspace=tmp_path,
        shell="bash",
        mode="workspace-write",
    )

    assert "AKS_PROVIDER_SECRET_KEYRING_JSON" not in environment
    assert environment["HOME"] == str(tmp_path)
    assert environment["DEEPSPACE_SHELL"] == "1"
