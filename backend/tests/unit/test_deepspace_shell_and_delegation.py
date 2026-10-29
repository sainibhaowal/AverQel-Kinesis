"""Tests for the DeepSpace shell tools, the delegation tools, and the
compaction pipeline."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.compaction_pipeline import (
    compact_request,
    elide_stale_messages,
    offload_images,
    prune_tool_results,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.subagent_runner import child_tool_names
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext

pytestmark = pytest.mark.unit_no_db


def _context(tmp_path: Path, **overrides: Any) -> ToolContext:
    """Build a tool context double for the shell and delegation tools."""
    settings = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_sandbox_backend": "none",
        "deepspace_shell_timeout_seconds": 15,
        "deepspace_shell_max_output_bytes": 65_536,
        "deepspace_subagent_max_depth": 1,
        "deepspace_subagent_max_children": 4,
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


def test_bash_runs_a_command_and_reports_its_exit_code(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo deepspace-ok"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "deepspace-ok" in str(result["output"])


def test_bash_reports_a_failing_command_without_raising(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = asyncio.run(dispatch_native_tool("bash", {"command": "exit 3"}, context))

    assert result is not None
    assert result["status"] == "failed"
    assert result["exit_code"] == 3


def test_bash_is_refused_by_the_read_only_permission_mode(tmp_path: Path) -> None:
    context = _context(tmp_path, deepspace_permission_mode="read-only")

    result = asyncio.run(dispatch_native_tool("bash", {"command": "rm -rf build"}, context))

    assert result is not None
    assert result["status"] == "denied"
    assert "read-only" in str(result["message"])


def test_sandbox_policy_overrides_the_permission_mode_for_shell(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        deepspace_permission_mode="danger-full-access",
        deepspace_sandbox_policy="read-only",
    )

    result = asyncio.run(dispatch_native_tool("bash", {"command": "rm -rf build"}, context))

    assert result is not None
    assert result["status"] == "denied"


def test_bash_runs_a_read_only_command_in_the_read_only_mode(tmp_path: Path) -> None:
    context = _context(tmp_path, deepspace_permission_mode="read-only")

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo read-only-ok"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert "read-only-ok" in str(result["output"])


def test_bash_starts_inside_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = asyncio.run(dispatch_native_tool("bash", {"command": "pwd"}, context))

    assert result is not None
    assert result["cwd"] == "."


def test_bash_reports_a_disabled_deployment(tmp_path: Path) -> None:
    context = _context(tmp_path, deepspace_shell_enabled=False)

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo hi"}, context))

    assert result is not None
    assert result["status"] == "unavailable"


def test_a_pruned_tool_result_keeps_its_head_and_tail() -> None:
    messages = [
        {"role": "user", "content": "run it"},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 20_000},
    ]

    pruned, count = prune_tool_results(messages, threshold=1_000)

    assert count == 1
    assert len(str(pruned[1]["content"])) < 20_000
    assert "truncated" in str(pruned[1]["content"])


def test_a_small_tool_result_is_left_alone() -> None:
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "small"}]

    pruned, count = prune_tool_results(messages, threshold=1_000)

    assert count == 0
    assert pruned == messages


def test_images_beyond_the_budget_are_offloaded() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"data": "a" * 100}},
                {"type": "image", "source": {"data": "b" * 100}},
                {"type": "image", "source": {"data": "c" * 100}},
            ],
        }
    ]

    offloaded_messages, count = offload_images(messages, image_budget=1)

    assert count == 2
    remaining = [
        item
        for item in offloaded_messages[0]["content"]
        if isinstance(item, dict) and item.get("type") == "image"
    ]
    assert len(remaining) == 1


def test_the_compaction_pipeline_reports_what_it_changed() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 30_000},
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"data": "a" * 100}},
                {"type": "image", "source": {"data": "b" * 100}},
            ],
        },
    ]

    outcome = compact_request(messages, prune_threshold=1_000, image_budget=0)

    assert outcome.pruned_tool_results == 1
    assert outcome.offloaded_images == 2
    assert outcome.changed is True


def test_the_compaction_pipeline_leaves_a_small_request_alone() -> None:
    messages = [{"role": "user", "content": "hello"}]

    outcome = compact_request(messages)

    assert outcome.changed is False
    assert outcome.messages == messages


def test_a_tool_result_payload_is_shortened_for_the_model() -> None:
    from app.deepspace.services.compaction_pipeline import compact_tool_result_payload

    text, shortened = compact_tool_result_payload({"value": "z" * 5_000}, threshold=500)

    assert shortened is True
    assert len(text) <= 500
    assert "tool result truncated" in text
    assert text.startswith('{"value": "z')


class _ToolNameRegistry:
    """A minimal registry double for the child tool filter."""

    def names(self) -> list[str]:
        return ["list_agents", "read", "send_message", "subagent"]


def test_subagent_defaults_to_a_continuable_child() -> None:
    from app.deepspace.services.tools.delegation import (
        SubagentArguments,
        SubagentForkArguments,
    )

    assert SubagentArguments(description="research", task="find it").mode == "continuable"
    assert SubagentForkArguments(description="research", task="find it").mode == "one-shot"


def test_child_tools_offer_delegation_below_the_depth_limit() -> None:
    child = SimpleNamespace(tool_filter={}, delegation_depth=0)

    names = child_tool_names(_ToolNameRegistry(), child, max_depth=2)

    assert {"subagent", "send_message", "list_agents"} <= set(names)


def test_child_tools_withhold_delegation_at_the_depth_limit() -> None:
    child = SimpleNamespace(tool_filter={}, delegation_depth=2)

    names = child_tool_names(_ToolNameRegistry(), child, max_depth=2)

    assert names == ["read"]


def test_child_tools_apply_the_deny_filter() -> None:
    child = SimpleNamespace(tool_filter={"deny": ["subagent"]}, delegation_depth=0)

    names = child_tool_names(_ToolNameRegistry(), child, max_depth=2)

    assert "subagent" not in names
    assert "send_message" in names


def test_stale_messages_are_elided_without_splitting_a_tool_exchange() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "start"},
    ]
    for index in range(10):
        messages.append({"role": "assistant", "content": f"turn {index}"})
        messages.append({"role": "user", "content": f"next {index}"})
    messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]})
    messages.append({"role": "tool", "tool_call_id": "c1", "content": "result"})

    elided, count = elide_stale_messages(messages, keep_recent=4)

    assert count > 0
    assert elided[0] == {"role": "system", "content": "instructions"}
    assert elided[1] == {"role": "user", "content": "start"}
    assert "elided" in str(elided[2]["content"])
    assert elided[-2]["role"] == "assistant"
    assert elided[-1]["role"] == "tool"


def test_compaction_elides_before_pruning() -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "instructions"}]
    for index in range(30):
        messages.append({"role": "user", "content": f"message {index}"})

    outcome = compact_request(messages, keep_recent=4)

    assert outcome.elided_messages > 0
    assert outcome.changed is True
    assert outcome.messages[0]["content"] == "instructions"
    assert outcome.messages[-1]["content"] == "message 29"


def test_ask_mode_pauses_a_destructive_native_tool() -> None:
    policy = DeepSpaceToolPolicy(approval_mode="ask", permission_mode="workspace-write")

    decision = policy.decide("fs_write", {})

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk_level == "high"


def test_ask_mode_does_not_pause_a_read_tool() -> None:
    policy = DeepSpaceToolPolicy(approval_mode="ask")

    decision = policy.decide("read", {})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_never_mode_keeps_write_tools_unattended() -> None:
    policy = DeepSpaceToolPolicy(approval_mode="never")

    decision = policy.decide("delete", {})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_read_only_preset_refuses_write_tools() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="read-only")

    decision = policy.decide("write", {"target": "library"})

    assert decision.allowed is False
    assert "read-only" in str(decision.reason)


def test_read_only_preset_still_allows_read_tools() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="read-only")

    decision = policy.decide("fs_read", {})

    assert decision.allowed is True
