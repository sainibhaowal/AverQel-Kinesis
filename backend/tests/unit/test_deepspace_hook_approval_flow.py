"""The hook's question reaches the user.

A hook may ask about a tool call instead of refusing it.  The ask is not
dropped: the pipeline reports it, the turn pauses with an approval request,
and the call runs when the user approves it.  The hooks are not asked about
the approved call a second time, so an approval cannot become a loop of
approvals.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.hook_gate import (
    APPROVED_CALL,
    HookParticipant,
    call_signature,
)
from app.deepspace.services.hooks import HookRegistry, HookRunner
from app.deepspace.services.tool_pipeline import DeepSpaceToolPipeline, PreOutcome

pytestmark = pytest.mark.unit_no_db


def _hook_script(tmp_path: Path, decision: str, reason: str) -> Path:
    """Write a hook script that answers with one decision.

    Args:
        tmp_path: The directory the script lives in.
        decision: The decision the hook answers with.
        reason: The reason the hook gives.

    Returns:
        The script's path.
    """
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write(json.dumps({{'decision': {decision!r}, 'reason': {reason!r}}}))\n",
        encoding="utf-8",
    )
    return script


def _participant(tmp_path: Path, decision: str, reason: str, matcher: str = "*") -> HookParticipant:
    """Build a participant whose only hook answers with one decision.

    Args:
        tmp_path: The directory the hook script lives in.
        decision: The decision the hook answers with.
        reason: The reason the hook gives.
        matcher: The tool the hook watches.

    Returns:
        The participant.
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
    return HookParticipant(HookRunner(registry=registry))


