"""Model-facing job tools for DeepSpace."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import Field

from app.deepspace.services.jobs.brand import JobId
from app.deepspace.services.jobs.registry import JobRegistry
from app.deepspace.services.jobs.view import JobView
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    ToolContext,
    define_native_tool,
)

MAX_INLINE_OUTPUT_BYTES = 2048
DEFAULT_WAIT_TIMEOUT_MS = 30_000
MAX_WAIT_TIMEOUT_MS = 600_000


class JobOutputArguments(ToolArguments):
    job_id: str = Field(min_length=1)
    wait: bool = False
    timeout_ms: int | None = None


class JobKillArguments(ToolArguments):
    job_id: str = Field(min_length=1)
    reason: str | None = None


def validate_job_id(value: str) -> JobId:
    if not value:
        raise ValueError("job_id must be non-empty")
    return JobId(value)


def public_job(job: JobView) -> dict[str, Any]:
    """Convert JobView to model-facing dict."""
    result: dict[str, Any] = {
        "id": str(job.id),
        "kind": str(job.kind),
        "label": job.label,
        "status": job.status,
        "startedAt": job.started_at,
    }
    if job.owner:
        result["owner"] = job.owner
    if job.output_limit_bytes:
        result["outputLimitBytes"] = job.output_limit_bytes
    if job.progress:
        result["progress"] = job.progress
    if job.detail:
        result["detail"] = job.detail
    if job.finished_at:
        result["finishedAt"] = job.finished_at
    if job.output:
        result["output"] = {
            "total": job.output.total,
            "earliest": job.output.earliest,
            "spillPaths": list(job.output.spill_paths),
        }
    return result


def status_line(job: JobView) -> str:
    if job.status == "running":
        return f"running ({job.progress or '...'})"
    elif job.status == "stopping":
        return "stopping"
    elif job.status == "completed":
        return f"completed (exit: {job.detail or '0'})"
    elif job.status == "killed":
        return f"killed ({job.detail or 'cancelled'})"
    elif job.status == "failed":
        return f"failed ({job.detail or 'error'})"
    return job.status


def fit_with_suffix(content: str, suffix: str, max_bytes: int | None, omitted: str) -> str:
    if max_bytes is None:
        return content + suffix
    complete = content + suffix
    if len(complete.encode("utf-8")) <= max_bytes:
        return complete
    fixed = omitted + suffix
    if len(fixed.encode("utf-8")) >= max_bytes:
        return fixed[:max_bytes]
    available = max_bytes - len(fixed.encode("utf-8"))
    return content.encode("utf-8")[:available].decode("utf-8", errors="ignore") + fixed


def inline_completion_notice(job: JobView, output: str, max_bytes: int) -> str | None:
    head = f"background job {job.id} (kind: {job.label}) finished {status_line(job)}"
    text = f"{head}. Output:\n{output.rstrip()}" if output else f"{head}. No new output."
    if len(text.encode("utf-8")) <= max_bytes and (
        job.output_limit_bytes is None or len(text.encode("utf-8")) <= job.output_limit_bytes
    ):
        return text
    return None


def fit_completion_notice(job: JobView, max_bytes: int | None) -> str:
    prefix = f"background job {job.id}"
    detail = f" (kind: {job.label}) finished {status_line(job)}"
    action = "\nDone; job_output."
    complete = f"{prefix}{detail}. Read its output with job_output."
    if max_bytes is None or len(complete.encode("utf-8")) <= max_bytes:
        return complete
    omitted = "\n[notice truncated]"
    fixed = f"{prefix}{omitted}{action}"
    if len(fixed.encode("utf-8")) <= max_bytes:
        return fixed
    compact = f"{prefix}{action}"
    if len(compact.encode("utf-8")) <= max_bytes:
        return compact
    recovery = "background job ended; use job_list for id."
    if len(recovery.encode("utf-8")) <= max_bytes:
        return recovery
    return "job_list"


def read_body(job_read: Any) -> tuple[str, dict[str, Any]]:
    """Build model-facing read body from JobRead."""
    chunks = job_read.chunks
    delta_parts = []
    for chunk in chunks:
        delta_parts.append(chunk.text)
    delta = "".join(delta_parts)
    text = delta
    if job_read.result:
        text = f"{delta}\n{job_read.result}" if delta else job_read.result
    return text, public_job(job_read.job)


async def _job_output(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    jobs: JobRegistry = context.services.jobs
    agent = context.agent
    job_id = validate_job_id(arguments["job_id"])
    wait = arguments.get("wait", False)
    timeout_ms = arguments.get("timeout_ms")

    # Track released reads to suppress duplicate settlements
    _end_released_read: Callable[[], None] | None = None
    released_by: str | None = None

    try:
        if wait:
            if agent is None:
                raise RuntimeError("wait requires a live agent")

            # Subscribe to user input to release wait
            input_controller = asyncio.Event()

            def check_input():
                if agent.inbox.next_step.any(lambda m: m.source.kind == "user"):
                    input_controller.set()

            check_input()
            unsubscribe = agent.on(
                "agent/inbox/inserted", lambda e: check_input() if e.agent == agent else None
            )

            try:
                await jobs.wait(
                    job_id,
                    min(timeout_ms or DEFAULT_WAIT_TIMEOUT_MS, MAX_WAIT_TIMEOUT_MS),
                    agent.id if agent else None,
                    input_controller,
                )
            except asyncio.CancelledError:
                if context.signal.aborted:
                    raise
                # Released by user input
                released_by = "user steered"
            finally:
                unsubscribe()

        body_text, job_proj = read_body(
            jobs.read(job_id, context.agent.id if context.agent else None)
        )

        # Withdraw pending notice if any
        notice_key = f"job_notice_{job_id}"
        if hasattr(context, "pending_notices") and notice_key in context.pending_notices:
            notice = context.pending_notices.pop(notice_key)
            body_text = f"{notice.get('held', '')}{body_text}"

        if released_by and job_proj["status"] in ("running", "stopping"):
            job_proj["detail"] = f"{job_proj.get('detail', '')}; {released_by}".strip("; ")

        return {"text": body_text, "job": job_proj}

    finally:
        pass  # end_released_read would be called here if we tracked it


async def _job_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    jobs: JobRegistry = context.services.jobs
    agent = context.agent
    job_views = jobs.list(agent.id if agent else None)
    return [public_job(j) for j in job_views]


async def _job_kill(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    jobs: JobRegistry = context.services.jobs
    agent = context.agent
    job_id = validate_job_id(arguments["job_id"])
    reason = arguments.get("reason")

    result = jobs.kill(job_id, agent.id if agent else None, reason)
    if result == "requested":
        # Mark that model's own kill is its delivery
        if not hasattr(context, "killed_by_model"):
            context.killed_by_model = set()
        context.killed_by_model.add(job_id)

    job = public_job(jobs.get(job_id, agent.id if agent else None))
    return {
        "outcome": "cancellation-requested" if result == "requested" else "already-finished",
        "job": job,
    }


JOB_OUTPUT_TOOL = define_native_tool(
    name="job_output",
    description=(
        "Read a background job: output since the previous read for stream jobs, "
        "or the result of a finished final-output job. Set wait=true to block "
        "until the job finishes or timeout expires."
    ),
    properties={
        "job_id": {
            "type": "string",
            "description": "Job id returned by the tool that started the background work.",
        },
        "wait": {
            "type": "boolean",
            "description": "Block until the job finishes or the timeout expires. Defaults to false.",
        },
        "timeout_ms": {
            "type": "number",
            "description": "Max wait in milliseconds with wait: true.",
        },
    },
    required=["job_id"],
    arguments_model=JobOutputArguments,
    mode="read",
    handler=_job_output,
)


JOB_LIST_TOOL = define_native_tool(
    name="job_list",
    description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
    properties={},
    required=[],
    arguments_model=ToolArguments,
    mode="read",
    handler=_job_list,
)


JOB_KILL_TOOL = define_native_tool(
    name="job_kill",
    description="Request cancellation of a running background job.",
    properties={
        "job_id": {
            "type": "string",
            "description": "Job id returned by the tool that started the background work.",
        },
        "reason": {
            "type": "string",
            "description": "Optional short reason, recorded in the log and forwarded to the job.",
        },
    },
    required=["job_id"],
    arguments_model=JobKillArguments,
    mode="interactive",
    handler=_job_kill,
)


JOBS_TOOLS = [JOB_OUTPUT_TOOL, JOB_LIST_TOOL, JOB_KILL_TOOL]

__all__ = ["JOBS_TOOLS", "JOB_OUTPUT_TOOL", "JOB_LIST_TOOL", "JOB_KILL_TOOL"]
