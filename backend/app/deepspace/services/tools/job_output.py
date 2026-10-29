"""Read the output of one background job for DeepSpace.

A background job is one queued conversation turn.  ``job_status`` reports when
that turn is running and when it ended; this tool reports what the turn
produced: the text the model wrote and the results its tools returned, read
from the durable run and step trail of the conversation.

The job is looked up among the turns and the runs the calling conversation
owns.  A job of another conversation is refused exactly like a job that does
not exist, so an id cannot be used to read another tenant's work.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.agent_runtime import DeepSpaceAgentRun, DeepSpaceAgentStep
from app.deepspace.models.message import Message
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)

#: The number of output bytes one read returns when the caller names no limit.
MAX_JOB_OUTPUT_BYTES = 65_536

#: The largest number of output bytes one read may return.
MAX_JOB_OUTPUT_BYTES_CEILING = 1_048_576

#: The largest number of seconds one read may wait for the job to finish.
MAX_WAIT_SECONDS = 120

#: The pause between two polls while a read waits for the job to finish.
POLL_INTERVAL_SECONDS = 0.5

#: The states a job can end in.  A job in one of them produces no more output.
TERMINAL_STATES = frozenset({"done", "failed"})

#: The refusal a caller gets for a job its conversation does not own.
NO_JOB_MESSAGE = "No job with that id belongs to this conversation."

#: How a queued turn's status maps to the state a caller reads.
_TURN_STATES = {
    "queued": "queued",
    "running": "running",
    "cancelling": "running",
    "awaiting_user": "running",
    "awaiting_approval": "running",
    "completed": "done",
    "failed": "failed",
    "cancelled": "failed",
}

#: How a run's status maps to the state a caller reads.
_RUN_STATES = {
    "running": "running",
    "cancelling": "running",
    "awaiting_user": "running",
    "awaiting_approval": "running",
    "completed": "done",
    "failed": "failed",
    "blocked": "failed",
    "cancelled": "failed",
}


class JobOutputArguments(ToolArguments):
    """Read the output of one background job."""

    job_id: str = Field(min_length=1, max_length=64)
    max_bytes: int | None = Field(default=None, ge=1, le=MAX_JOB_OUTPUT_BYTES_CEILING)
    wait_seconds: int | None = Field(default=None, ge=0, le=MAX_WAIT_SECONDS)


@dataclass(frozen=True, slots=True)
class _JobRecords:
    """The durable records of one background job of the calling conversation."""

    #: The id the caller named.
    job_id: str
    #: The queued turn, when the caller named a turn.
    turn: DeepSpaceQueuedTurn | None
    #: The run that produced the output, when the turn has started.
    run: DeepSpaceAgentRun | None
    #: The run's recorded steps, in the order they happened.
    steps: tuple[DeepSpaceAgentStep, ...]
    #: The answer the run wrote, when it wrote one.
    message_content: str


class _JobStore:
    """Reads the durable records of the calling conversation's background jobs.

    Every read is scoped to the tenant, the user, and the conversation of the
    calling request, so a job that belongs to somebody else is not found.
    """

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self.db: Session = require_db(context)

    def load(self, job_id: uuid.UUID) -> _JobRecords | None:
        """Read one job of the calling conversation.

        Args:
            job_id: The identity the caller named.

        Returns:
            The job's records, or None when the conversation owns no such job.
        """
        turn = self._find_turn(job_id)
        if turn is not None:
            run = self._find_turn_run(turn)
            return _JobRecords(
                job_id=str(turn.id),
                turn=turn,
                run=run,
                steps=self._steps(run),
                message_content=self._message_content(run),
            )
        run = self._find_run(job_id)
        if run is None:
            return None
        return _JobRecords(
            job_id=str(run.id),
            turn=None,
            run=run,
            steps=self._steps(run),
            message_content=self._message_content(run),
        )

    def _find_turn(self, job_id: uuid.UUID) -> DeepSpaceQueuedTurn | None:
        """Read the queued turn with this id, when the conversation owns it."""
        return self.db.execute(
            select(DeepSpaceQueuedTurn)
            .where(
                DeepSpaceQueuedTurn.id == job_id,
                DeepSpaceQueuedTurn.tenant_id == self.context.auth.tenant_id,
                DeepSpaceQueuedTurn.user_id == self.context.auth.user_id,
                DeepSpaceQueuedTurn.conversation_id == self.context.conversation_id,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _find_run(self, job_id: uuid.UUID) -> DeepSpaceAgentRun | None:
        """Read the run with this id, when the conversation owns it."""
        return self.db.execute(
            select(DeepSpaceAgentRun)
            .where(
                DeepSpaceAgentRun.id == job_id,
                DeepSpaceAgentRun.tenant_id == self.context.auth.tenant_id,
                DeepSpaceAgentRun.user_id == self.context.auth.user_id,
                DeepSpaceAgentRun.conversation_id == self.context.conversation_id,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _find_turn_run(self, turn: DeepSpaceQueuedTurn) -> DeepSpaceAgentRun | None:
        """Read the run that produced the answer of one queued turn.

        The run and the turn are linked through the assistant message, which
        carries the request id of the turn it answered.

        Args:
            turn: The queued turn.

        Returns:
            The run, when the turn has started one.
        """
        return self.db.execute(
            select(DeepSpaceAgentRun)
            .join(Message, Message.id == DeepSpaceAgentRun.assistant_message_id)
            .where(
                DeepSpaceAgentRun.tenant_id == self.context.auth.tenant_id,
                DeepSpaceAgentRun.user_id == self.context.auth.user_id,
                DeepSpaceAgentRun.conversation_id == self.context.conversation_id,
                Message.conversation_id == self.context.conversation_id,
                Message.metadata_json["client_request_id"].astext == turn.client_request_id,
            )
            .order_by(DeepSpaceAgentRun.updated_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _steps(self, run: DeepSpaceAgentRun | None) -> tuple[DeepSpaceAgentStep, ...]:
        """Read the recorded steps of one run, in the order they happened."""
        if run is None:
            return ()
        rows = (
            self.db.execute(
                select(DeepSpaceAgentStep)
                .where(
                    DeepSpaceAgentStep.run_id == run.id,
                    DeepSpaceAgentStep.tenant_id == self.context.auth.tenant_id,
                    DeepSpaceAgentStep.user_id == self.context.auth.user_id,
                    DeepSpaceAgentStep.conversation_id == self.context.conversation_id,
                )
                .order_by(DeepSpaceAgentStep.sequence.asc())
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        return tuple(rows)

    def _message_content(self, run: DeepSpaceAgentRun | None) -> str:
        """Read the answer one run wrote, when it wrote one."""
        if run is None or run.assistant_message_id is None:
            return ""
        message = self.db.execute(
            select(Message)
            .where(
                Message.id == run.assistant_message_id,
                Message.conversation_id == self.context.conversation_id,
                Message.role == "assistant",
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if message is None:
            return ""
        return str(message.content or "")


def _store(context: ToolContext) -> _JobStore:
    """Return the reader for the calling conversation's background jobs.

    Args:
        context: The calling request's tool context.

    Returns:
        The reader.
    """
    return _JobStore(context)


def _job_state(records: _JobRecords) -> str:
    """Report the state of one job.

    The run is the authority once the turn has started, because the turn keeps
    its own status while it waits for the user.

    Args:
        records: The job's records.

    Returns:
        The state: queued, running, done, or failed.
    """
    if records.run is not None:
        return _RUN_STATES.get(str(records.run.status or ""), "running")
    if records.turn is not None:
        return _TURN_STATES.get(str(records.turn.status or ""), "running")
    return "queued"


def _step_output(step: DeepSpaceAgentStep) -> str | None:
    """Return the text one recorded step adds to the job's output.

    Args:
        step: One recorded step.

    Returns:
        The step's text, or None when the step carries no output.
    """
    payload = step.result_json if isinstance(step.result_json, dict) else {}
    if step.step_type == "model_message":
        text = str(payload.get("text") or "")
        return text or None
    if step.step_type in {"tool_result", "mcp_tool_result"}:
        output = str(payload.get("output") or "")
        if not output:
            return None
        return f"[{step.tool_name or 'tool'}] {output}"
    return None


def _job_output_text(records: _JobRecords) -> str:
    """Assemble the text one job has produced so far.

    The text is the model's narration, then the results its tools returned,
    then the answer it wrote.

    Args:
        records: The job's records.

    Returns:
        The produced text.
    """
    parts = [text for text in (_step_output(step) for step in records.steps) if text]
    if records.message_content.strip():
        parts.append(records.message_content)
    return "\n\n".join(parts)


def _job_error(records: _JobRecords) -> str | None:
    """Return the failure reason the job recorded, when it recorded one.

    Args:
        records: The job's records.

    Returns:
        The failure reason, or None when the job recorded no failure.
    """
    if records.run is not None and records.run.last_error:
        return str(records.run.last_error)
    if records.turn is not None and records.turn.error:
        return str(records.turn.error)
    return None


def _cap_output(text: str, max_bytes: int) -> tuple[str, int, bool]:
    """Cap the output at the caller's byte budget.

    Args:
        text: The produced text.
        max_bytes: The largest number of bytes to keep.

    Returns:
        The kept text, the number of dropped bytes, and whether anything was
        dropped.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, 0, False
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return kept, len(encoded) - len(kept.encode("utf-8")), True


