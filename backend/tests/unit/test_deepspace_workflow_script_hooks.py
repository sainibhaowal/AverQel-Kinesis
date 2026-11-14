"""Tests for the JavaScript hook contract a workflow script may use.

The tests run the real Node.js PTC runtime with a stubbed child runner, so
the script that DeepSpace generates is executed as the model would execute
it.  That is the only way to prove the contract the model reads: a child
answer reaches the script, a schema shapes that answer, and a provider
override reaches the child.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.services import workflow_children as workflow_children_module
from app.deepspace.services.sandbox_backend import BwrapBackend
from app.deepspace.services.subagent_runner import SubagentOutcome
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workflow_engine import (
    WorkflowEngine,
    WorkflowMeta,
    WorkflowStartRequest,
)

pytestmark = pytest.mark.unit_no_db

requires_node = pytest.mark.skipif(
    shutil.which("node") is None or not BwrapBackend().available(),
    reason="the Node PTC runtime needs node and bubblewrap",
)


class FakeDb:
    """The smallest session the child seam needs."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def add(self, record: Any) -> None:
        self.records.append(record)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None


def _context(tmp_path: Path) -> ToolContext:
    """Build the tool context one workflow script runs under."""
    auth = AuthContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        roles=frozenset({"user"}),
        token_id="test",
    )

    async def executor(_tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    return ToolContext(
        auth=auth,
        conversation_id=uuid4(),
        settings=get_settings(),
        db=None,
        task_store=None,
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        provider_selection=SimpleNamespace(),
        native_executor=executor,
        workspace_root=tmp_path,
    )


def _install_child_runner(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, str] | None = None,
    *,
    stop_reason: str = "completed",
) -> list[dict[str, Any]]:
    """Replace the child's turn with a scripted answer and record its request.

    Args:
        monkeypatch: The patcher that installs the stub.
        answers: The answers per child label; a missing label answers with
            the label itself.
        stop_reason: How the child stops.

    Returns:
        The list that receives each child's request.
    """
    seen: list[dict[str, Any]] = []

    @contextmanager
    def fake_session():
        yield FakeDb()

    monkeypatch.setattr(workflow_children_module, "managed_db_session", fake_session)
    monkeypatch.setattr(workflow_children_module, "set_db_tenant_context", lambda *_args: None)

    async def fake_run_child_turn(*, context, child, task):
        seen.append(
            {
                "label": child.label,
                "task": task,
                "provider": child.agent_provider,
                "model": child.agent_model,
            }
        )
        answer = (answers or {}).get(child.label, f"answer:{child.label}")
        return SubagentOutcome(text=answer, stop_reason=stop_reason)

    monkeypatch.setattr(workflow_children_module, "run_child_turn", fake_run_child_turn)
    return seen


async def _run_script(tmp_path: Path, script: str, meta: WorkflowMeta | None = None):
    """Run one workflow script and return the finished run's value."""
    engine = WorkflowEngine(get_settings(), _context(tmp_path))
    run = await engine.start(
        WorkflowStartRequest(
            script=script,
            meta=meta or WorkflowMeta(name="hook-test"),
        )
    )
    try:
        return await run.wait()
    finally:
        await run.dispose()


@requires_node
async def test_agent_hook_resolves_to_the_child_answer(tmp_path: Path) -> None:
    """A child answer reaches the script instead of resolving to undefined."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch, {"alpha": "first answer", "beta": "second answer"})
        value = await _run_script(
            tmp_path,
            """
            const a = await workflow.agent('first task', { label: 'alpha' });
            const b = await workflow.agent('second task', { label: 'beta' });
            return { a, b };
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == {"a": "first answer", "b": "second answer"}


@requires_node
async def test_agent_hook_with_a_schema_resolves_to_validated_json(
    tmp_path: Path,
) -> None:
    """A schema turns the child's answer into a validated object."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(
            monkeypatch,
            {"judge": '{"score": 42, "verdict": "ok"}'},
        )
        value = await _run_script(
            tmp_path,
            """
            const verdict = await workflow.agent('judge it', {
              label: 'judge',
              schema: {
                type: 'object',
                properties: { score: { type: 'integer' }, verdict: { type: 'string' } },
                required: ['score', 'verdict'],
                additionalProperties: false,
              },
            });
            return verdict;
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == {"score": 42, "verdict": "ok"}


