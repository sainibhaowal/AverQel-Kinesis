"""Tests for the DeepSpace job_output tool."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.deepspace.models.agent_runtime import DeepSpaceAgentRun, DeepSpaceAgentStep
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.services.tools import job_output
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.job_output import (
    JOB_OUTPUT_TOOLS,
    JobOutputArguments,
    _JobRecords,
)

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()


def _context() -> ToolContext:
    """Build a tool context for the calling conversation."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=SimpleNamespace(),
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _turn(job_id: uuid.UUID, **overrides: Any) -> DeepSpaceQueuedTurn:
    """Build one queued turn of the calling conversation."""
    values: dict[str, Any] = {
        "id": job_id,
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "client_request_id": "request-1",
        "prompt": "Do the thing.",
        "priority": 0,
        "sequence": 1,
        "thinking_enabled": False,
        "roles_json": [],
        "permissions_json": [],
        "attachment_file_ids_json": [],
        "status": "completed",
    }
    values.update(overrides)
    return DeepSpaceQueuedTurn(**values)


def _step(
    sequence: int, step_type: str, result: dict[str, Any], **overrides: Any
) -> DeepSpaceAgentStep:
    """Build one recorded step of a run."""
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "sequence": sequence,
        "step_type": step_type,
        "status": "completed",
        "input_json": {},
        "result_json": result,
    }
    values.update(overrides)
    return DeepSpaceAgentStep(**values)


def _records(
    job_id: uuid.UUID,
    *,
    turn_status: str = "completed",
    turn_error: str | None = None,
    run_status: str | None = "completed",
    run_error: str | None = None,
    steps: tuple[DeepSpaceAgentStep, ...] = (),
    message_content: str = "",
) -> _JobRecords:
    """Build the durable records of one finished job."""
    turn = _turn(job_id, status=turn_status, error=turn_error)
    run = None
    if run_status is not None:
        run = DeepSpaceAgentRun(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            assistant_message_id=uuid.uuid4(),
            status=run_status,
            cancel_requested=False,
            step_count=len(steps),
            last_sequence=len(steps),
            checkpoint={},
            last_error=run_error,
        )
    return _JobRecords(
        job_id=str(job_id),
        turn=turn,
        run=run,
        steps=steps,
        message_content=message_content,
    )


@dataclass
class _FakeJob:
    """One job the fake store holds, with the conversation that owns it."""

    conversation_id: uuid.UUID
    records: _JobRecords


