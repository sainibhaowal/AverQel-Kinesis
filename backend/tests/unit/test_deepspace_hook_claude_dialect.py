"""Tests for the Claude Code and Codex hook dialect in DeepSpace.

A hook script the deployment already runs with Claude Code has to work
here unchanged, so the checks cover both dialects: the permission decision
inside ``hookSpecificOutput``, the block that exit status 2 means, the
older top-level decision, the stop request, the message for the user, the
context for the model, and the arguments that replace the call's own.

The checks run real hook commands as short-lived Python subprocesses, so
the process boundary itself is covered.  No database and no network are
used.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.hook_gate import (
    HookParticipant,
    HookPreOutcome,
    SessionEventHookAudit,
    hook_audit_recorder,
)
from app.deepspace.services.hooks import (
    HookDecision,
    HookDefinition,
    HookEvent,
    HookRegistry,
    HookRunner,
)
from app.deepspace.services.session_events import HOOK_INVOKED, HOOK_RESULT

pytestmark = pytest.mark.unit_no_db


def _command(source: str) -> list[str]:
    """Build the argv of one hook command.

    Args:
        source: The Python source the hook runs.

    Returns:
        The argv.
    """
    return [sys.executable, "-c", source]


def _answer_source(answer: dict[str, Any]) -> str:
    """Build the source of a hook that answers with one JSON object.

    Args:
        answer: The object the hook answers with.

    Returns:
        The source.
    """
    return f"import json, sys; sys.stdin.read(); print(json.dumps({answer!r}))"


def _definition(
    hook_id: str,
    source: str,
    *,
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    matcher: str | None = None,
    matcher_kind: str = "literal",
    timeout_seconds: float = 10.0,
) -> HookDefinition:
    """Build one hook definition for a test.

    Args:
        hook_id: The hook's identifier.
        source: The Python source the hook runs.
        event: The moment the hook takes part in.
        matcher: The tool name filter.
        matcher_kind: How the matcher is read.
        timeout_seconds: How long the hook may take.

    Returns:
        The definition.
    """
    return HookDefinition(
        id=hook_id,
        event=event,
        command=tuple(_command(source)),
        timeout_seconds=timeout_seconds,
        matcher=matcher,
        matcher_kind=matcher_kind,  # type: ignore[arg-type]
    )


def _registry(*definitions: HookDefinition) -> HookRegistry:
    """Build a registry that holds the given hooks.

    Args:
        definitions: The hooks the registry holds.

    Returns:
        The registry.
    """
    return HookRegistry(definitions)


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion.

    Args:
        coroutine: The coroutine to run.

    Returns:
        Its result.
    """
    return asyncio.run(coroutine)


