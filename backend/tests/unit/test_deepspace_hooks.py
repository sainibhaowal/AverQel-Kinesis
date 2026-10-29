"""Tests for the DeepSpace hook bridge.

The bridge lets a deployment's own command take part in a tool call, so the
checks cover the ways that can go wrong: a manifest that is not usable, a
hook that answers badly, a hook that never answers, and a hook that blocks
a call.

The checks run real hook commands as short-lived Python subprocesses, so the
process boundary itself is covered.  No database and no network are used.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.hooks import (
    MAX_HOOKS,
    HookDecision,
    HookDefinition,
    HookError,
    HookEvent,
    HookRegistry,
    HookRunner,
    hook_environment,
    hook_registry,
)
from app.deepspace.services.tools import hook as hook_tools
from app.deepspace.services.tools._base import ToolContext

pytestmark = pytest.mark.unit_no_db


def _command(source: str) -> list[str]:
    """Build the argv of one hook command.

    Args:
        source: The Python source the hook runs.

    Returns:
        The argv.
    """
    return [sys.executable, "-c", source]


def _context() -> ToolContext:
    """Build a tool context that has no database session.

    Returns:
        The context.
    """
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=SimpleNamespace(),
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )


def _registry(*definitions: HookDefinition) -> HookRegistry:
    """Build a registry that holds the given hooks.

    Args:
        definitions: The hooks the registry holds.

    Returns:
        The registry.
    """
    return HookRegistry(definitions)


def _definition(
    hook_id: str,
    source: str,
    *,
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    matcher: str | None = None,
    timeout_seconds: float = 10.0,
) -> HookDefinition:
    """Build one hook definition for a test.

    Args:
        hook_id: The hook's identifier.
        source: The Python source the hook runs.
        event: The moment the hook takes part in.
        matcher: The tool name filter.
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
    )


def test_a_manifest_is_parsed_and_a_broken_entry_is_skipped() -> None:
    registry = HookRegistry.from_manifest(
        {
            "hooks": [
                {
                    "id": "block-prod-writes",
                    "event": "PreToolUse",
                    "command": ["python3", "guard.py"],
                    "matcher": "fs_write",
                },
                {"id": "no-command", "event": "PreToolUse"},
                {
                    "id": "unknown-event",
                    "event": "BeforeLunch",
                    "command": ["python3", "guard.py"],
                },
            ]
        }
    )

    hooks = registry.all()
    assert [hook.id for hook in hooks] == ["block-prod-writes"]
    assert hooks[0].event is HookEvent.PRE_TOOL_USE
    assert hooks[0].command == ("python3", "guard.py")
    assert hooks[0].matcher == "fs_write"
    assert registry.errors()


def test_a_manifest_that_is_not_an_object_yields_no_hooks() -> None:
    registry = HookRegistry.from_manifest(["not", "a", "manifest"])

    assert registry.all() == ()
    assert registry.errors()


def test_hooks_for_matches_the_event_and_the_tool_matcher() -> None:
    registry = _registry(
        _definition("write-guard", "pass", matcher="fs_write"),
        _definition("read-guard", "pass", matcher="fs_read"),
        _definition("every-tool", "pass"),
        _definition(
            "post-guard",
            "pass",
            event=HookEvent.POST_TOOL_USE,
            matcher="fs_write",
        ),
    )

    pre = registry.hooks_for(HookEvent.PRE_TOOL_USE, "fs_write")
    assert [hook.id for hook in pre] == ["write-guard", "every-tool"]

    post = registry.hooks_for(HookEvent.POST_TOOL_USE, "fs_write")
    assert [hook.id for hook in post] == ["post-guard"]

    assert registry.hooks_for(HookEvent.PRE_TOOL_USE, "web_search") == (registry.all()[2],)
    assert registry.hooks_for(HookEvent.STOP, "fs_write") == ()


def test_hooks_for_refuses_an_unknown_event() -> None:
    registry = _registry()

    with pytest.raises(HookError):
        registry.hooks_for("BeforeLunch", "fs_write")


def test_a_hook_that_blocks_stops_the_call_and_reports_its_reason() -> None:
    registry = _registry(
        _definition(
            "block-prod-writes",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'block', "
            "'reason': 'the tool would delete production data'}))",
            matcher="fs_write",
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {"path": "prod.db"}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "the tool would delete production data"


def test_a_hook_that_asks_returns_the_ask_decision() -> None:
    registry = _registry(
        _definition(
            "ask-before-write",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'ask', 'reason': 'confirm with the user'}))",
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "ask"
    assert decision.reason == "confirm with the user"


def test_the_first_blocking_hook_reports_its_reason() -> None:
    registry = _registry(
        _definition(
            "first-block",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'block', 'reason': 'the first reason'}))",
        ),
        _definition(
            "second-block",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'block', 'reason': 'the second reason'}))",
        ),
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "block"
    assert decision.reason == "the first reason"


