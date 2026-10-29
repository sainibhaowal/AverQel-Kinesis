"""Tests for the wall-clock budget every DeepSpace tool call runs under.

A tool that never returns must not stall a turn, so the pipeline gives every
call a deadline and reports a coded timeout when the deadline passes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS, Settings
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.tool_pipeline import (
    TOOL_TIMEOUT_CODE,
    DeepSpaceToolPipeline,
    PreOutcome,
    ToolPipeline,
)

pytestmark = pytest.mark.unit_no_db


def _settings(timeout_seconds: float) -> Settings:
    """Build the deployment settings with one tool deadline.

    Args:
        timeout_seconds: The deadline the deployment configures.

    Returns:
        The settings.
    """
    return Settings(deepspace_tool_timeout_seconds=timeout_seconds)


def test_a_body_that_overruns_its_budget_reports_a_timeout() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy())

    async def body() -> Any:
        await asyncio.sleep(5)
        return {"status": "ok"}

    outcome = asyncio.run(
        pipeline.run(
            tool_name="fs_read", arguments={"path": "a.md"}, body=body, timeout_seconds=0.05
        )
    )

    assert outcome.success is False
    assert outcome.error == "Tool execution exceeded its time budget."
    assert outcome.error_category == "timeout"
    assert outcome.error_code == TOOL_TIMEOUT_CODE
    assert outcome.stages == ("deepspace-policy:allow",)


def test_a_body_that_finishes_in_time_returns_its_payload() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy(), settings=_settings(5.0))

    async def body() -> Any:
        await asyncio.sleep(0)
        return {"status": "ok", "value": 1}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is True
    assert outcome.payload == {"status": "ok", "value": 1}
    assert outcome.error is None
    assert outcome.error_code is None
    assert outcome.stages == ("deepspace-policy:allow", "deepspace-policy:accept")


def test_the_default_budget_comes_from_the_settings() -> None:
    pipeline = DeepSpaceToolPipeline(DeepSpaceToolPolicy(), settings=_settings(0.05))

    async def body() -> Any:
        await asyncio.sleep(5)
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is False
    assert outcome.error_category == "timeout"
    assert outcome.error_code == TOOL_TIMEOUT_CODE


def test_a_per_tool_deadline_overrides_the_default() -> None:
    pipeline = DeepSpaceToolPipeline(
        DeepSpaceToolPolicy(),
        settings=_settings(5.0),
        tool_timeouts={"fs_read": 0.05},
    )

    async def body() -> Any:
        await asyncio.sleep(5)
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is False
    assert outcome.error_category == "timeout"
    assert outcome.error_code == TOOL_TIMEOUT_CODE


def test_a_per_tool_deadline_may_exceed_the_default() -> None:
    pipeline = DeepSpaceToolPipeline(
        DeepSpaceToolPolicy(),
        settings=_settings(0.05),
        tool_timeouts={"fs_read": 5.0},
    )

    async def body() -> Any:
        await asyncio.sleep(0.2)
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is True
    assert outcome.payload == {"status": "ok"}


def test_a_tool_without_a_deadline_runs_under_the_default() -> None:
    pipeline = DeepSpaceToolPipeline(
        DeepSpaceToolPolicy(),
        settings=_settings(0.05),
        tool_timeouts={"web_fetch": 5.0},
    )

    async def body() -> Any:
        await asyncio.sleep(5)
        return {"status": "ok"}

    outcome = asyncio.run(pipeline.run(tool_name="fs_read", arguments={"path": "a.md"}, body=body))

    assert outcome.success is False
    assert outcome.error_code == TOOL_TIMEOUT_CODE


def test_a_denial_still_wins_over_the_budget() -> None:
    pipeline = ToolPipeline()
    pipeline.register_pre("deny-all", lambda _name, _arguments: PreOutcome("deny", "no"))
    calls: list[str] = []

    async def body() -> Any:
        calls.append("body")
        return {"status": "ok"}

    outcome = asyncio.run(
        pipeline.run(tool_name="x", arguments={}, body=body, timeout_seconds=0.05)
    )

    assert outcome.success is False
    assert outcome.error == "no"
    assert outcome.error_code is None
    assert outcome.stages == ("deny-all:deny",)
    assert calls == []


def test_a_pipeline_without_settings_still_has_a_budget() -> None:
    pipeline = ToolPipeline()

    assert pipeline.resolve_timeout("x", None) == DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS


def test_a_deadline_that_is_not_positive_is_refused() -> None:
    with pytest.raises(ValueError):
        DeepSpaceToolPipeline(DeepSpaceToolPolicy(), tool_timeouts={"fs_read": 0})
