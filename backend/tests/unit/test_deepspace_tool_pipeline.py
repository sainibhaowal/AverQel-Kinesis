"""Tests for the DeepSpace tool pipeline and the workspace targets of the
unified read, edit, and delete tools."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.tool_pipeline import (
    DeepSpaceToolPipeline,
    PostOutcome,
    PreOutcome,
    ToolPipeline,
)

pytestmark = pytest.mark.unit_no_db


def test_the_policy_participates_first_and_last() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy())

    assert pipeline.participants == ("deepspace-policy", "deepspace-policy")


def test_a_denied_call_never_reaches_the_body() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy())
    calls: list[str] = []

    async def body() -> Any:
        calls.append("body")
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="not_a_tool", arguments={}, body=body))

    assert outcome.success is False
    assert calls == []
    assert "allowlist" in str(outcome.error)


def test_an_allowed_call_reaches_the_body() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy())

    async def body() -> Any:
        return {"status": "ok", "value": 1}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is True
    assert outcome.payload == {"status": "ok", "value": 1}
    assert outcome.stages == ("deepspace-policy:allow", "deepspace-policy:accept")


def test_a_pre_participant_can_deny_a_call() -> None:
    pipeline = ToolPipeline()
    pipeline.register_pre("deny-all", lambda _name, _arguments: PreOutcome("deny", "no"))

    async def body() -> Any:
        raise AssertionError("the body must not run")

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is False
    assert outcome.error == "no"


def test_a_pre_participant_can_cancel_a_call() -> None:
    pipeline = ToolPipeline()
    pipeline.register_pre("cancel", lambda _name, _arguments: PreOutcome("cancel", "stop"))

    async def body() -> Any:
        raise AssertionError("the body must not run")

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is False
    assert outcome.error_category == "cancelled"


def test_a_post_participant_can_replace_the_result() -> None:
    pipeline = ToolPipeline()
    pipeline.register_post(
        "redact", lambda _name, payload: PostOutcome("replace", {"redacted": True})
    )

    async def body() -> Any:
        return {"secret": "value"}

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is True
    assert outcome.payload == {"redacted": True}


def test_a_post_participant_can_block_the_result() -> None:
    pipeline = ToolPipeline()
    pipeline.register_post("block", lambda _name, _payload: PostOutcome("block", None, "no"))

    async def body() -> Any:
        return {"secret": "value"}

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is False
    assert outcome.error_category == "blocked"
    assert outcome.error == "no"


def test_a_failing_participant_does_not_fail_the_call() -> None:
    pipeline = ToolPipeline()

    def broken(_name: str, _arguments: dict[str, Any]) -> PreOutcome:
        raise RuntimeError("participant is broken")

    pipeline.register_pre("broken", broken)

    async def body() -> Any:
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is True
    assert outcome.payload == {"status": "ok"}


def test_a_failing_body_becomes_a_tool_failure() -> None:
    pipeline = ToolPipeline()

    async def body() -> Any:
        raise RuntimeError("the tool broke")

    outcome = asyncio.run(pipeline.run(tool_name="x", arguments={}, body=body))

    assert outcome.success is False
    assert outcome.error_category == "tool"
    assert "the tool broke" in str(outcome.error)


def test_a_body_that_overruns_its_budget_is_stopped() -> None:
    pipeline = ToolPipeline()

    async def body() -> Any:
        await asyncio.sleep(5)
        return {"status": "ok"}

    outcome = asyncio.run(
        pipeline.run(tool_name="x", arguments={}, body=body, timeout_seconds=0.05)
    )

    assert outcome.success is False
    assert outcome.error_category == "timeout"


def test_the_workspace_targets_are_accepted_by_the_registry() -> None:
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY

    read = NATIVE_TOOL_REGISTRY.validate("read", {"target": "workspace", "path": "a.md"})
    edit = NATIVE_TOOL_REGISTRY.validate(
        "edit", {"target": "workspace", "operation": "replace", "path": "a.md", "content": "x"}
    )
    delete = NATIVE_TOOL_REGISTRY.validate("delete", {"target": "workspace", "path": "a.md"})

    assert read["target"] == "workspace"
    assert read["path"] == "a.md"
    assert edit["target"] == "workspace"
    assert delete["target"] == "workspace"
