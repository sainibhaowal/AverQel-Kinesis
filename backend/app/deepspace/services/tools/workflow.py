"""The workflow runner for DeepSpace.

A workflow is a JSON document in the project workspace that names an ordered
set of steps.  Each step is a task, and each step runs as its own child
agent, so a workflow gets the same tools, the same model, and the same
durable trail a delegated child gets.  Steps that declare dependencies wait
for the steps they depend on.

Also supports inline JavaScript orchestration scripts via the 'script' parameter.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from pydantic import Field

from app.deepspace.services.subagent_runner import new_child_record, run_child_turn
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    object_property,
    require_db,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)
from app.platform.database.session import managed_db_session, set_db_tenant_context
from app.providers.services.selection_service import ProviderSelectionService

#: The directory under the workspace root that holds the workflows.
WORKFLOWS_DIRECTORY = ".averqel/workflows"

#: The largest workflow document one call may read, in characters.
MAX_WORKFLOW_CHARS = 200_000

#: The largest number of steps one workflow may declare.
MAX_WORKFLOW_STEPS = 20

#: The largest task text one step may carry.
MAX_STEP_TASK_CHARS = 20_000


class WorkflowRunArguments(ToolArguments):
    """Run a workflow from the project workspace or a script."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    input: str | None = Field(default=None, max_length=20_000)
    # Script mode (alternative to name)
    script: str | None = Field(
        default=None, max_length=500_000, description="JavaScript orchestration script"
    )
    meta: dict[str, Any] | None = Field(default=None, description="Workflow metadata")


def _load_workflow(context: ToolContext, name: str) -> tuple[dict[str, Any], str]:
    """Read one workflow document from the workspace.

    Args:
        context: The calling request's tool context.
        name: The workflow's name.

    Returns:
        The parsed workflow and its workspace-relative path.

    Raises:
        WorkspacePathError: When the name does not resolve to a workspace path.
        ValueError: When the document is missing or malformed.
    """
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    try:
        resolved = resolve_within_root(root, f"{WORKFLOWS_DIRECTORY}/{name}.json")
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc
    document = resolved.absolute
    if not document.is_file():
        raise ValueError(f"No workflow named '{name}' is installed under {WORKFLOWS_DIRECTORY}.")
    raw = document.read_text(encoding="utf-8", errors="replace")[:MAX_WORKFLOW_CHARS]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"The workflow document for '{name}' is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"The workflow document for '{name}' must be a JSON object.")
    return parsed, resolved.relative


def _workflow_steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Read and validate a workflow's steps.

    Args:
        document: The parsed workflow document.

    Returns:
        The steps, each with an id, a task, and its dependencies.

    Raises:
        ValueError: When the document declares no usable step.
    """
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("The workflow document declares no steps.")
    if len(raw_steps) > MAX_WORKFLOW_STEPS:
        raise ValueError(f"A workflow may declare at most {MAX_WORKFLOW_STEPS} steps.")
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"Workflow step {index + 1} must be an object.")
        task = str(raw.get("task") or "").strip()[:MAX_STEP_TASK_CHARS]
        if not task:
            raise ValueError(f"Workflow step {index + 1} has no task.")
        step_id = str(raw.get("id") or f"step-{index + 1}")
        if not step_id.strip():
            raise ValueError(f"Workflow step {index + 1} has no id.")
        depends_on = [
            str(item)
            for item in (raw.get("depends_on") or raw.get("dependencies") or [])
            if str(item)
        ]
        steps.append({"id": step_id, "task": task, "depends_on": depends_on})
    known = {step["id"] for step in steps}
    if len(known) != len(steps):
        raise ValueError("Workflow step ids must be unique.")
    for step in steps:
        unknown = [item for item in step["depends_on"] if item not in known]
        if unknown:
            raise ValueError(
                f"Workflow step '{step['id']}' depends on unknown steps: {', '.join(unknown[:5])}."
            )
    return steps


def _order_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the steps so every step follows the ones it depends on.

    Args:
        steps: The validated steps.

    Returns:
        The steps in execution order.

    Raises:
        ValueError: When the dependency graph contains a cycle.
    """
    by_id = {step["id"]: step for step in steps}
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    visiting: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in placed:
            return
        if step_id in visiting:
            raise ValueError("The workflow's step dependencies contain a cycle.")
        visiting.add(step_id)
        for dependency in by_id[step_id]["depends_on"]:
            visit(dependency)
        visiting.discard(step_id)
        placed.add(step_id)
        ordered.append(by_id[step_id])

    for step in steps:
        visit(step["id"])
    return ordered


