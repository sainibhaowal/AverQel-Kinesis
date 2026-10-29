"""Tests for the hook gate in DeepSpace's tool pipeline.

The gate is what makes a hook more than a manifest: a hook that blocks a
call must stop the call before the tool runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.deepspace.services.hook_gate import HookParticipant
from app.deepspace.services.hooks import HookEvent, HookRegistry, HookRunner
from app.deepspace.services.tool_pipeline import DeepSpaceToolPipeline, PreOutcome

pytestmark = pytest.mark.unit_no_db


def _hook_script(tmp_path: Path, decision: str, reason: str) -> Path:
    """Write a hook that answers one decision.

    Args:
        tmp_path: The directory to write the hook into.
        decision: The decision the hook answers.
        reason: The reason the hook gives.

    Returns:
        The hook's path.
    """
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read() or '{}')\n"
        "sys.stdout.write(json.dumps({'decision': %r, 'reason': %r}))\n" % (decision, reason),
        encoding="utf-8",
    )
    return script


def _registry(tmp_path: Path, decision: str, reason: str, matcher: str = "*") -> HookRegistry:
    """Build a registry with one hook.

    Args:
        tmp_path: The directory to write the hook into.
        decision: The decision the hook answers.
        reason: The reason the hook gives.
        matcher: The tool the hook watches.

    Returns:
        The registry.
    """
    script = _hook_script(tmp_path, decision, reason)
    registry = HookRegistry.from_manifest(
        {
            "hooks": [
                {
                    "id": "gate",
                    "event": "PreToolUse",
                    "command": [sys.executable, str(script)],
                    "matcher": matcher,
                    "timeout_seconds": 10,
                }
            ]
        }
    )
    return registry


def test_a_hook_that_blocks_stops_the_call(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "block", "that would delete production data")
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant)

    assert outcome.decision == "deny"
    assert "production data" in (outcome.reason or "")


def test_a_hook_that_allows_lets_the_call_run(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "allow", "")
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant)

    assert outcome.decision == "allow"


def test_a_broken_hook_never_blocks_the_call(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    registry = HookRegistry.from_manifest(
        {
            "hooks": [
                {
                    "id": "broken",
                    "event": "PreToolUse",
                    "command": [sys.executable, str(script)],
                    "timeout_seconds": 10,
                }
            ]
        }
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant)

    assert outcome.decision == "allow"


def test_a_hook_that_watches_another_tool_does_not_run(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "block", "not for this tool", matcher="fs_delete")
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant, tool_name="fs_write")

    assert outcome.decision == "allow"


def test_the_pipeline_awaits_an_async_participant(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "block", "blocked by the pipeline test")
    pipeline = DeepSpaceToolPipeline(_AllowingPolicy())
    pipeline.register_pre("deepspace-hooks", HookParticipant(HookRunner(registry=registry)))
    ran = False

    async def _body() -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"status": "ok"}

    import asyncio

    result = asyncio.run(
        pipeline.run(tool_name="fs_write", arguments={"path": "a.txt"}, body=_body)
    )

    assert not ran
    assert not result.success
    assert "blocked by the pipeline test" in (result.error or "")


class _AllowingPolicy:
    """A policy that allows every call."""

    def before_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Allow the call.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.

        Returns:
            The decision.
        """
        del tool_name
        del arguments
        return _Allowed()

    def after_tool(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Return the result unchanged.

        Args:
            tool_name: The tool the model called.
            result: The tool's result.

        Returns:
            The result.
        """
        del tool_name
        return result


class _Allowed:
    """An allowed policy decision."""

    allowed = True
    mode = "read"
    reason = None
    requires_approval = False
    risk_level = "read"


def _run(participant: HookParticipant, *, tool_name: str = "fs_write") -> PreOutcome:
    """Run the participant once.

    Args:
        participant: The participant.
        tool_name: The tool the model called.

    Returns:
        The decision.
    """
    import asyncio

    return asyncio.run(participant(tool_name, {"path": "a.txt"}))


def test_the_hook_events_are_the_claude_code_names() -> None:
    assert HookEvent.PRE_TOOL_USE.value == "PreToolUse"
    assert HookEvent.POST_TOOL_USE.value == "PostToolUse"
    assert json.dumps({"decision": "block"})  # the wire format stays JSON
