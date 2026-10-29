"""Tests for the limits one tool call runs under.

A call that the deployment cannot keep is refused instead of ending the
turn, a call that is cancelled from the outside stops its process, a tool
that declares a deadline runs under it, and a child's own turn runs on the
model queue rather than behind document extraction.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS, Settings
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.tools import NATIVE_TOOL_DEADLINES, dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.worker.celery_app import celery_app

pytestmark = pytest.mark.unit_no_db


def _context(tmp_path: Path, **overrides: Any) -> ToolContext:
    """Build a tool context double for the shell tool."""
    settings = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_sandbox_backend": "none",
        "deepspace_shell_timeout_seconds": 30,
        "deepspace_shell_max_output_bytes": 65_536,
    }
    settings.update(overrides)
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=SimpleNamespace(**settings),
        db=SimpleNamespace(),
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )


def test_a_result_the_deployment_cannot_store_is_refused() -> None:
    service = object.__new__(DeepSpaceChatService)
    service.settings = Settings(deepspace_spill_max_chars=10_000)
    auth = SimpleNamespace(tenant_id=uuid.uuid4())

    payload, reference = service._spill_tool_result(
        auth=auth,
        tool_name="bash",
        payload={"output": "x" * 50_000},
    )

    assert reference is None
    assert payload["status"] == "refused"
    assert "spill" in str(payload["error"]).lower()
    assert "narrow the call" in str(payload["message"]).lower()


def test_a_result_the_deployment_can_store_is_spilled() -> None:
    service = object.__new__(DeepSpaceChatService)
    service.settings = Settings()
    service._preset_resolution = None
    auth = SimpleNamespace(tenant_id=uuid.uuid4())

    payload, reference = service._spill_tool_result(
        auth=auth,
        tool_name="bash",
        payload={"output": "x" * 50_000},
    )

    assert reference is not None
    assert payload["status"] == "spilled"
    assert payload["spill_ref"] == reference.sha256


def test_a_cancelled_shell_call_stops_its_process(tmp_path: Path) -> None:
    context = _context(tmp_path)
    marker = tmp_path / "marker"

    async def run() -> None:
        task = asyncio.create_task(
            dispatch_native_tool(
                "bash",
                {"command": f"sleep 2; touch {marker}"},
                context,
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    time.sleep(2.5)

    assert not marker.exists()


def test_a_shell_command_that_finishes_leaves_no_cancellation(tmp_path: Path) -> None:
    context = _context(tmp_path)
    marker = tmp_path / "marker"

    result = asyncio.run(dispatch_native_tool("bash", {"command": f"touch {marker}"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert marker.exists()


def test_the_chat_service_pipeline_carries_the_tools_own_deadline() -> None:
    service = object.__new__(DeepSpaceChatService)
    service.settings = Settings()
    service.tool_policy = DeepSpaceToolPolicy()
    service._hook_conversation_id = ""
    service._hook_auth = None

    pipeline = service.tool_pipeline

    assert NATIVE_TOOL_DEADLINES
    for name, deadline in NATIVE_TOOL_DEADLINES.items():
        assert pipeline.resolve_timeout(name, None) == deadline
    assert pipeline.resolve_timeout("fs_read", None) == DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS


def test_a_child_turn_runs_on_the_model_queue() -> None:
    route = celery_app.amqp.router.route({}, "deepspace.subagent_turn", args=(), kwargs={})
    default_route = celery_app.amqp.router.route({}, "deepspace.run", args=(), kwargs={})

    assert route["queue"].name == "deepspace"
    assert default_route["queue"].name == "deepspace"
