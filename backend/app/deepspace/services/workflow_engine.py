"""Typed, durable workflow execution seam for DeepSpace."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from app.deepspace.models.workflow_job import DeepSpaceWorkflowJob
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workflow_jobs import (
    WORKFLOW_AGENT_START,
    WORKFLOW_END,
    WORKFLOW_START,
    WorkflowJobStore,
)
from app.deepspace.services.workspace_root import workspace_root_for

logger = logging.getLogger(__name__)


class WorkflowPhase(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStopReason(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    AGENT_CAP = "agent_cap"
    ITEM_CAP = "item_cap"


class WorkflowCancelled(Exception):  # noqa: N818 - compatibility name is part of the engine API
    """Raised inside the engine when a caller or durable job requests stop."""


@dataclass(frozen=True, slots=True)
class WorkflowMeta:
    name: str
    version: str = "1"
    description: str = ""
    max_total_agents: int = 100
    max_concurrent_agents: int = 4
    subagent_provider: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStartRequest:
    script: str
    meta: WorkflowMeta
    args: Any = None
    subagent_provider: str | None = None
    max_total_agents: int | None = None
    run_in_background: bool = False
    mode: Literal["script", "named"] = "script"
    workflow_name: str | None = None
    input_text: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowAgentInfo:
    seq: int
    label: str
    phase: str | None
    child_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WorkflowAgentEndInfo:
    seq: int
    outcome: Literal["ok", "error", "cancelled"]
    answer: str | None
    tools_used: list[str]


@dataclass(frozen=True, slots=True)
class WorkflowRunInfo:
    id: UUID
    meta: WorkflowMeta


@dataclass(frozen=True, slots=True)
class WorkflowResultInfo:
    stop_reason: WorkflowStopReason
    error: str | None
    agent_count: int


@dataclass(slots=True)
class WorkflowRun:
    """A live workflow handle with explicit cancellation and disposal."""

    id: UUID
    script: str
    meta: WorkflowMeta
    args: Any
    subagent_provider: str
    max_total_agents: int
    max_concurrent_agents: int
    status: WorkflowPhase = WorkflowPhase.PENDING
    result: Any = None
    error: str | None = None
    agents_spawned: int = 0
    current_concurrent: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    completion: asyncio.Future[Any] | None = field(default=None, repr=False)
    _task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _engine: WorkflowEngine | None = field(default=None, repr=False)
    _job: DeepSpaceWorkflowJob | None = field(default=None, repr=False)
    _disposed: bool = field(default=False, repr=False)

    async def wait(self) -> Any:
        """Wait for this run without polling its mutable status."""
        if self.completion is None:
            return self.result
        return await self.completion

    async def cancel(self, reason: str = "cancelled") -> None:
        """Request cancellation and stop a local foreground task promptly."""
        if self._engine is not None:
            await self._engine.cancel(self, reason=reason)

    async def dispose(self) -> None:
        """Release the run's local task/runtime ownership exactly once."""
        if self._disposed:
            return
        self._disposed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.completion is not None and not self.completion.done():
            self.completion.cancel()


WorkflowEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class WorkflowEngine:
    """The single typed seam used by workflow tools and workers."""

    def __init__(self, settings: Any, context: ToolContext | None = None):
        self.settings = settings
        self._context = context
        self._store = WorkflowJobStore(context.db) if context and context.db is not None else None

    def _get_native_executor(self) -> Any:
        return getattr(self._context, "native_executor", None) if self._context else None

    def _get_workspace_root(self) -> Path:
        if self._context and self._context.workspace_root:
            return self._context.workspace_root
        if self._context and self._context.auth:
            return workspace_root_for(
                self.settings, self._context.auth.tenant_id, self._context.auth.user_id
            )
        return Path.cwd()

    def _job_for_request(self, request: WorkflowStartRequest) -> DeepSpaceWorkflowJob | None:
        if self._store is None or self._context is None:
            return None
        auth = self._context.auth
        return self._store.create(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=self._context.conversation_id,
            mode=request.mode,
            workflow_name=request.workflow_name or request.meta.name,
            script=request.script if request.mode == "script" else None,
            input_text=request.input_text,
            meta=asdict(request.meta),
            args=request.args if isinstance(request.args, dict) else {"value": request.args},
            roles=sorted(auth.roles),
            permissions=sorted(auth.permissions),
        )

    async def start(self, request: WorkflowStartRequest) -> WorkflowRun:
        """Create a run handle and start it foreground or durably in background."""
        if not request.script or not request.script.strip():
            raise ValueError("Workflow script cannot be empty")
        provider = request.subagent_provider or request.meta.subagent_provider or "spawn"
        max_total = request.max_total_agents or request.meta.max_total_agents or 100
        max_concurrent = request.meta.max_concurrent_agents or 4
        if max_total < 1 or max_total > 1000:
            raise ValueError("Workflow max_total_agents must be between 1 and 1000")
        if max_concurrent < 1 or max_concurrent > 100:
            raise ValueError("Workflow max_concurrent_agents must be between 1 and 100")

        job = self._job_for_request(request)
        if job is not None:
            self._store.append(
                job,
                WORKFLOW_START,
                {"mode": request.mode, "background": request.run_in_background},
            )

        loop = asyncio.get_running_loop()
        run = WorkflowRun(
            id=job.id if job is not None else uuid.uuid4(),
            script=request.script,
            meta=request.meta,
            args=request.args,
            subagent_provider=provider,
            max_total_agents=max_total,
            max_concurrent_agents=max_concurrent,
            completion=loop.create_future(),
            _engine=self,
            _job=job,
        )
        if request.run_in_background:
            if job is None:
                raise RuntimeError("Background workflows require a durable database context")
            self._enqueue_background(job)
            return run
        run._task = asyncio.create_task(self._execute(run))
        return run

    def _enqueue_background(self, job: DeepSpaceWorkflowJob) -> None:
        from app.deepspace.workers.workflow_tasks import run_workflow_job_task

        run_workflow_job_task.apply_async(
            kwargs={
                "job_id": str(job.id),
                "tenant_id": str(job.tenant_id),
                "user_id": str(job.user_id),
                "conversation_id": str(job.conversation_id),
            }
        )

    async def run_persisted_job(self, job: DeepSpaceWorkflowJob) -> WorkflowRun:
        """Run a durable job from a worker process."""
        raw = dict(job.meta or {})
        meta = WorkflowMeta(
            name=job.workflow_name,
            version=str(raw.get("version") or "1"),
            description=str(raw.get("description") or ""),
            max_total_agents=int(raw.get("max_total_agents") or 100),
            max_concurrent_agents=int(raw.get("max_concurrent_agents") or 4),
            subagent_provider=raw.get("subagent_provider"),
        )
        run = WorkflowRun(
            id=job.id,
            script=job.script or "",
            meta=meta,
            args=job.args or {},
            subagent_provider=meta.subagent_provider or "spawn",
            max_total_agents=meta.max_total_agents,
            max_concurrent_agents=meta.max_concurrent_agents,
            completion=asyncio.get_running_loop().create_future(),
            _engine=self,
            _job=job,
        )
        run._task = asyncio.create_task(self._execute(run))
        await run.wait()
        return run

    async def cancel(self, run: WorkflowRun, *, reason: str = "cancelled") -> None:
        if run._job is not None and self._store is not None:
            self._store.cancel(run._job, reason=reason)
        if run._task is not None and not run._task.done():
            run._task.cancel()

    def _cancelled(self, run: WorkflowRun) -> bool:
        if run._job is not None and self._store is not None:
            self._store.db.expire(run._job, ["cancel_requested", "status"])
            self._store.db.refresh(run._job, attribute_names=["cancel_requested", "status"])
            if run._job.cancel_requested:
                return True
        return bool(self._context and self._context.cancel_check and self._context.cancel_check())

    async def _event(self, run: WorkflowRun, event_type: str, payload: dict[str, Any]) -> None:
        if self._cancelled(run) and event_type not in {WORKFLOW_END}:
            raise WorkflowCancelled("The workflow was cancelled.")
        if event_type == WORKFLOW_AGENT_START:
            run.agents_spawned += 1
            if run.agents_spawned > run.max_total_agents:
                raise WorkflowCancelled("The workflow reached its agent cap.")
        if self._store is not None and run._job is not None:
            run._job.agents_spawned = run.agents_spawned
            self._store.append(run._job, event_type, payload)

    async def _execute(self, run: WorkflowRun) -> None:
        run.status = WorkflowPhase.RUNNING
        if self._store is not None and run._job is not None:
            self._store.set_state(run._job, status="running")
        try:
            if self._cancelled(run):
                raise WorkflowCancelled("The workflow was cancelled before it started.")
            if run._job is not None and run._job.mode == "named":
                from dataclasses import replace

                from app.deepspace.services.tools.workflow import run_named_workflow

                if self._context is None:
                    raise RuntimeError("Named workflow has no tool context")
                named_context = replace(
                    self._context,
                    cancel_check=lambda: self._cancelled(run),
                )
                result = await run_named_workflow(
                    {"name": run._job.workflow_name, "input": run._job.input_text or ""},
                    named_context,
                    progress=lambda event_type, payload: self._event(run, event_type, payload),
                )
            else:
                result = await self._run_workflow_script(run)
            if self._cancelled(run):
                raise WorkflowCancelled("The workflow was cancelled.")
            run.result = result
            run.status = WorkflowPhase.COMPLETED
            run.completed_at = datetime.now(UTC)
            if self._store is not None and run._job is not None:
                self._store.set_state(
                    run._job, status="completed", stop_reason="completed", result=result
                )
            await self._event(run, WORKFLOW_END, {"status": "completed", "result": result})
            self._finish(run, result)
        except asyncio.CancelledError:
            run.status = WorkflowPhase.CANCELLED
            run.error = "The workflow was cancelled."
            run.completed_at = datetime.now(UTC)
            if self._store is not None and run._job is not None:
                self._store.set_state(
                    run._job, status="cancelled", stop_reason="cancelled", error=run.error
                )
            with contextlib.suppress(Exception):
                await self._event(run, WORKFLOW_END, {"status": "cancelled", "error": run.error})
            self._finish(run, {"status": "cancelled", "error": run.error})
        except WorkflowCancelled as exc:
            run.status = WorkflowPhase.CANCELLED
            run.error = str(exc)
            run.completed_at = datetime.now(UTC)
            reason = "agent_cap" if "agent cap" in str(exc).lower() else "cancelled"
            if self._store is not None and run._job is not None:
                self._store.set_state(
                    run._job, status="cancelled", stop_reason=reason, error=run.error
                )
            with contextlib.suppress(Exception):
                await self._event(run, WORKFLOW_END, {"status": "cancelled", "error": run.error})
            self._finish(run, {"status": "cancelled", "error": run.error})
        except Exception as exc:  # noqa: BLE001 - the durable run reports the failure
            logger.warning("DeepSpace workflow failed", exc_info=True)
            run.status = WorkflowPhase.FAILED
            run.error = str(exc)[:20_000] or exc.__class__.__name__
            run.completed_at = datetime.now(UTC)
            if self._store is not None and run._job is not None:
                self._store.set_state(
                    run._job, status="failed", stop_reason="error", error=run.error
                )
            with contextlib.suppress(Exception):
                await self._event(run, WORKFLOW_END, {"status": "failed", "error": run.error})
            self._finish(run, {"status": "failed", "error": run.error})

    def _finish(self, run: WorkflowRun, value: Any) -> None:
        if run.completion is not None and not run.completion.done():
            run.completion.set_result(value)

    def _build_ptc_program(self, run: WorkflowRun) -> str:
        meta = json.dumps(asdict(run.meta), ensure_ascii=False)
        args = json.dumps(run.args, ensure_ascii=False, default=str)
        return f"""
const __workflowEvent = (type, payload = {{}}) =>
  console.log('__PTC_EVENT__' + JSON.stringify({{ type, ...payload }}));
const workflow = {{
  id: {json.dumps(str(run.id))},
  meta: {meta},
  args: {args},
  agents: [],
  currentPhase: null,
  async agent(task, options = {{}}) {{
    if (this.agents.length >= this.meta.max_total_agents) throw new Error('Workflow agent cap reached');
    const label = options.label || `agent-${{this.agents.length + 1}}`;
    const phase = options.phase || this.currentPhase;
    const seq = this.agents.length + 1;
    __workflowEvent('workflow/agent-start', {{ seq, label, phase }});
    try {{
      const result = await tool('subagent', {{
        description: label, task, mode: options.mode || 'one-shot', persona: options.persona,
        model: options.model, tool_allow: options.tool_allow, tool_deny: options.tool_deny,
      }});
      this.agents.push({{ seq, label, phase, child_id: result && result.agent_id }});
      __workflowEvent('workflow/agent-end', {{ seq, outcome: result && result.status === 'ok' ? 'ok' : 'error', answer: result && result.answer }});
      return result && result.answer;
    }} catch (error) {{
      __workflowEvent('workflow/agent-end', {{ seq, outcome: 'error', answer: String(error) }});
      throw error;
    }}
  }},
  async parallel(items, mapper, options = {{}}) {{
    if (!Array.isArray(items)) throw new Error('workflow.parallel expects an array');
    const concurrency = Math.max(1, Math.min(Number(options.concurrency || this.meta.max_concurrent_agents || 4), this.meta.max_concurrent_agents || 4));
    const results = [];
    for (let i = 0; i < items.length; i += concurrency) {{
      results.push(...await Promise.all(items.slice(i, i + concurrency).map(item => mapper(item))));
    }}
    return results;
  }},
  async pipeline(stages, options = {{}}) {{
    let data = options.initial;
    for (const stage of stages) data = await stage(data);
    return data;
  }},
  phase(title) {{ this.currentPhase = String(title || ''); __workflowEvent('workflow/phase', {{ title: this.currentPhase }}); }},
  log(message) {{ __workflowEvent('workflow/log', {{ message: String(message ?? '') }}); }},
  returnValue(value) {{ throw {{ __workflow_return: true, value }}; }},
}};
let __workflowUserResult;
try {{
  __workflowUserResult = await (async () => {{
{run.script}
  }})();
}} catch (error) {{
  if (!error || error.__workflow_return !== true) throw error;
  __workflowUserResult = error.value;
}}
return __workflowUserResult;
"""

    async def _run_workflow_script(self, run: WorkflowRun) -> dict[str, Any]:
        executor = self._get_native_executor()
        if executor is None:
            raise RuntimeError("No tool executor available for workflow")
        from app.deepspace.services.ptc_runtime_node import create_node_ptc_runtime

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            await self._event(run, event_type, payload)

        async def call_tool(arguments: dict[str, Any]) -> Any:
            if self._cancelled(run):
                raise WorkflowCancelled("The workflow was cancelled.")
            return await executor("subagent", arguments)

        runtime = create_node_ptc_runtime(self.settings)
        spec = runtime.resolve(
            self._build_ptc_program(run),
            {"subagent": call_tool},
            str(self._get_workspace_root()),
            None,
            None,
            max_tool_calls=int(getattr(self.settings, "deepspace_ptc_max_tool_calls", 24)),
            on_event=emit,
        )
        try:
            outcome = await runtime.run(spec)
        finally:
            runtime.dispose()
        if outcome.status != "ok":
            raise RuntimeError(outcome.error or "Workflow execution failed")
        return {
            "status": "completed",
            "value": outcome.value,
            "output": outcome.output,
            "tool_calls": len(outcome.tool_calls),
        }


