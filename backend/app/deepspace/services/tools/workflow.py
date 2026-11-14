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
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    boolean_property,
    define_native_tool,
    object_property,
    string_property,
)
from app.deepspace.services.workflow_children import (
    WorkflowChildOutcome,
    WorkflowChildRequest,
    run_workflow_child,
)
from app.deepspace.services.workflow_jobs import WORKFLOW_START, WorkflowJobStore
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)

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
    run_in_background: bool = Field(
        default=False,
        description="Queue the workflow and return a durable job handle immediately.",
    )


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


async def run_named_workflow(
    arguments: dict[str, Any],
    context: ToolContext,
    *,
    progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run a workflow from the project workspace or a script."""
    script = arguments.get("script")
    meta = arguments.get("meta") or {}
    name = arguments.get("name")

    # Script mode uses the shared typed engine so foreground and background
    # runs have identical lifecycle, cancellation, phase, and log semantics.
    if script is not None:
        if not script.strip():
            return {"status": "error", "message": "Script cannot be empty"}
        meta = dict(meta)
        from app.deepspace.services.workflow_engine import (
            WorkflowEngine,
            WorkflowMeta,
            WorkflowStartRequest,
        )

        workflow_meta = WorkflowMeta(
            name=str(meta.get("name") or "inline-workflow"),
            version=str(meta.get("version") or "1"),
            description=str(meta.get("description") or ""),
            max_total_agents=int(meta.get("max_total_agents") or 100),
            max_concurrent_agents=int(meta.get("max_concurrent_agents") or 4),
            subagent_provider=meta.get("subagent_provider"),
        )
        try:
            engine = WorkflowEngine(context.settings, context)
            run = await engine.start(
                WorkflowStartRequest(
                    script=script,
                    meta=workflow_meta,
                    args={"input": arguments.get("input") or ""},
                    run_in_background=bool(arguments.get("run_in_background")),
                )
            )
            if arguments.get("run_in_background"):
                return {
                    "status": "background",
                    "workflow": workflow_meta.name,
                    "run_id": str(run.id),
                    "job_id": str(run.id),
                }
            try:
                value = await run.wait()
                return {
                    "status": run.status.value,
                    "workflow": workflow_meta.name,
                    "run_id": str(run.id),
                    "result": value,
                    "error": run.error,
                }
            finally:
                await run.dispose()
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible
            return {"status": "error", "message": str(exc)}

    # Workspace workflow mode (existing logic)
    if name is None:
        return {"status": "error", "message": "Either 'name' or 'script' must be provided"}

    if arguments.get("run_in_background"):
        if context.db is None:
            return {
                "status": "error",
                "message": "Background workflows require a database context.",
            }
        try:
            document, relative = _load_workflow(context, str(name))
            validated_steps = _workflow_steps(document)
            _workflow_waves(validated_steps)
            store = WorkflowJobStore(context.db)
            job = store.create(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                mode="named",
                workflow_name=str(name),
                script=None,
                input_text=str(arguments.get("input") or ""),
                meta={
                    "name": str(name),
                    "max_concurrency": _workflow_concurrency(document, len(validated_steps)),
                    "step_count": len(validated_steps),
                },
                args={"input": str(arguments.get("input") or "")},
                roles=sorted(context.auth.roles),
                permissions=sorted(context.auth.permissions),
            )
            store.append(
                job, WORKFLOW_START, {"mode": "named", "path": relative, "background": True}
            )
            from app.deepspace.workers.workflow_tasks import run_workflow_job_task

            run_workflow_job_task.apply_async(
                kwargs={
                    "job_id": str(job.id),
                    "tenant_id": str(job.tenant_id),
                    "user_id": str(job.user_id),
                    "conversation_id": str(job.conversation_id),
                }
            )
            return {
                "status": "background",
                "workflow": str(name),
                "path": relative,
                "run_id": str(job.id),
                "job_id": str(job.id),
            }
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    # The production SQLAlchemy context uses the same durable engine for named
    # foreground runs.  The small no-database fallback below remains for unit
    # callers that exercise the dependency planner in isolation.
    if context.db is not None and hasattr(context.db, "execute"):
        try:
            document, relative = _load_workflow(context, str(name))
            validated_steps = _workflow_steps(document)
            concurrency = _workflow_concurrency(document, len(validated_steps))
            store = WorkflowJobStore(context.db)
            job = store.create(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                mode="named",
                workflow_name=str(name),
                script=None,
                input_text=str(arguments.get("input") or ""),
                meta={
                    "name": str(name),
                    "max_concurrency": concurrency,
                    "step_count": len(validated_steps),
                },
                args={"input": str(arguments.get("input") or "")},
                roles=sorted(context.auth.roles),
                permissions=sorted(context.auth.permissions),
            )
            store.append(job, WORKFLOW_START, {"mode": "named", "path": relative})
            from app.deepspace.services.workflow_engine import WorkflowEngine

            run = await WorkflowEngine(context.settings, context).run_persisted_job(job)
            return {
                "status": "ok" if run.status.value == "completed" else run.status.value,
                "workflow": str(name),
                "path": relative,
                "run_id": str(run.id),
                "result": run.result,
                "error": run.error,
            }
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    try:
        return await execute_named_workflow(
            context,
            name=str(name),
            input_text=str(arguments.get("input") or ""),
            progress=progress,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}


async def execute_named_workflow(
    context: ToolContext,
    *,
    name: str,
    input_text: str,
    progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run the steps of one named workflow document.

    This is the execution half of a named workflow.  It never creates a job
    and never queues a task, so the engine can run it for a foreground run
    and a durable worker can run it for a background run without either path
    re-entering the tool that started it.

    Args:
        context: The calling workflow's tool context.
        name: The workflow document's name.
        input_text: The input the document's steps receive.
        progress: The reporter that receives each phase and log event.

    Returns:
        The workflow's outcome, with one entry per completed step.

    Raises:
        ValueError: When the document is missing, malformed, or cyclic.
    """
    document, relative = _load_workflow(context, name)
    validated_steps = _workflow_steps(document)
    waves = _workflow_waves(validated_steps)
    concurrency = _workflow_concurrency(document, len(validated_steps))
    if progress is not None:
        await progress("workflow/phase", {"title": "validated"})
    supplied_input = str(input_text or "").strip()
    results: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    tool_lock = asyncio.Lock()

    async def run_step(
        step: dict[str, Any],
        dependency_outputs: dict[str, str],
    ) -> tuple[dict[str, Any], WorkflowChildOutcome]:
        """Run one child with the step's task and its dependencies' results."""
        task = step["task"]
        if supplied_input:
            task = f"{task}\n\nInput:\n{supplied_input}"
        if dependency_outputs:
            prior = "\n\n".join(
                f"Result of {dependency}:\n{value}"
                for dependency, value in dependency_outputs.items()
            )
            task = f"{task}\n\nDependency results:\n{prior}"
        outcome = await run_workflow_child(
            context,
            WorkflowChildRequest(label=f"{name}:{step['id']}", task=task),
            tool_lock=tool_lock,
            # A named workflow has always started its own children directly.
            check_policy=False,
        )
        return step, outcome

    for wave_index, wave in enumerate(waves, start=1):
        if progress is not None:
            await progress(
                "workflow/phase",
                {"title": f"wave-{wave_index}", "step_count": len(wave)},
            )
        if context.cancel_check is not None and context.cancel_check():
            return {
                "status": "cancelled",
                "workflow": str(name),
                "path": relative,
                "completed_steps": results,
                "message": "The workflow was cancelled before it finished.",
            }
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_run(
            step: dict[str, Any], wave_semaphore: asyncio.Semaphore
        ) -> tuple[dict[str, Any], WorkflowChildOutcome]:
            async with wave_semaphore:
                dependencies = {
                    dependency: outputs[dependency]
                    for dependency in step["depends_on"]
                    if dependency in outputs
                }
                return await run_step(step, dependencies)

        completed_wave = await asyncio.gather(
            *(bounded_run(step, semaphore) for step in wave),
            return_exceptions=True,
        )
        failed_step: str | None = None
        for item in completed_wave:
            if isinstance(item, BaseException):
                raise item
            step, outcome = item
            outputs[step["id"]] = outcome.text
            results.append(
                {
                    "step_id": step["id"],
                    "agent_id": outcome.agent_id,
                    "stop_reason": outcome.stop_reason,
                    "output": outcome.text,
                    "diagnostic": outcome.diagnostic,
                }
            )
            if progress is not None:
                await progress(
                    "workflow/log",
                    {
                        "message": f"Step '{step['id']}' finished with {outcome.stop_reason}.",
                        "step_id": step["id"],
                        "status": outcome.stop_reason,
                    },
                )
            if outcome.stop_reason != "completed" and failed_step is None:
                failed_step = step["id"]
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
    "Run a workflow that fans work out across many child agents and return the "
    "workflow's final value. Use this when the user asks for a workflow, or for "
    "work that splits across many independent pieces: an audit over many files, a "
    "migration, multi-angle research, or an adversarial check of findings. For one "
    "or two delegations, call the subagent tool directly instead.\n"
    "\n"
    "Two forms are accepted:\n"
    "- 'name': the name of a workflow document installed under "
    f"{WORKFLOWS_DIRECTORY} in the project workspace. The document lists the steps "
    "and the dependencies between them; each step runs as its own agent.\n"
    "- 'script': a JavaScript orchestration script. Its hooks are:\n"
    "  * workflow.agent(task, options?) - run one child agent to completion and "
    "resolve to its answer. options: {label, phase, mode, persona, model, provider, "
    "tool_allow, tool_deny, schema}. With no schema the answer is the child's text. "
    "With a schema the answer is the validated JSON object, or null when the child "
    "failed or did not match the schema. A schema is an object-rooted JSON Schema "
    "using only type/properties/required/additionalProperties/items/enum/const/oneOf.\n"
    "  * workflow.parallel(items, mapper, options?) - run mapper over every item and "
    "resolve to the results in order. options: {concurrency}.\n"
    "  * workflow.pipeline(items, ...stages) - run every item through the stages; "
    "each stage receives (previous, item, index) and the hook resolves to one result "
    "per item. A stage that throws drops that item to null and skips its remaining "
    "stages. workflow.pipeline(stages, {initial}) is also accepted and runs one "
    "value through the stages in order.\n"
    "  * workflow.phase(title) - start a progress phase. workflow.log(message) - "
    "narrate progress. workflow.args - this call's input, verbatim.\n"
    "  * End the script with 'return <value>'; that value is the tool's result.\n"
    "  * Misused hooks (a bad argument, an unknown option, an unsupported schema, a "
    "tripped agent cap) end the whole script instead of producing null. The script "
    "has no filesystem, network, timer, or Node.js APIs; the agents do the work."
)

