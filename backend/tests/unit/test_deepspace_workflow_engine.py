"""Focused lifecycle tests for the shared workflow engine seam."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.deepspace.services.tools.workflow import WORKFLOW_TOOLS
from app.deepspace.services.workflow_engine import (
    WorkflowEngine,
    WorkflowMeta,
    WorkflowPhase,
    WorkflowStartRequest,
)

pytestmark = pytest.mark.unit_no_db


def _request(*, background: bool = False) -> WorkflowStartRequest:
    return WorkflowStartRequest(
        script="workflow.phase('read'); workflow.log('hello'); return { ok: true };",
        meta=WorkflowMeta(name="test-workflow"),
        args={"input": "value"},
        run_in_background=background,
    )


def test_workflow_tool_declares_background_flag() -> None:
    parameters = WORKFLOW_TOOLS[0].definition["function"]["parameters"]
    assert parameters["properties"]["run_in_background"] == {
        "type": "boolean",
        "description": "Queue the workflow and return a durable job id without waiting for completion.",
    }


def test_workflow_program_exposes_phase_log_and_return_value() -> None:
    engine = WorkflowEngine(SimpleNamespace(), None)
    run = asyncio.run(engine.start(_request()))
    program = engine._build_ptc_program(run)
    assert "workflow.phase" in program
    assert "workflow.log" in program
    assert "returnValue" in program
    asyncio.run(run.dispose())


def test_foreground_handle_waits_and_disposes() -> None:
    async def scenario() -> None:
        engine = WorkflowEngine(SimpleNamespace(), None)

        async def fake_run(run) -> dict[str, bool]:
            await asyncio.sleep(0)
            return {"ok": True}

        engine._run_workflow_script = fake_run  # type: ignore[method-assign]
        run = await engine.start(_request())
        assert await run.wait() == {"ok": True}
        assert run.status is WorkflowPhase.COMPLETED
        await run.dispose()
        await run.dispose()
        assert run._disposed is True

    asyncio.run(scenario())


def test_cancelled_handle_finishes_with_cancelled_status() -> None:
    async def scenario() -> None:
        engine = WorkflowEngine(SimpleNamespace(), None)
        gate = asyncio.Event()

        async def fake_run(run) -> dict[str, bool]:
            await gate.wait()
            return {"ok": True}

        engine._run_workflow_script = fake_run  # type: ignore[method-assign]
        run = await engine.start(_request())
        await asyncio.sleep(0)
        await run.cancel()
        result = await run.wait()
        assert run.status is WorkflowPhase.CANCELLED
        assert result["status"] == "cancelled"
        await run.dispose()

    asyncio.run(scenario())
