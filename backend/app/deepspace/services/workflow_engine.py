"""Workflow orchestration engine for DeepSpace.

Ported from NeoSIS @averqel/neosis-workflow and @averqel/neosis-workflow-ptc.
Executes JavaScript orchestration scripts with subagent fan-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workspace_root import workspace_root_for

logger = logging.getLogger(__name__)


class WorkflowPhase(str, Enum):
    """Workflow execution phase."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStopReason(str, Enum):
    """Reason a workflow stopped."""

    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    AGENT_CAP = "agent_cap"
    ITEM_CAP = "item_cap"


@dataclass(frozen=True, slots=True)
class WorkflowMeta:
    """Workflow metadata."""

    name: str
    version: str = "1"
    description: str = ""
    max_total_agents: int = 100
    max_concurrent_agents: int = 4
    subagent_provider: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStartRequest:
    """Request to start a workflow run."""

    script: str
    meta: WorkflowMeta
    args: Any = None
    subagent_provider: str | None = None
    max_total_agents: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowAgentInfo:
    """Information about a spawned agent."""

    seq: int
    label: str
    phase: str
    child_id: UUID


@dataclass(frozen=True, slots=True)
class WorkflowAgentEndInfo:
    """Information about a completed agent."""

    seq: int
    outcome: Literal["ok", "error", "cancelled"]
    answer: str | None
    tools_used: list[str]


@dataclass(frozen=True, slots=True)
class WorkflowRunInfo:
    """Workflow run identity snapshot."""

    id: UUID
    meta: WorkflowMeta


@dataclass(frozen=True, slots=True)
class WorkflowResultInfo:
    """Workflow result summary."""

    stop_reason: WorkflowStopReason
    error: str | None
    agent_count: int


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """A workflow run."""

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