class _AllowingPolicy:
    """A policy that lets every call run."""

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
        return SimpleNamespace(
            allowed=True,
            mode="read",
            reason=None,
            requires_approval=False,
            risk_level="read",
        )

    def after_tool(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Return the result unchanged.

        Args:
            tool_name: The tool that ran.
            result: The tool's result.

        Returns:
            The result.
        """
        del tool_name
        return result


def _pipeline(participant: HookParticipant) -> DeepSpaceToolPipeline:
    """Build a pipeline whose only rule is the hook participant.

    Args:
        participant: The participant.

    Returns:
        The pipeline.
    """
    pipeline = DeepSpaceToolPipeline(_AllowingPolicy())
    pipeline.register_pre("deepspace-hooks", participant)
    return pipeline


async def test_a_hook_that_asks_is_reported_to_the_caller(tmp_path: Path) -> None:
    """The pipeline reports the ask instead of dropping it."""
    pipeline = _pipeline(_participant(tmp_path, "ask", "check this first"))
    ran = False

    async def body() -> Any:
        """Record that the tool ran.

        Returns:
            Nothing.
        """
        nonlocal ran
        ran = True
        return {"status": "ok"}

    result = await pipeline.run(tool_name="fs_write", arguments={"path": "a"}, body=body)

    assert result.success is False
    assert result.requires_approval is True
    assert ran is False


async def test_the_call_runs_when_the_user_approved_it(tmp_path: Path) -> None:
    """An approved call is not asked about again."""
    pipeline = _pipeline(_participant(tmp_path, "ask", "check this first"))
    ran = False

    async def body() -> Any:
        """Record that the tool ran.

        Returns:
            Nothing.
        """
        nonlocal ran
        ran = True
        return {"status": "ok"}

    token = APPROVED_CALL.set(call_signature("fs_write", {"path": "a"}))
    try:
        result = await pipeline.run(tool_name="fs_write", arguments={"path": "a"}, body=body)
    finally:
        APPROVED_CALL.reset(token)

    assert result.success is True
    assert ran is True


async def test_another_call_is_still_asked_about(tmp_path: Path) -> None:
    """The approval covers one call, not every call."""
    pipeline = _pipeline(_participant(tmp_path, "ask", "check this first"))

    async def body() -> Any:
        """Return nothing.

        Returns:
            Nothing.
        """
        return {"status": "ok"}

    token = APPROVED_CALL.set(call_signature("fs_write", {"path": "a"}))
    try:
        result = await pipeline.run(tool_name="fs_write", arguments={"path": "b"}, body=body)
    finally:
        APPROVED_CALL.reset(token)

    assert result.requires_approval is True


def test_the_approval_request_carries_the_hook_question() -> None:
    """The request tells the user why the agent is waiting."""

    class _Service:
        """The part of the chat service the request builder uses."""

        _now = staticmethod(lambda: "2026-10-21T12:00:00+00:00")
        _hook_approval_request = DeepSpaceChatService._hook_approval_request

    state = SimpleNamespace(
        result={
            "success": False,
            "error": "A hook asked to confirm this tool call: check this first.",
            "requires_approval": True,
            "approval_reason": "A hook asked to confirm this tool call: check this first.",
        },
        call_id="call-1",
        round_index=2,
        step_id="step-1",
        tool_name="fs_write",
        mcp_bindings={},
        item={"arguments": {"path": "a"}},
        risk_level="medium",
    )

    request = _Service()._hook_approval_request(state)

    assert request["call_id"] == "call-1"
    assert request["tool_name"] == "fs_write"
    assert request["tool_input"] == {"path": "a"}
    assert request["decision"] == "pending"
    assert request["permission_level"] == "human"
    assert "check this first" in request["message"]


def test_the_pipeline_speaks_the_approval_vocabulary() -> None:
    """A participant may report that it asked the user."""

    async def participant(tool_name: str, arguments: dict[str, Any]) -> PreOutcome:
        """Ask the user about the call.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.

        Returns:
            The outcome.
        """
        del tool_name
        del arguments
        return PreOutcome("cancel", "please confirm", requires_approval=True)

    async def body() -> Any:
        """Return nothing.

        Returns:
            Nothing.
        """
        return None

    pipeline = DeepSpaceToolPipeline(_AllowingPolicy())
    pipeline.register_pre("asker", participant)

    result = asyncio.run(pipeline.run(tool_name="fs_write", arguments={}, body=body))

    assert result.requires_approval is True
    assert result.error == "please confirm"


class _AskingProvider:
    """A provider that calls one tool and then answers."""

    def __init__(self) -> None:
        """Count the requests the provider answered."""
        self.calls = 0

    async def stream_generate_events(self, request: Any) -> Any:
        """Answer one request.

        Args:
            request: The provider request.

        Yields:
            The provider's events.
        """
        del request
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_calls_delta",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_discover_1",
                        "function": {
                            "name": "discover_tools",
                            "arguments": '{"query":"list the runtime"}',
                        },
                    }
                ],
            }
            return
        yield {"type": "delta", "text": "The runtime is ready."}


@pytest.mark.asyncio
async def test_a_hook_that_asks_pauses_the_turn_for_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn waits for the user instead of running the call."""
    from app.deepspace.services import chat_service as chat_service_module
    from app.deepspace.services import hook_gate as hook_gate_module
    from tests.unit.test_deepspace_chat_service import (
        _FakeRegistry,
        _FakeRepository,
        _FakeSelectionService,
        _FakeTaskStore,
    )

    provider = _AskingProvider()
    registry = _FakeRegistry(SimpleNamespace())
    registry.get_chat_provider_from_selection = lambda candidate: provider
    monkeypatch.setattr(chat_service_module, "DeepSpaceChatRepository", _FakeRepository)
    monkeypatch.setattr(chat_service_module, "ProviderSelectionService", _FakeSelectionService)
    monkeypatch.setattr(chat_service_module, "ProviderRegistry", lambda settings: registry)
    monkeypatch.setattr(chat_service_module, "DeepSpaceTaskLoopStore", _FakeTaskStore)

    hooks = _participant(tmp_path, "ask", "check this first")
    monkeypatch.setattr(hook_gate_module, "hook_registry", lambda: hooks._runner._registry)

    class _NoSessionEvents:
        """A session log that records nothing."""

        def __init__(self, db: Any) -> None:
            """Create the store.

            Args:
                db: The database session.
            """
            del db

        def append(self, *args: Any, **kwargs: Any) -> None:
            """Ignore the event.

            Args:
                args: The event's fields.
                kwargs: The event's fields.
            """

        def append_many(self, *args: Any, **kwargs: Any) -> None:
            """Ignore the events.

            Args:
                args: The events' fields.
                kwargs: The events' fields.
            """

    monkeypatch.setattr(chat_service_module, "SessionEventStore", _NoSessionEvents)

    service = DeepSpaceChatService(
        db=SimpleNamespace(commit=lambda: None, rollback=lambda: None),
        settings=SimpleNamespace(llm_temperature=0.2, llm_max_tokens_per_request=128),
    )
    service.runtime.create_run = lambda **_kwargs: (_ for _ in ()).throw(AttributeError())

    frames = [
        frame
        async for frame in service.stream_turn(
            auth=SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()),
            conversation_id=None,
            prompt="What can the runtime do?",
        )
    ]

    approvals = [frame for frame in frames if frame.startswith("event: approval_request")]
    assert approvals, [frame.split("\n", 1)[0] for frame in frames]
    request = json.loads(approvals[0].split("data: ", 1)[1].strip())
    assert request["tool_name"] == "discover_tools"
    assert "check this first" in request["message"]
    assert request["decision"] == "pending"
    # The turn stopped at the question: the provider was not asked again,
    # the tool did not run, and the turn waits for the user.
    assert provider.calls == 1
    assert not any(frame.startswith("event: tool_result") for frame in frames)
    done = next(frame for frame in frames if frame.startswith("event: done"))
    assert json.loads(done.split("data: ", 1)[1].strip())["status"] == "awaiting_approval"
