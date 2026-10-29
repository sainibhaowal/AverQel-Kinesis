"""Background work tools for DeepSpace.

DeepSpace runs a conversation turn as a durable job.  These tools let an
agent see the work that is queued for its conversation and take it back when
it is no longer wanted.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field
from sqlalchemy import select

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

#: The largest number of jobs one listing may report.
MAX_LISTED_JOBS = 50


class JobListArguments(ToolArguments):
    """List the background work of the current conversation."""

    status: str | None = Field(default=None, max_length=32)
    limit: int | None = Field(default=None, ge=1, le=MAX_LISTED_JOBS)


class JobStatusArguments(ToolArguments):
    """Read one background job."""

    job_id: str = Field(min_length=1, max_length=64)


class JobKillArguments(ToolArguments):
    """Stop one background job."""

    job_id: str = Field(min_length=1, max_length=64)


def _load_job(context: ToolContext, job_id: str) -> DeepSpaceQueuedTurn:
    """Load one queued turn of the calling conversation.

    Args:
        context: The calling request's tool context.
        job_id: The identity the model supplied.

    Returns:
        The queued turn.

    Raises:
        ValueError: When the identity is unknown to this conversation.
    """
    try:
        parsed = uuid.UUID(str(job_id).strip())
    except ValueError as exc:
        raise ValueError("The job id is not a valid identifier.") from exc
    job = (
        require_db(context)
        .execute(
            select(DeepSpaceQueuedTurn).where(
                DeepSpaceQueuedTurn.id == parsed,
                DeepSpaceQueuedTurn.tenant_id == context.auth.tenant_id,
                DeepSpaceQueuedTurn.user_id == context.auth.user_id,
                DeepSpaceQueuedTurn.conversation_id == context.conversation_id,
            )
        )
        .scalar_one_or_none()
    )
    if job is None:
        raise ValueError("No job with that id belongs to this conversation.")
    return job


def _serialize(job: DeepSpaceQueuedTurn) -> dict[str, Any]:
    """Project one queued turn for a model-facing result."""
    return {
        "job_id": str(job.id),
        "status": job.status,
        "prompt": (job.prompt or "")[:500],
        "priority": job.priority,
        "sequence": job.sequence,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


async def _job_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the background work of the current conversation."""
    status = arguments.get("status")
    limit = min(MAX_LISTED_JOBS, int(arguments.get("limit") or 20))
    filters = [
        DeepSpaceQueuedTurn.tenant_id == context.auth.tenant_id,
        DeepSpaceQueuedTurn.user_id == context.auth.user_id,
        DeepSpaceQueuedTurn.conversation_id == context.conversation_id,
    ]
    if status:
        filters.append(DeepSpaceQueuedTurn.status == str(status))
    rows = (
        require_db(context)
        .execute(
            select(DeepSpaceQueuedTurn)
            .where(*filters)
            .order_by(DeepSpaceQueuedTurn.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    jobs = [_serialize(job) for job in rows]
    return {"status": "ok", "job_count": len(jobs), "jobs": jobs}


async def _job_status(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one background job."""
    job = _load_job(context, str(arguments["job_id"]))
    return {"status": "ok", "job": _serialize(job)}


async def _job_kill(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Stop one background job."""
    job = _load_job(context, str(arguments["job_id"]))
    if job.status in {"completed", "failed", "cancelled"}:
        return {
            "status": "noop",
            "job_id": str(job.id),
            "message": f"The job already ended with status '{job.status}'.",
        }
    job.status = "cancelled"
    job.error = "Cancelled by an agent request."
    require_db(context).commit()
    return {"status": "ok", "job_id": str(job.id), "message": "The job was cancelled."}


JOBS_DESCRIPTION = (
    "Background work tools. Use job_list to see the queued work of this "
    "conversation, job_status to read one job, and job_kill to stop one that "
    "is no longer wanted."
)

JOBS_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="job_list",
        description=JOBS_DESCRIPTION,
        properties={
            "status": string_property(
                "Only report jobs with this status, such as 'queued' or 'running'.",
                max_length=32,
            ),
            "limit": integer_property(
                "Largest number of jobs to report.", minimum=1, maximum=MAX_LISTED_JOBS
            ),
        },
        arguments_model=JobListArguments,
        mode="read",
        handler=_job_list,
    ),
    define_native_tool(
        name="job_status",
        description="Read the status and detail of one background job.",
        properties={
            "job_id": string_property("The id of the job to read.", min_length=1, max_length=64)
        },
        required=["job_id"],
        arguments_model=JobStatusArguments,
        mode="read",
        handler=_job_status,
    ),
    define_native_tool(
        name="job_kill",
        description="Cancel one queued or running background job.",
        properties={
            "job_id": string_property("The id of the job to cancel.", min_length=1, max_length=64)
        },
        required=["job_id"],
        arguments_model=JobKillArguments,
        mode="write",
        handler=_job_kill,
    ),
]

__all__ = ["JOBS_TOOLS", "JobKillArguments", "JobListArguments", "JobStatusArguments"]