class WorkflowEngine:
    """Workflow orchestration engine."""

    def __init__(self, settings: Any, context: ToolContext | None = None):
        self.settings = settings
        self._context = context

    def _get_native_executor(self) -> Any:
        """Get the native executor from the tool context."""
        return getattr(self._context, "native_executor", None) if self._context else None

    def _get_workspace_root(self) -> Path:
        """Get the workspace root from the tool context or resolve it."""
        if self._context and self._context.workspace_root:
            return self._context.workspace_root
        if self._context and self._context.auth:
            return workspace_root_for(
                self.settings, self._context.auth.tenant_id, self._context.auth.user_id
            )
        return Path.cwd()

    def _get_auth(self) -> Any:
        """Get the auth context from the tool context."""
        return getattr(self._context, "auth", None) if self._context else None

    async def start(self, request: WorkflowStartRequest) -> WorkflowRun:
        """Start a workflow run."""
        # Validate script
        if not request.script or not request.script.strip():
            raise ValueError("Workflow script cannot be empty")

        # Resolve provider
        provider = request.subagent_provider or request.meta.subagent_provider or "spawn"

        # Resolve caps
        max_total = request.max_total_agents or request.meta.max_total_agents or 100
        max_concurrent = request.meta.max_concurrent_agents or 4

        run = WorkflowRun(
            id=uuid.uuid4(),
            script=request.script,
            meta=request.meta,
            args=request.args,
            subagent_provider=provider,
            max_total_agents=max_total,
            max_concurrent_agents=max_concurrent,
        )

        # Execute workflow asynchronously
        asyncio.create_task(self._execute_workflow(run))

        return run

    async def _execute_workflow(self, run: WorkflowRun) -> None:
        """Execute the workflow script."""
        run.status = WorkflowPhase.RUNNING

        try:
            # Run the orchestration script using PTC
            # The script gets access to: agent(), parallel(), pipeline(), phase(), log(), return
            ptc_program = self._build_ptc_program(run)

            result = await self._run_workflow_script(run, ptc_program)

            run.result = result
            run.status = WorkflowPhase.COMPLETED
            run.completed_at = datetime.now(UTC)

        except Exception as e:
            logger.error("Workflow execution failed: %s", e, exc_info=True)
            run.status = WorkflowPhase.FAILED
            run.error = str(e)
            run.completed_at = datetime.now(UTC)

    def _build_ptc_program(self, run: WorkflowRun) -> str:
        """Build a PTC program that implements the workflow helpers."""
        return f"""
// Workflow execution context
const workflow = {{
    id: "{run.id}",
    meta: {json.dumps(run.meta.__dict__)},
    args: {json.dumps(run.args)},
    agents: [],
    currentPhase: null,

    async agent(task, options = {{}}) {{
        const label = options.label || `agent-${{this.agents.length + 1}}`;
        const phase = options.phase || this.currentPhase;
        const seq = this.agents.length + 1;

        // Emit agent-start event
        console.log(JSON.stringify({{ type: 'workflow/agent-start', seq, label, phase }}));

        try {{
            const result = await tool('subagent', {{
                description: label,
                task: task,
                mode: options.mode || 'one-shot',
                persona: options.persona,
                model: options.model,
                tool_allow: options.tool_allow,
                tool_deny: options.tool_deny,
            }});

            this.agents.push({{ seq, label, phase, child_id: result.agent_id }});

            // Emit agent-end event
            console.log(JSON.stringify({{ type: 'workflow/agent-end', seq, outcome: result.status === 'ok' ? 'ok' : 'error', answer: result.answer }}));

            return result.answer;
        }} catch (err) {{
            console.log(JSON.stringify({{ type: 'workflow/agent-end', seq, outcome: 'error', answer: String(err) }}));
            throw err;
        }}
    }},

    async parallel(items, mapper, options = {{}}) {{
        const concurrency = options.concurrency || this.meta.max_concurrent_agents || 4;
        const results = [];

        for (let i = 0; i < items.length; i += concurrency) {{
            const batch = items.slice(i, i + concurrency);
            const batchResults = await Promise.all(
                batch.map(item => mapper(item))
            );
            results.push(...batchResults);
        }}

        return results;
    }},

    async pipeline(stages, options = {{}}) {{
        let data = options.initial;
        for (const stage of stages) {{
            data = await stage(data);
        }}
        return data;
    }},

    phase(title) {{
        this.currentPhase = title;
        console.log(JSON.stringify({{ type: 'workflow/phase', title }}));
    }},

    log(message) {{
        console.log(JSON.stringify({{ type: 'workflow/log', message }}));
    }},

    returnValue(value) {{
        console.log(JSON.stringify({{ type: 'workflow/return', value }}));
        throw {{ __workflow_return: true, value }};
    }},
}};

// User script
{run.script}
"""

    async def _run_workflow_script(self, run: WorkflowRun, ptc_program: str) -> Any:
        """Run the workflow script via PTC."""
        executor = self._get_native_executor()
        if executor is None:
            return {"status": "error", "message": "No tool executor available for workflow"}

        workspace_root = self._get_workspace_root()
        auth = self._get_auth()

        # Determine allowed tools for the workflow program
        # Only subagent and read-only tools should be available
        allowed_tools = {
            "subagent",
            "read_file",
            "write_file",
            "list_files",
            "web_search",
            "url_read",
        }

        # Build bindings for the PTC runtime
        def binding(tool_name: str):
            async def call(arguments: dict[str, Any]) -> Any:
                return await executor(tool_name, arguments)

            return call

        bindings = {name: binding(name) for name in allowed_tools}

        from app.deepspace.services.ptc_runtime_node import create_node_ptc_runtime

        node_runtime = create_node_ptc_runtime(self.settings)
        spec = node_runtime.resolve(
            ptc_program,
            bindings,
            str(workspace_root),
            300_000,  # 5 minute timeout
            None,
            max_tool_calls=50,
        )
        outcome = await node_runtime.run(spec)

        if outcome.status == "ok":
            return {"status": "completed", "value": outcome.value}
        else:
            return {"status": "error", "message": outcome.error or "Workflow execution failed"}


async def run_workflow(
    context: ToolContext,
    script: str,
    meta: dict,
    args: Any = None,
    subagent_provider: str | None = None,
) -> dict[str, Any]:
    """Run a workflow script."""
    # This is the main entry point called from the workflow tool

    engine = WorkflowEngine(context.settings, context)

    workflow_meta = WorkflowMeta(
        name=meta.get("name", "workflow"),
        version=meta.get("version", "1"),
        description=meta.get("description", ""),
        max_total_agents=meta.get("max_total_agents", 100),
        max_concurrent_agents=meta.get("max_concurrent_agents", 4),
        subagent_provider=meta.get("subagent_provider"),
    )

    request = WorkflowStartRequest(
        script=script,
        meta=workflow_meta,
        args=args,
        subagent_provider=subagent_provider,
    )

    run = await engine.start(request)

    # Wait for completion (with timeout)
    timeout = 3600  # 1 hour max
    start = asyncio.get_event_loop().time()
    while run.status == WorkflowPhase.RUNNING:
        if asyncio.get_event_loop().time() - start > timeout:
            return {"status": "timeout", "run_id": str(run.id)}
        await asyncio.sleep(1)

    return {
        "status": run.status.value,
        "run_id": str(run.id),
        "result": run.result,
        "error": run.error,
    }