def _workflow_waves(steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group dependency-ready steps into deterministic execution waves.

    Independent steps in one wave are safe to run concurrently. A later wave
    is not released until every step in the current wave has produced a
    result, so dependency outputs are complete before they are supplied to a
    child.

    Args:
        steps: Validated workflow steps.

    Returns:
        Ordered waves, preserving the declaration order within each wave.

    Raises:
        ValueError: When dependencies contain a cycle.
    """
    pending = {step["id"]: step for step in steps}
    completed: set[str] = set()
    waves: list[list[dict[str, Any]]] = []
    while pending:
        ready = [
            step for step in steps if step["id"] in pending and set(step["depends_on"]) <= completed
        ]
        if not ready:
            raise ValueError("The workflow's step dependencies contain a cycle.")
        waves.append(ready)
        for step in ready:
            pending.pop(step["id"])
            completed.add(step["id"])
    return waves


def _workflow_concurrency(document: dict[str, Any], step_count: int) -> int:
    """Resolve the bounded concurrency for named workflow steps."""
    raw = document.get("max_concurrency", step_count)
    try:
        concurrency = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Workflow max_concurrency must be an integer.") from exc
    if concurrency < 1:
        raise ValueError("Workflow max_concurrency must be at least one.")
    return min(concurrency, step_count, MAX_WORKFLOW_STEPS)


async def _workflow_run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a workflow from the project workspace or a script."""
    script = arguments.get("script")
    meta = arguments.get("meta") or {}
    name = arguments.get("name")

    # Script mode
    if script is not None:
        if not script.strip():
            return {"status": "error", "message": "Script cannot be empty"}

        workflow_name = meta.get("name", "inline-workflow")

        ptc_program = f"""
// Workflow execution context
const workflow = {{
    meta: {json.dumps(meta)},
    args: {json.dumps(arguments.get("input") or {})},
    agents: [],
    currentPhase: null,

    async agent(task, options = {{}}) {{
        const label = options.label || `agent-${{this.agents.length + 1}}`;
        const phase = options.phase || this.currentPhase;
        const seq = this.agents.length + 1;

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
            return result.answer;
        }} catch (err) {{
            throw err;
        }}
    }},

    async parallel(items, mapper, options = {{}}) {{
        const concurrency = options.concurrency || 4;
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
    }},

    log(message) {{
        // Log to output
    }},

    returnValue(value) {{
        throw {{ __workflow_return: true, value }};
    }},
}};

// User script
{script}
"""

        try:
            # Lazy import to avoid circular dependency
            from app.deepspace.services.ptc_runtime import run_program

            outcome = await run_program(
                context=context,
                program=ptc_program,
                tool_names=["subagent"],
                timeout_seconds=3600,
                language="javascript",
            )

            if outcome.status == "failed":
                return {
                    "status": "failed",
                    "workflow": workflow_name,
                    "message": outcome.error or "Workflow execution failed",
                    "output": outcome.output,
                }

            return {
                "status": "ok",
                "workflow": workflow_name,
                "result": outcome.result,
                "output": outcome.output,
            }

        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # Workspace workflow mode (existing logic)
    if name is None:
        return {"status": "error", "message": "Either 'name' or 'script' must be provided"}

    try:
        document, relative = _load_workflow(context, str(name))
        validated_steps = _workflow_steps(document)
        waves = _workflow_waves(validated_steps)
        concurrency = _workflow_concurrency(document, len(validated_steps))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    supplied_input = str(arguments.get("input") or "").strip()
    results: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    tool_lock = asyncio.Lock()

    async def run_step(
        step: dict[str, Any],
        dependency_outputs: dict[str, str],
        child: Any,
    ) -> tuple[dict[str, Any], Any, Any]:
        """Run one child with a private database session."""
        task = step["task"]
        if supplied_input:
            task = f"{task}\n\nInput:\n{supplied_input}"
        if dependency_outputs:
            prior = "\n\n".join(
                f"Result of {dependency}:\n{value}"
                for dependency, value in dependency_outputs.items()
            )
            task = f"{task}\n\nDependency results:\n{prior}"

        with managed_db_session() as child_db:
            set_db_tenant_context(child_db, context.auth.tenant_id)

            async def execute_child_tool(
                tool_name: str, arguments: dict[str, Any]
            ) -> dict[str, Any]:
                if context.native_executor is None:
                    raise RuntimeError("The workflow child has no tool executor.")
                # Native tools currently share the parent service's executor.
                # Serialize only tool calls; model requests remain concurrent.
                async with tool_lock:
                    return await context.native_executor(tool_name, arguments)

            child_context = replace(
                context,
                db=child_db,
                provider_selection=ProviderSelectionService(child_db, context.settings),
                native_executor=execute_child_tool,
            )
            outcome = await run_child_turn(context=child_context, child=child, task=task)
        return step, child, outcome

    for wave in waves:
        if context.cancel_check is not None and context.cancel_check():
            return {
                "status": "cancelled",
                "workflow": str(name),
                "path": relative,
                "completed_steps": results,
                "message": "The workflow was cancelled before it finished.",
            }
        children: list[tuple[dict[str, Any], Any]] = []
        for step in wave:
            child = new_child_record(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                parent_run_id=context.run_id,
                parent_agent_id=context.parent_agent_id,
                origin="subagent",
                mode="one-shot",
                label=f"{name}:{step['id']}",
                delegation_depth=context.depth + 1,
                agent_provider=None,
                agent_model=None,
                reasoning_effort=None,
                persona=None,
                tool_filter={},
                inherited_history=None,
            )
            require_db(context).add(child)
            children.append((step, child))
        require_db(context).commit()

        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_run(
            step: dict[str, Any], child: Any, wave_semaphore: asyncio.Semaphore
        ) -> tuple[dict[str, Any], Any, Any]:
            async with wave_semaphore:
                dependencies = {
                    dependency: outputs[dependency]
                    for dependency in step["depends_on"]
                    if dependency in outputs
                }
                return await run_step(step, dependencies, child)

        completed_wave = await asyncio.gather(
            *(bounded_run(step, child, semaphore) for step, child in children),
            return_exceptions=True,
        )
        failed_step: str | None = None
        for item in completed_wave:
            if isinstance(item, BaseException):
                raise item
            step, child, outcome = item
            outcome_text = getattr(outcome, "text", "")
            stop_reason = getattr(outcome, "stop_reason", "error")
            diagnostic = getattr(outcome, "diagnostic", None)
            child.result_text = outcome_text
            child.stop_reason = stop_reason
            child.turn_count = 1
            child.status = "idle" if stop_reason == "completed" else stop_reason
            child.last_error = diagnostic
            outputs[step["id"]] = outcome_text
            results.append(
                {
                    "step_id": step["id"],
                    "agent_id": str(child.id),
                    "stop_reason": stop_reason,
                    "output": outcome_text,
                    "diagnostic": diagnostic,
                }
            )
            if stop_reason != "completed" and failed_step is None:
                failed_step = step["id"]
        require_db(context).commit()
        if failed_step is not None:
            return {
                "status": "failed",
                "workflow": str(name),
                "path": relative,
                "completed_steps": results,
                "message": f"Step '{failed_step}' did not complete cleanly.",
            }
    return {
        "status": "ok",
        "workflow": str(name),
        "path": relative,
        "step_count": len(results),
        "steps": results,
    }


WORKFLOW_DESCRIPTION = (
    "Run a workflow installed in the project workspace under "
    f"{WORKFLOWS_DIRECTORY}. Each step runs as its own agent. The workflow "
    "document names the steps and the dependencies between them. "
    "Alternatively, provide a 'script' parameter with a JavaScript orchestration script."
)

WORKFLOW_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="workflow_run",
        description=WORKFLOW_DESCRIPTION,
        properties={
            "name": string_property(
                "The name of the workflow to run.", min_length=1, max_length=120
            ),
            "input": string_property("An input the workflow's steps receive.", max_length=20_000),
            "script": string_property(
                "JavaScript orchestration script (alternative to name).", max_length=500_000
            ),
            "meta": object_property(
                "Workflow metadata for script mode.",
                additional_properties=True,
            ),
        },
        required=[],
        arguments_model=WorkflowRunArguments,
        mode="write",
        handler=_workflow_run,
    ),
]

__all__ = ["WORKFLOW_TOOLS", "WorkflowRunArguments"]