async def run_workflow(
    context: ToolContext,
    script: str,
    meta: dict[str, Any],
    args: Any = None,
    subagent_provider: str | None = None,
    *,
    run_in_background: bool = False,
) -> dict[str, Any]:
    """Run a script and return a stable foreground or background envelope."""
    workflow_meta = WorkflowMeta(
        name=str(meta.get("name") or "inline-workflow"),
        version=str(meta.get("version") or "1"),
        description=str(meta.get("description") or ""),
        max_total_agents=int(meta.get("max_total_agents") or 100),
        max_concurrent_agents=int(meta.get("max_concurrent_agents") or 4),
        subagent_provider=subagent_provider or meta.get("subagent_provider"),
    )
    engine = WorkflowEngine(context.settings, context)
    run = await engine.start(
        WorkflowStartRequest(
            script=script,
            meta=workflow_meta,
            args=args,
            subagent_provider=subagent_provider,
            run_in_background=run_in_background,
        )
    )
    if run_in_background:
        return {
            "status": "background",
            "run_id": str(run.id),
            "job_id": str(run.id),
            "workflow": workflow_meta.name,
        }
    try:
        result = await run.wait()
        return {
            "status": run.status.value,
            "run_id": str(run.id),
            "result": result,
            "error": run.error,
        }
    finally:
        await run.dispose()


__all__ = [
    "WorkflowEngine",
    "WorkflowMeta",
    "WorkflowPhase",
    "WorkflowRun",
    "WorkflowStartRequest",
    "WorkflowStopReason",
    "run_workflow",
]