@requires_node
async def test_agent_hook_with_a_schema_resolves_to_null_when_the_child_fails(
    tmp_path: Path,
) -> None:
    """A failed child yields null rather than ending the script."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch, stop_reason="error")
        value = await _run_script(
            tmp_path,
            """
            const verdict = await workflow.agent('judge it', {
              label: 'judge',
              schema: {
                type: 'object',
                properties: { score: { type: 'integer' } },
                required: ['score'],
              },
            });
            return { verdict, survived: true };
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == {"verdict": None, "survived": True}


@requires_node
async def test_agent_hook_forwards_the_provider_and_model(tmp_path: Path) -> None:
    """A provider override reaches the child that runs."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = _install_child_runner(monkeypatch)
        await _run_script(
            tmp_path,
            "return await workflow.agent('do it', { provider: 'openai', model: 'gpt-4o' });",
        )
    finally:
        monkeypatch.undo()

    assert len(seen) == 1
    assert seen[0]["provider"] == "openai"
    assert seen[0]["model"] == "gpt-4o"


@requires_node
async def test_pipeline_runs_every_item_through_every_stage(tmp_path: Path) -> None:
    """The per-item pipeline resolves to one result per item."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            return await workflow.pipeline(
              [1, 2, 3],
              async (previous, item, index) => previous + item,
              async (previous, item, index) => previous * 10,
            );
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == [20, 40, 60]


@requires_node
async def test_pipeline_drops_the_item_whose_stage_throws(tmp_path: Path) -> None:
    """A stage that throws leaves that item null and keeps the others."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            return await workflow.pipeline(
              [1, 2, 3],
              async (previous, item) => {
                if (item === 2) throw new Error('item two failed');
                return previous + item;
              },
            );
            """,
        )
    finally:
        monkeypatch.undo()

    # Each stage receives (previous, item, index), and the first stage sees
    # the item itself as the previous value.
    assert value["value"] == [2, None, 6]


@requires_node
async def test_legacy_pipeline_chain_still_works(tmp_path: Path) -> None:
    """The original chain form runs one value through the stages."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            return await workflow.pipeline(
              [async (previous) => previous + 1, async (previous) => previous * 3],
              { initial: 5 },
            );
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == 18


@requires_node
async def test_parallel_runs_the_mapper_over_every_item(tmp_path: Path) -> None:
    """The parallel hook resolves to one mapper result per item."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            return await workflow.parallel(
              ['a', 'b'],
              async (item) => {
                const answer = await workflow.agent('handle ' + item, { label: item });
                return answer;
              },
            );
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"] == ["answer:a", "answer:b"]


@requires_node
async def test_the_earlier_direct_host_call_still_works(tmp_path: Path) -> None:
    """A script that calls the host binding directly still gets an answer."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch, {"reader": "direct answer"})
        value = await _run_script(
            tmp_path,
            """
            const result = await tool('subagent', {
              description: 'reader',
              task: 'read the files',
              mode: 'one-shot',
            });
            return { answer: result.answer, agent: result.agent_id };
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"]["answer"] == "direct answer"
    assert value["value"]["agent"]


@requires_node
async def test_misused_agent_hook_ends_the_script(tmp_path: Path) -> None:
    """A hook called with a bad argument fails the run instead of returning null."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            try {
              await workflow.agent('');
              return { survived: true };
            } catch (error) {
              return { survived: false, message: String(error.message || error) };
            }
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["value"]["survived"] is False
    assert "task" in value["value"]["message"]


@requires_node
async def test_agent_cap_ends_the_script(tmp_path: Path) -> None:
    """A script that starts more children than the workflow allows fails."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            try {
              await workflow.agent('one', { label: 'one' });
              await workflow.agent('two', { label: 'two' });
              return { survived: true };
            } catch (error) {
              return { survived: false, message: String(error.message || error) };
            }
            """,
            meta=WorkflowMeta(name="capped", max_total_agents=1),
        )
    finally:
        monkeypatch.undo()

    assert value["value"]["survived"] is False
    assert "cap" in value["value"]["message"]


@requires_node
async def test_unsupported_schema_fails_the_run_before_a_child_runs(
    tmp_path: Path,
) -> None:
    """A schema outside the supported subset stops the run."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = _install_child_runner(monkeypatch)
        value = await _run_script(
            tmp_path,
            """
            return await workflow.agent('judge it', {
              label: 'judge',
              schema: { type: 'object', properties: { score: { type: 'integer', minimum: 0 } } },
            });
            """,
        )
    finally:
        monkeypatch.undo()

    assert value["status"] == "failed"
    assert seen == []