def test_an_allowing_hook_keeps_its_reason() -> None:
    registry = _registry(
        _definition(
            "allowing",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'allow', 'reason': 'the path is inside the workspace'}))",
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"
    assert decision.reason == "the path is inside the workspace"


def test_a_hook_that_answers_nothing_allows_the_call() -> None:
    registry = _registry(_definition("silent", "import sys; sys.stdin.read()"))
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"
    assert decision.reason == ""


def test_a_hook_that_exits_non_zero_allows_the_call() -> None:
    registry = _registry(_definition("crashing", "import sys; sys.stdin.read(); sys.exit(3)"))
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"


def test_a_hook_that_prints_invalid_json_allows_the_call() -> None:
    registry = _registry(
        _definition("noisy", "import sys; sys.stdin.read(); print('not json at all')")
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"


def test_a_hook_that_cannot_be_started_allows_the_call() -> None:
    registry = _registry(
        HookDefinition(
            id="missing-command",
            event=HookEvent.PRE_TOOL_USE,
            command=("this-command-does-not-exist-9f3a",),
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"


def test_a_hook_that_hangs_is_timed_out_and_ignored() -> None:
    registry = _registry(
        _definition(
            "hanging",
            "import sys, time; sys.stdin.read(); time.sleep(30)",
            timeout_seconds=1.0,
        )
    )
    runner = HookRunner(registry)

    started = time.monotonic()
    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))
    elapsed = time.monotonic() - started

    assert decision.action == "allow"
    assert elapsed < 20


def test_the_hook_environment_carries_no_deployment_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deployment-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "deployment-secret-value")
    report = tmp_path / "environment.json"
    registry = _registry(
        _definition(
            "env-reader",
            "import json, os, sys; sys.stdin.read(); "
            f"open({str(report)!r}, 'w').write(json.dumps(dict(os.environ)))",
        )
    )
    runner = HookRunner(registry)

    _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    seen = json.loads(report.read_text(encoding="utf-8"))
    assert "DEEPSEEK_API_KEY" not in seen
    assert "AWS_SECRET_ACCESS_KEY" not in seen
    assert seen["DEEPSPACE_HOOK"] == "1"
    assert set(seen) <= {"PATH", "HOME", "LANG", "DEEPSPACE_HOOK"}