#: The guidance the agent reads when the workflow tool is available.  The
#: tool description carries the hook contract; this section says when to
#: reach for the tool, which a description cannot say.
WORKFLOW_SYSTEM_GUIDANCE = (
    "Use the workflow_run tool when the user asks for a workflow, or when the "
    "work genuinely fans out across many independent pieces: an audit over many "
    "files, a migration, multi-angle research, or an adversarial check of "
    "findings. You write a JavaScript orchestration script that starts the "
    "children, groups them into phases with workflow.phase, narrates with "
    "workflow.log, and ends with 'return <value>'. The tool description "
    "documents every hook and its exact arguments, so read it before writing a "
    "script.\n"
    "For one or two delegations, call the subagent tool directly instead; a "
    "workflow is the heavier tool and it is meant for fan-out."
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
                "JavaScript orchestration script (alternative to name). The tool description "
                "documents the script's hooks: workflow.agent, workflow.parallel, "
                "workflow.pipeline, workflow.phase, workflow.log, and workflow.args.",
                max_length=500_000,
            ),
            "meta": object_property(
                "Workflow metadata for script mode.",
                additional_properties=True,
            ),
            "run_in_background": boolean_property(
                "Queue the workflow and return a durable job id without waiting for completion."
            ),
        },
        required=[],
        arguments_model=WorkflowRunArguments,
        mode="write",
        handler=run_named_workflow,
    ),
]


async def _workflow_run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Backward-compatible test and registry entry point."""
    return await run_named_workflow(arguments, context)


__all__ = [
    "WORKFLOW_SYSTEM_GUIDANCE",
    "WORKFLOW_TOOLS",
    "WorkflowRunArguments",
    "execute_named_workflow",
    "run_named_workflow",
]
