"""Focused tests for dependency waves in named workflows."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.auth.dependencies import AuthContext
from app.deepspace.services.subagent_runner import SubagentOutcome
from app.deepspace.services.tools import workflow as workflow_module
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workflow_engine import (
    WorkflowEngine,
    WorkflowMeta,
    WorkflowPhase,
    WorkflowRun,
    WorkflowStartRequest,
)

pytestmark = pytest.mark.unit_no_db


def test_workflow_run_transitions_to_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asynchronously updated run exposes its completed state and result."""
    engine = WorkflowEngine(SimpleNamespace())
    run = WorkflowRun(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        script="return 1",
        meta=WorkflowMeta(name="test"),
        args=None,
        subagent_provider="spawn",
        max_total_agents=1,
        max_concurrent_agents=1,
    )

    async def fake_run_script(_run: WorkflowRun) -> dict[str, str]:
        return {"status": "completed"}

    monkeypatch.setattr(engine, "_run_workflow_script", fake_run_script)

    async def start_and_wait() -> WorkflowRun:
        started = await engine.start(
            WorkflowStartRequest(script=run.script, meta=run.meta, args=run.args)
        )
        await started.wait()
        return started

    run = asyncio.run(start_and_wait())

    assert run.status is WorkflowPhase.COMPLETED
    assert run.result == {"status": "completed"}
    assert run.completed_at is not None


def test_workflow_waves_keep_independent_steps_together() -> None:
    """The dependency planner creates parallel-ready waves and detects cycles."""
    steps = [
        {"id": "a", "task": "A", "depends_on": []},
        {"id": "b", "task": "B", "depends_on": []},
        {"id": "c", "task": "C", "depends_on": ["a", "b"]},
    ]
    assert [[step["id"] for step in wave] for wave in workflow_module._workflow_waves(steps)] == [
        ["a", "b"],
        ["c"],
    ]
    with pytest.raises(ValueError, match="unique"):
        workflow_module._workflow_steps(
            {"steps": [{"id": "same", "task": "A"}, {"id": "same", "task": "B"}]}
        )


def test_named_workflow_runs_independent_children_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named workflows fan out and pass completed dependency results forward."""
    workflow_path = tmp_path / ".averqel" / "workflows"
    workflow_path.mkdir(parents=True)
    (workflow_path / "parallel.json").write_text(
        '{"max_concurrency": 2, "steps": ['
        '{"id":"a","task":"first"},'
        '{"id":"b","task":"second"},'
        '{"id":"c","task":"third","depends_on":["a","b"]}'
        "]}",
        encoding="utf-8",
    )

    class FakeDb:
        def __init__(self) -> None:
            self.records = []

        def add(self, record) -> None:
            self.records.append(record)

        def commit(self) -> None:
            return None

    db = FakeDb()

    @contextmanager
    def fake_session():
        yield db

    monkeypatch.setattr(workflow_module, "managed_db_session", fake_session)
    monkeypatch.setattr(workflow_module, "set_db_tenant_context", lambda *_args: None)

    active = 0
    peak = 0
    seen_tasks: list[str] = []

    async def fake_run_child_turn(*, context, child, task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen_tasks.append(task)
        await asyncio.sleep(0.01)
        active -= 1
        return SubagentOutcome(text=child.label.rsplit(":", 1)[-1].upper(), stop_reason="completed")

    monkeypatch.setattr(workflow_module, "run_child_turn", fake_run_child_turn)
    auth = AuthContext(
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        roles=frozenset({"user"}),
        token_id="test",
    )
    context = ToolContext(
        auth=auth,
        conversation_id=UUID("33333333-3333-3333-3333-333333333333"),
        settings=SimpleNamespace(),
        db=db,
        task_store=None,
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        provider_selection=SimpleNamespace(),
        native_executor=None,
        workspace_root=tmp_path,
    )

    result = asyncio.run(workflow_module._workflow_run({"name": "parallel"}, context))

    assert result["status"] == "ok"
    assert [step["step_id"] for step in result["steps"]] == ["a", "b", "c"]
    assert peak == 2
    assert any("Result of a:\nA" in task and "Result of b:\nB" in task for task in seen_tasks)