def test_the_environment_builder_keeps_only_the_names_a_hook_needs(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSPACE_TOKEN", "deployment-secret-value")

    environment = hook_environment()

    assert environment["DEEPSPACE_HOOK"] == "1"
    assert "DEEPSPACE_TOKEN" not in environment
    assert set(environment) <= {"PATH", "HOME", "LANG", "DEEPSPACE_HOOK"}


def test_the_hook_receives_the_event_on_standard_input(tmp_path: Path) -> None:
    report = tmp_path / "payload.json"
    registry = _registry(
        _definition(
            "payload-reader",
            "import json, sys; "
            f"open({str(report)!r}, 'w').write(sys.stdin.read()); "
            "print(json.dumps({'decision': 'allow'}))",
        )
    )
    runner = HookRunner(registry)

    _run(runner.run_pre_tool("fs_write", {"path": "notes.txt"}, "conv-1"))

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload == {
        "event": "PreToolUse",
        "tool_name": "fs_write",
        "arguments": {"path": "notes.txt"},
        "conversation_id": "conv-1",
    }


def test_a_post_tool_hook_adds_context_for_the_model() -> None:
    registry = _registry(
        _definition(
            "note-writer",
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps({'decision': 'allow', "
            "'additional_context': 'the file was written to the workspace'}))",
            event=HookEvent.POST_TOOL_USE,
        )
    )
    runner = HookRunner(registry)

    context = _run(runner.run_post_tool("fs_write", {"status": "ok"}, "conv-1"))

    assert context == "the file was written to the workspace"


def test_a_post_tool_hook_that_adds_nothing_returns_an_empty_context() -> None:
    registry = _registry(
        _definition(
            "quiet",
            "import json, sys; sys.stdin.read(); print(json.dumps({'decision': 'allow'}))",
            event=HookEvent.POST_TOOL_USE,
        )
    )
    runner = HookRunner(registry)

    assert _run(runner.run_post_tool("fs_write", {"status": "ok"}, "conv-1")) == ""


def test_a_hook_that_answers_with_a_huge_blob_allows_the_call() -> None:
    registry = _registry(
        _definition(
            "noisy",
            "import sys; sys.stdin.read(); print('x' * 70000)",
        )
    )
    runner = HookRunner(registry)

    decision = _run(runner.run_pre_tool("fs_write", {}, "conv-1"))

    assert decision.action == "allow"


def test_a_registry_holds_a_bounded_number_of_hooks() -> None:
    registry = HookRegistry()

    added = registry.extend(_definition(f"hook-{index}", "pass") for index in range(MAX_HOOKS + 5))

    assert added == MAX_HOOKS
    assert len(registry.all()) == MAX_HOOKS
    assert registry.errors()


def test_a_decision_is_read_from_the_json_a_hook_answers_with() -> None:
    decision = HookDecision.from_json(
        '{"decision": "block", "reason": "no writes here", "additional_context": "note"}'
    )

    assert decision.action == "block"
    assert decision.reason == "no writes here"
    assert decision.additional_context == "note"


def test_a_decision_without_a_decision_allows_the_call() -> None:
    decision = HookDecision.from_json('{"additional_context": "note"}')

    assert decision.action == "allow"
    assert decision.additional_context == "note"


def test_a_decision_that_is_not_json_is_refused() -> None:
    with pytest.raises(HookError):
        HookDecision.from_json("not json at all")


def test_a_decision_with_an_unknown_action_is_refused() -> None:
    with pytest.raises(HookError):
        HookDecision.from_json('{"decision": "maybe"}')


def test_the_hook_run_tool_reports_a_block_with_its_reason() -> None:
    registry = _registry(
        _definition(
            "block-prod-writes",
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'decision': 'block', 'reason': 'not allowed here'}))",
            matcher="fs_write",
        )
    )

    result = _with_registry(registry, lambda: _run_hook_tool("PreToolUse", "fs_write"))

    assert result == {
        "status": "ok",
        "decision": "block",
        "reason": "not allowed here",
        "additional_context": "",
    }


def test_the_hook_run_tool_returns_an_error_dict_instead_of_raising() -> None:
    registry = _registry()
    handler = hook_tools.HOOK_TOOLS[0].handler

    async def _call() -> dict[str, Any]:
        return await handler({"event": "BeforeLunch"}, _context())

    result = _with_registry(registry, lambda: _run(_call()))
    assert result["status"] == "error"
    assert "BeforeLunch" in str(result["message"])


def test_the_hook_run_tool_works_without_a_database_session() -> None:
    registry = _registry(
        _definition(
            "allow-everything",
            "import json, sys; sys.stdin.read(); print(json.dumps({'decision': 'allow'}))",
        )
    )

    result = _with_registry(registry, lambda: _run_hook_tool("PreToolUse", "fs_write"))

    assert result["status"] == "ok"
    assert result["decision"] == "allow"


def test_the_hook_run_tool_is_declared_as_a_read_tool() -> None:
    spec = hook_tools.HOOK_TOOLS[0]

    assert spec.name == "hook_run"
    assert spec.mode == "read"
    assert spec.definition["function"]["parameters"]["required"] == ["event"]


def test_the_process_wide_registry_is_the_same_object_every_time() -> None:
    assert hook_registry() is hook_registry()


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion.

    Args:
        coroutine: The coroutine to run.

    Returns:
        Its result.
    """
    return asyncio.run(coroutine)


def _with_registry(registry: HookRegistry, call: Any) -> Any:
    """Run a call with the process-wide registry replaced.

    Args:
        registry: The registry the call reads.
        call: The call to run.

    Returns:
        The call's result.
    """
    process_wide = hook_registry()
    previous = process_wide.all()
    process_wide.replace(registry.all())
    try:
        return call()
    finally:
        process_wide.replace(previous)


def _run_hook_tool(event: str, tool_name: str) -> dict[str, Any]:
    """Run the hook tool for one event.

    Args:
        event: The event to ask about.
        tool_name: The tool the call would make.

    Returns:
        The tool's result.
    """
    handler = hook_tools.HOOK_TOOLS[0].handler

    async def _call() -> dict[str, Any]:
        return await handler(
            {"event": event, "tool_name": tool_name, "arguments": {"path": "notes.txt"}},
            _context(),
        )

    return _run(_call())