async def _wait_for_job(
    store: _JobStore,
    job_id: uuid.UUID,
    records: _JobRecords,
    wait_seconds: int,
) -> _JobRecords:
    """Wait for one job to finish, then return its latest records.

    Args:
        store: The reader for the calling conversation's background jobs.
        job_id: The identity the caller named.
        records: The records read so far.
        wait_seconds: How long to wait for the job to end.

    Returns:
        The records read when the job ended, or the last records read before
        the wait ran out.
    """
    deadline = time.monotonic() + wait_seconds
    while wait_seconds > 0 and _job_state(records) not in TERMINAL_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        refreshed = store.load(job_id)
        if refreshed is None:
            break
        records = refreshed
    return records


async def _job_output(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the output one background job of this conversation produced."""
    try:
        parsed = uuid.UUID(str(arguments.get("job_id") or "").strip())
    except ValueError:
        return {"status": "error", "message": "The job id is not a valid identifier."}
    try:
        max_bytes = min(
            MAX_JOB_OUTPUT_BYTES_CEILING,
            max(1, int(arguments.get("max_bytes") or MAX_JOB_OUTPUT_BYTES)),
        )
        wait_seconds = min(MAX_WAIT_SECONDS, max(0, int(arguments.get("wait_seconds") or 0)))
        store = _store(context)
        records = store.load(parsed)
        if records is None:
            return {"status": "error", "message": NO_JOB_MESSAGE}
        records = await _wait_for_job(store, parsed, records, wait_seconds)
    except Exception as exc:  # noqa: BLE001 - a tool call must not raise
        return {"status": "error", "message": f"The job output could not be read: {exc}"}
    text = _job_output_text(records)
    error = _job_error(records)
    if error:
        text = f"The job failed: {error}\n\n{text}" if text else f"The job failed: {error}"
    capped, dropped, truncated = _cap_output(text, max_bytes)
    return {
        "status": "ok",
        "job_id": records.job_id,
        "output": capped,
        "state": _job_state(records),
        "truncated": truncated,
        "dropped_bytes": dropped,
    }


JOB_OUTPUT_DESCRIPTION = (
    "Read the output a background job produced. Use job_list to find the id of "
    "a job and job_output to read the text it wrote and the results its tools "
    "returned. Set wait_seconds to wait for a running job to finish."
)

JOB_OUTPUT_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="job_output",
        description=JOB_OUTPUT_DESCRIPTION,
        properties={
            "job_id": string_property("The id of the job to read.", min_length=1, max_length=64),
            "max_bytes": integer_property(
                "The largest number of output bytes to return.",
                minimum=1,
                maximum=MAX_JOB_OUTPUT_BYTES_CEILING,
            ),
            "wait_seconds": integer_property(
                "How long to wait for a running job to finish before returning "
                "what it has produced.",
                minimum=0,
                maximum=MAX_WAIT_SECONDS,
            ),
        },
        required=["job_id"],
        arguments_model=JobOutputArguments,
        mode="read",
        handler=_job_output,
    ),
]

__all__ = ["JOB_OUTPUT_TOOLS", "JobOutputArguments"]