class _FakeStore:
    """A stand-in for the durable job store.

    It holds the same scoping the real store applies: a job of another
    conversation is not found.
    """

    def __init__(
        self,
        conversation_id: uuid.UUID,
        jobs: dict[str, _FakeJob],
        sequence: list[_JobRecords] | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.jobs = jobs
        self.sequence = sequence or []
        self.load_count = 0

    def load(self, job_id: uuid.UUID) -> _JobRecords | None:
        """Return one job of the calling conversation."""
        self.load_count += 1
        job = self.jobs.get(str(job_id))
        if job is None or job.conversation_id != self.conversation_id:
            return None
        if self.sequence:
            index = min(self.load_count - 1, len(self.sequence) - 1)
            return self.sequence[index]
        return job.records


class _BrokenStore:
    """A store whose read fails."""

    def load(self, job_id: uuid.UUID) -> _JobRecords | None:
        """Fail the way a lost database connection fails."""
        raise RuntimeError("the database connection is gone")


class _EmptyResult:
    """A result set with no rows."""

    def scalar_one_or_none(self) -> None:
        """Return no row."""
        return None

    def scalars(self) -> _EmptyResult:
        """Return the row set."""
        return self

    def all(self) -> list[Any]:
        """Return no rows."""
        return []


class _RecordingSession:
    """A session that records the statements the store builds."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _EmptyResult:
        """Record one statement and answer it with no rows."""
        self.statements.append(statement)
        return _EmptyResult()


def _install(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    """Make the tool read the given store."""
    monkeypatch.setattr(job_output, "_store", lambda context: store)


def _call(arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the registered job_output handler."""
    return asyncio.run(JOB_OUTPUT_TOOLS[0].handler(arguments, _context()))


def test_the_job_output_spec_declares_its_name_and_mode() -> None:
    spec = JOB_OUTPUT_TOOLS[0]

    assert spec.name == "job_output"
    assert spec.mode == "read"
    assert spec.arguments_model is JobOutputArguments
    parameters = spec.definition["function"]["parameters"]
    assert parameters["required"] == ["job_id"]
    assert set(parameters["properties"]) == {"job_id", "max_bytes", "wait_seconds"}


def test_the_job_output_arguments_refuse_values_outside_their_bounds() -> None:
    with pytest.raises(ValidationError):
        JobOutputArguments(job_id="a", unexpected="value")
    with pytest.raises(ValidationError):
        JobOutputArguments(job_id="a", max_bytes=1_048_577)
    with pytest.raises(ValidationError):
        JobOutputArguments(job_id="a", wait_seconds=121)


def test_reading_a_finished_job_returns_what_it_produced(monkeypatch) -> None:
    job_id = uuid.uuid4()
    records = _records(
        job_id,
        steps=(
            _step(1, "model_message", {"text": "Working on it."}),
            _step(2, "tool_result", {"output": "file contents"}, tool_name="fs_read"),
        ),
        message_content="The answer is 42.",
    )
    _install(
        monkeypatch,
        _FakeStore(CONVERSATION_ID, {str(job_id): _FakeJob(CONVERSATION_ID, records)}),
    )

    result = _call({"job_id": str(job_id)})

    assert result == {
        "status": "ok",
        "job_id": str(job_id),
        "output": "Working on it.\n\n[fs_read] file contents\n\nThe answer is 42.",
        "state": "done",
        "truncated": False,
        "dropped_bytes": 0,
    }


def test_a_job_of_another_conversation_is_refused(monkeypatch) -> None:
    job_id = uuid.uuid4()
    records = _records(job_id, message_content="The answer is 42.")
    _install(
        monkeypatch,
        _FakeStore(CONVERSATION_ID, {str(job_id): _FakeJob(uuid.uuid4(), records)}),
    )

    result = _call({"job_id": str(job_id)})

    assert result == {"status": "error", "message": job_output.NO_JOB_MESSAGE}


def test_an_unknown_job_is_refused_like_a_foreign_one(monkeypatch) -> None:
    _install(monkeypatch, _FakeStore(CONVERSATION_ID, {}))

    result = _call({"job_id": str(uuid.uuid4())})

    assert result == {"status": "error", "message": job_output.NO_JOB_MESSAGE}


def test_an_id_that_is_not_an_identifier_is_refused(monkeypatch) -> None:
    _install(monkeypatch, _FakeStore(CONVERSATION_ID, {}))

    result = _call({"job_id": "not-a-uuid"})

    assert result == {"status": "error", "message": "The job id is not a valid identifier."}


def test_a_queued_job_has_produced_nothing_yet(monkeypatch) -> None:
    job_id = uuid.uuid4()
    records = _records(job_id, turn_status="queued", run_status=None)
    _install(
        monkeypatch,
        _FakeStore(CONVERSATION_ID, {str(job_id): _FakeJob(CONVERSATION_ID, records)}),
    )

    result = _call({"job_id": str(job_id)})

    assert result == {
        "status": "ok",
        "job_id": str(job_id),
        "output": "",
        "state": "queued",
        "truncated": False,
        "dropped_bytes": 0,
    }


def test_a_failed_job_reports_the_reason_it_failed(monkeypatch) -> None:
    job_id = uuid.uuid4()
    records = _records(job_id, run_status="failed", run_error="The provider refused the request.")
    _install(
        monkeypatch,
        _FakeStore(CONVERSATION_ID, {str(job_id): _FakeJob(CONVERSATION_ID, records)}),
    )

    result = _call({"job_id": str(job_id)})

    assert result == {
        "status": "ok",
        "job_id": str(job_id),
        "output": "The job failed: The provider refused the request.",
        "state": "failed",
        "truncated": False,
        "dropped_bytes": 0,
    }


def test_the_output_is_capped_and_reports_the_dropped_bytes(monkeypatch) -> None:
    job_id = uuid.uuid4()
    records = _records(job_id, message_content="0123456789abcdef")
    _install(
        monkeypatch,
        _FakeStore(CONVERSATION_ID, {str(job_id): _FakeJob(CONVERSATION_ID, records)}),
    )

    result = _call({"job_id": str(job_id), "max_bytes": 10})

    assert result["output"] == "0123456789"
    assert result["truncated"] is True
    assert result["dropped_bytes"] == 6


def test_a_read_waits_for_a_running_job_to_finish(monkeypatch) -> None:
    job_id = uuid.uuid4()
    running = _records(
        job_id,
        turn_status="running",
        run_status="running",
        steps=(_step(1, "model_message", {"text": "Still working."}),),
    )
    finished = _records(job_id, message_content="The answer is 42.")
    store = _FakeStore(
        CONVERSATION_ID,
        {str(job_id): _FakeJob(CONVERSATION_ID, finished)},
        sequence=[running, finished],
    )
    _install(monkeypatch, store)

    result = _call({"job_id": str(job_id), "wait_seconds": 5})

    assert result["state"] == "done"
    assert result["output"] == "The answer is 42."
    assert store.load_count >= 2


def test_a_wait_that_runs_out_returns_what_the_job_has(monkeypatch) -> None:
    job_id = uuid.uuid4()
    running = _records(
        job_id,
        turn_status="running",
        run_status="running",
        steps=(_step(1, "model_message", {"text": "Still working."}),),
    )
    store = _FakeStore(
        CONVERSATION_ID,
        {str(job_id): _FakeJob(CONVERSATION_ID, running)},
        sequence=[running],
    )
    _install(monkeypatch, store)

    result = _call({"job_id": str(job_id), "wait_seconds": 1})

    assert result["state"] == "running"
    assert result["output"] == "Still working."


def test_a_read_that_cannot_reach_the_store_reports_an_error(monkeypatch) -> None:
    _install(monkeypatch, _BrokenStore())

    result = _call({"job_id": str(uuid.uuid4())})

    assert result["status"] == "error"
    assert "the database connection is gone" in str(result["message"])


def test_every_store_read_is_scoped_to_the_calling_conversation() -> None:
    session = _RecordingSession()
    store = job_output._JobStore(replace(_context(), db=session))
    turn = _turn(uuid.uuid4())
    run = DeepSpaceAgentRun(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        assistant_message_id=uuid.uuid4(),
        status="running",
        cancel_requested=False,
        step_count=0,
        last_sequence=0,
        checkpoint={},
    )

    assert store.load(run.id) is None
    store._find_turn_run(turn)
    store._steps(run)
    store._message_content(run)

    turn_read, run_read, turn_run_read, step_read, message_read = (
        str(statement.compile(dialect=postgresql.dialect())) for statement in session.statements
    )
    for sql in (turn_read, run_read, turn_run_read, step_read):
        assert "tenant_id" in sql
        assert "user_id" in sql
        assert "conversation_id" in sql
    assert "deepspace_queued_turns" in turn_read
    assert "deepspace_agent_runs" in run_read
    assert "deepspace_agent_steps" in step_read
    # The message row carries the conversation only; the tenant and the user
    # are scoped through the run that owns the message.
    assert "conversation_id" in message_read