def _claude_answer(
    permission: str,
    reason: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build a Claude-style answer.

    Args:
        permission: The permission decision the hook answers with.
        reason: The reason the hook gives.
        extra: The other fields the answer carries.

    Returns:
        The answer.
    """
    output: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": permission}
    if reason:
        output["permissionDecisionReason"] = reason
    output.update(extra)
    return {"hookSpecificOutput": output}


def test_a_claude_deny_blocks_the_call() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("deny", "this call would delete production data")),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "prod.db"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "this call would delete production data"


def test_a_claude_deny_stops_the_call_in_the_gate() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("deny", "no writes here")),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant("fs_write", {"path": "a.txt"}))

    assert outcome.decision == "deny"
    assert "no writes here" in (outcome.reason or "")


def test_a_claude_allow_lets_the_call_run() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("allow", "the path is inside the workspace")),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "allow"
    assert decision.reason == "the path is inside the workspace"


def test_a_claude_ask_is_surfaced_and_not_dropped() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("ask", "confirm this with the user first")),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "ask"
    assert decision.reason == "confirm this with the user first"


def test_the_gate_reports_an_ask_as_an_approval_the_caller_can_act_on() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("ask", "confirm this with the user first")),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant("fs_write", {"path": "a.txt"}))

    assert isinstance(outcome, HookPreOutcome)
    assert outcome.decision == "cancel"
    assert outcome.action == "ask"
    assert outcome.requires_approval is True
    assert "confirm this with the user first" in (outcome.reason or "")


def test_exit_code_two_with_a_reason_on_stderr_blocks_the_call() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            "import sys; "
            "sys.stdin.read(); "
            "sys.stderr.write('the tool would delete production data\\n'); "
            "sys.exit(2)",
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "prod.db"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "the tool would delete production data"


def test_exit_code_two_stops_the_call_in_the_gate() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            "import sys; sys.stdin.read(); sys.stderr.write('no writes here\\n'); sys.exit(2)",
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant("fs_write", {"path": "a.txt"}))

    assert outcome.decision == "deny"
    assert "no writes here" in (outcome.reason or "")


def test_the_older_top_level_decision_shape_is_accepted() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"decision": "approve"}),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "allow"


def test_the_older_top_level_block_shape_is_accepted() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"decision": "block", "reason": "not allowed here"}),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "not allowed here"


def test_continue_false_stops_the_agent() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"continue": False, "stopReason": "the agent must stop here"}),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.stop is True
    assert decision.stop_reason == "the agent must stop here"


def test_continue_false_cancels_the_call_with_the_stop_reason() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"continue": False, "stopReason": "the agent must stop here"}),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant("fs_write", {"path": "a.txt"}))

    assert outcome.decision == "cancel"
    assert "the agent must stop here" in (outcome.reason or "")


def test_updated_input_replaces_the_arguments() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(
                _claude_answer(
                    "allow",
                    "the path was rewritten",
                    updatedInput={"path": "safe.txt"},
                )
            ),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))
    arguments: dict[str, Any] = {"path": "prod.db", "mode": "overwrite"}

    outcome = _run(participant("fs_write", arguments))

    assert outcome.decision == "allow"
    assert arguments == {"path": "safe.txt"}
    assert outcome.updated_input == {"path": "safe.txt"}


def test_additional_context_and_system_message_are_carried() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "the file was written to the workspace",
                    },
                    "systemMessage": "the user should know about this",
                }
            ),
            event=HookEvent.POST_TOOL_USE,
        )
    )
    runner = HookRunner(registry)

    decision = _run(
        runner.run_event(
            HookEvent.POST_TOOL_USE,
            tool_name="fs_write",
            result={"status": "ok"},
            conversation_id="conv-1",
        )
    )

    assert decision.additional_context == "the file was written to the workspace"
    assert decision.system_message == "the user should know about this"


def test_a_literal_matcher_matches_only_that_tool() -> None:
    registry = _registry(
        _definition("write-guard", "pass", matcher="fs_write"),
        _definition("read-guard", "pass", matcher="fs_read"),
    )

    assert [hook.id for hook in registry.hooks_for(HookEvent.PRE_TOOL_USE, "fs_write")] == [
        "write-guard"
    ]
    assert registry.hooks_for(HookEvent.PRE_TOOL_USE, "fs_write_all") == ()


def test_a_regex_matcher_matches_by_pattern() -> None:
    registry = _registry(
        _definition("fs-guard", "pass", matcher=r"^fs_", matcher_kind="regex"),
    )

    assert [hook.id for hook in registry.hooks_for(HookEvent.PRE_TOOL_USE, "fs_write")] == [
        "fs-guard"
    ]
    assert [hook.id for hook in registry.hooks_for(HookEvent.PRE_TOOL_USE, "fs_read")] == [
        "fs-guard"
    ]
    assert registry.hooks_for(HookEvent.PRE_TOOL_USE, "web_search") == ()


def test_a_manifest_declares_the_matcher_kind() -> None:
    registry = HookRegistry.from_manifest(
        {
            "hooks": [
                {
                    "id": "fs-guard",
                    "event": "PreToolUse",
                    "command": ["python3", "guard.py"],
                    "matcher": "^fs_",
                    "matcher_kind": "regex",
                },
                {
                    "id": "write-guard",
                    "event": "PreToolUse",
                    "command": ["python3", "guard.py"],
                    "matcher": "fs_write",
                },
            ]
        }
    )

    hooks = {hook.id: hook for hook in registry.all()}
    assert hooks["fs-guard"].matcher_kind == "regex"
    assert hooks["write-guard"].matcher_kind == "literal"
    assert not registry.errors()


def test_a_manifest_refuses_a_matcher_kind_it_does_not_know() -> None:
    registry = HookRegistry.from_manifest(
        {
            "hooks": [
                {
                    "id": "fs-guard",
                    "event": "PreToolUse",
                    "command": ["python3", "guard.py"],
                    "matcher": "^fs_",
                    "matcher_kind": "glob",
                }
            ]
        }
    )

    assert registry.all() == ()
    assert registry.errors()


def test_a_hook_that_answers_in_both_dialects_is_accepted() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(
                {
                    "decision": "allow",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "the claude shape wins",
                    },
                }
            ),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "the claude shape wins"


def test_the_deepspace_dialect_still_answers_a_hook() -> None:
    registry = _registry(
        _definition(
            "deepspace-guard",
            _answer_source(
                {
                    "decision": "block",
                    "reason": "the tool would delete production data",
                    "additional_context": "the workspace is read-only",
                }
            ),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "prod.db"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "the tool would delete production data"
    assert decision.additional_context == "the workspace is read-only"


def test_a_hook_that_answers_in_the_claude_dialect_can_be_turned_off() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"decision": "deny", "reason": "no writes here"}),
        )
    )
    runner = HookRunner(registry, claude_dialect=False)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "allow"


def test_the_claude_dialect_is_accepted_when_it_is_on() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"decision": "deny", "reason": "no writes here"}),
        )
    )
    runner = HookRunner(registry, claude_dialect=True)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "no writes here"


def test_exit_code_two_only_blocks_when_the_claude_dialect_is_on() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            "import sys; sys.stdin.read(); sys.stderr.write('no writes here\\n'); sys.exit(2)",
        )
    )
    runner = HookRunner(registry, claude_dialect=False)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "allow"


def test_an_ask_in_the_deepspace_dialect_is_surfaced_too() -> None:
    registry = _registry(
        _definition(
            "deepspace-guard",
            _answer_source({"decision": "ask", "reason": "confirm with the user"}),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    outcome = _run(participant("fs_write", {"path": "a.txt"}))

    assert outcome.decision == "cancel"
    assert outcome.action == "ask"
    assert outcome.requires_approval is True
    assert "confirm with the user" in (outcome.reason or "")


def test_a_top_level_updated_input_replaces_the_arguments() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source({"updatedInput": {"path": "safe.txt"}}),
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))
    arguments: dict[str, Any] = {"path": "prod.db"}

    outcome = _run(participant("fs_write", arguments))

    assert outcome.decision == "allow"
    assert arguments == {"path": "safe.txt"}


def test_the_hook_receives_the_conversation_the_participant_serves(tmp_path: Path) -> None:
    report = tmp_path / "payload.json"
    registry = _registry(
        _definition(
            "payload-reader",
            "import json, sys; "
            f"open({str(report)!r}, 'w').write(sys.stdin.read()); "
            "print(json.dumps({'decision': 'allow'}))",
        )
    )
    participant = HookParticipant(HookRunner(registry=registry), conversation_id="conv-7")

    _run(participant("fs_write", {"path": "a.txt"}))

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["conversation_id"] == "conv-7"


def test_the_post_tool_hooks_add_context_for_the_model() -> None:
    registry = _registry(
        _definition(
            "note-writer",
            _answer_source(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "the file was written to the workspace",
                    }
                }
            ),
            event=HookEvent.POST_TOOL_USE,
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    decision = _run(participant.after_tool("fs_write", {"status": "ok"}))

    assert decision.additional_context == "the file was written to the workspace"


def test_the_stop_hooks_message_reaches_the_user() -> None:
    registry = _registry(
        _definition(
            "stop-notice",
            _answer_source({"systemMessage": "the agent finished the migration"}),
            event=HookEvent.STOP,
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    decision = _run(participant.on_stop(conversation_id="conv-1"))

    assert decision.system_message == "the agent finished the migration"


def test_the_session_start_hooks_add_context() -> None:
    registry = _registry(
        _definition(
            "session-context",
            _answer_source(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "the workspace is /srv/project",
                    }
                }
            ),
            event=HookEvent.SESSION_START,
        )
    )
    participant = HookParticipant(HookRunner(registry=registry))

    decision = _run(participant.on_session_start(conversation_id="conv-1"))

    assert decision.additional_context == "the workspace is /srv/project"


def test_the_audit_events_are_written_with_the_right_payloads() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("deny", "no writes here")),
            matcher="fs_write",
        )
    )
    audit = _RecordingAudit()
    runner = HookRunner(registry, recorder=audit)

    _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert len(audit.invocations) == 1
    assert audit.invocations[0]["hook_id"] == "claude-guard"
    assert audit.invocations[0]["event"] == "PreToolUse"
    assert audit.invocations[0]["tool_name"] == "fs_write"
    assert audit.invocations[0]["duration_ms"] >= 0
    assert len(audit.results) == 1
    assert audit.results[0]["hook_id"] == "claude-guard"
    assert audit.results[0]["event"] == "PreToolUse"
    assert audit.results[0]["decision"] == "block"
    assert audit.results[0]["reason"] == "no writes here"


def test_the_audit_records_a_hook_that_failed() -> None:
    registry = _registry(_definition("crashing", "import sys; sys.stdin.read(); sys.exit(3)"))
    audit = _RecordingAudit()
    runner = HookRunner(registry, recorder=audit)

    _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert len(audit.invocations) == 1
    assert len(audit.results) == 1
    assert audit.results[0]["decision"] == "allow"
    assert "status 3" in audit.results[0]["reason"]


def test_a_failure_to_record_never_breaks_the_tool_call() -> None:
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("allow", "the path is inside the workspace")),
        )
    )
    runner = HookRunner(registry, recorder=_BrokenAudit())

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "allow"


def test_the_session_log_carries_the_audit_events() -> None:
    store = _FakeStore()
    audit = SessionEventHookAudit(
        store,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("deny", "no writes here")),
        )
    )
    runner = HookRunner(registry, recorder=audit)

    _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert [event["event_type"] for event in store.events] == [HOOK_INVOKED, HOOK_RESULT]
    invoked = store.events[0]["payload"]
    assert invoked["hook_id"] == "claude-guard"
    assert invoked["event"] == "PreToolUse"
    assert invoked["tool_name"] == "fs_write"
    assert invoked["duration_ms"] >= 0
    result = store.events[1]["payload"]
    assert result["hook_id"] == "claude-guard"
    assert result["decision"] == "block"
    assert result["reason"] == "no writes here"


def test_the_audit_recorder_is_off_when_the_deployment_turns_it_off() -> None:
    store = _FakeStore()
    settings = SimpleNamespace(deepspace_hook_audit_enabled=False)

    recorder = hook_audit_recorder(
        settings,
        store,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert recorder is None


def test_the_audit_recorder_is_written_when_the_deployment_keeps_it_on() -> None:
    store = _FakeStore()
    settings = SimpleNamespace(deepspace_hook_audit_enabled=True)

    recorder = hook_audit_recorder(
        settings,
        store,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert recorder is not None


def test_a_failing_session_log_never_breaks_the_tool_call() -> None:
    store = _BrokenStore()
    audit = SessionEventHookAudit(
        store,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    registry = _registry(
        _definition(
            "claude-guard",
            _answer_source(_claude_answer("deny", "no writes here")),
        )
    )
    runner = HookRunner(registry, recorder=audit)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "a.txt"}, "conv-1"))

    assert decision.action == "block"


def test_the_decision_carries_every_field_of_a_claude_answer() -> None:
    decision = HookDecision.from_json(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "no writes here",
                    "additionalContext": "the workspace is read-only",
                },
                "systemMessage": "the user should know",
                "updatedInput": {"path": "safe.txt"},
                "continue": False,
                "stopReason": "the agent must stop",
            }
        )
    )

    assert decision.action == "block"
    assert decision.reason == "no writes here"
    assert decision.additional_context == "the workspace is read-only"
    assert decision.system_message == "the user should know"
    assert decision.updated_input == {"path": "safe.txt"}
    assert decision.stop is True
    assert decision.stop_reason == "the agent must stop"


class _RecordingAudit:
    """A recorder that keeps what it was told."""

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    def record_invoked(self, **fields: Any) -> None:
        """Keep the record of one invocation.

        Args:
            fields: The record's fields.
        """
        self.invocations.append(fields)

    def record_result(self, **fields: Any) -> None:
        """Keep the record of one result.

        Args:
            fields: The record's fields.
        """
        self.results.append(fields)


class _BrokenAudit:
    """A recorder that fails every write."""

    def record_invoked(self, **fields: Any) -> None:
        """Fail the write.

        Args:
            fields: The record's fields.
        """
        raise RuntimeError("the audit store is down")

    def record_result(self, **fields: Any) -> None:
        """Fail the write.

        Args:
            fields: The record's fields.
        """
        raise RuntimeError("the audit store is down")


class _FakeStore:
    """A session event store that keeps what it was told."""

    def __init__(self) -> None:
        self.db = None
        self.events: list[dict[str, Any]] = []

    def append(self, **fields: Any) -> Any:
        """Keep one event.

        Args:
            fields: The event's fields.

        Returns:
            The event.
        """
        self.events.append(fields)
        return SimpleNamespace(**fields)


class _BrokenStore:
    """A session event store that fails every write."""

    db = None

    def append(self, **fields: Any) -> Any:
        """Fail the write.

        Args:
            fields: The event's fields.

        Raises:
            RuntimeError: Always, because the store is down.
        """
        raise RuntimeError("the session log is down")
