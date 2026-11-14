"""One owner for running a DeepSpace workflow's child agents.

A workflow child is a delegated agent that the workflow itself started.  Both
workflow modes use this seam, so a named workflow and a scripted workflow
produce the same child record, the same answer, and the same structured
result for the same request.

The child runs inline, in a private database session, so the workflow sees the
child's answer as soon as the child finishes instead of a handle it would
have to poll.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from app.deepspace.services.subagent_runner import new_child_record, run_child_turn
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workflow_schema import (
    parse_structured_answer,
    structured_output_instruction,
    validate_workflow_schema,
)
from app.platform.database.session import managed_db_session, set_db_tenant_context
from app.providers.services.selection_service import ProviderSelectionService


@dataclass(frozen=True, slots=True)
class WorkflowChildRequest:
    """One child a workflow asked for."""

    #: The short label the run's progress reports.
    label: str
    #: The complete task the child carries out.
    task: str
    #: Whether the child ends with its task or stays available for messages.
    mode: str = "one-shot"
    #: The provider route the child runs under.
    provider: str | None = None
    #: The model the child runs under.
    model: str | None = None
    #: The persona that shapes how the child works.
    persona: str | None = None
    #: The only tools the child may use.
    tool_allow: list[str] | None = None
    #: The tools the child may not use.
    tool_deny: list[str] | None = None
    #: The only skills the child may see.
    skill_allow: list[str] | None = None
    #: The skills the child may not see.
    skill_deny: list[str] | None = None
    #: The JSON Schema the child's answer must satisfy.
    schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowChildOutcome:
    """What one workflow child produced."""

    #: The durable child record's id.
    agent_id: str
    #: The label the run reported.
    label: str
    #: The child's answer, exactly as the child wrote it.
    text: str
    #: Why the child stopped: ``completed``, ``error``, ``cancelled``, or
    #: ``denied``.
    stop_reason: str
    #: The failure detail, when the child did not complete cleanly.
    diagnostic: str | None = None
    #: The validated structured value, when a schema was asked for.
    value: Any = None
    #: Whether a schema was asked for.
    structured: bool = False
    #: The tools the child called, by name.
    called_tools: list[str] = field(default_factory=list)


async def run_workflow_child(
    context: ToolContext,
    request: WorkflowChildRequest,
    *,
    tool_lock: asyncio.Lock | None = None,
    check_policy: bool = True,
) -> WorkflowChildOutcome:
    """Run one workflow child to completion and read its answer.

    The child runs in a private database session, so a workflow that starts
    several children at once never shares one session between them.  The
    child's own tool calls run through the parent's executor, so they face the
    same policy the parent's tools face.

    Args:
        context: The calling workflow's tool context.
        request: The child the workflow asked for.
        tool_lock: The lock that serializes the child's tool calls, when the
            caller runs several children at once.
        check_policy: Whether the deployment's tool policy decides whether
            this conversation may start the child at all.  A scripted
            workflow keeps the gate its ``subagent`` call always had; a named
            workflow keeps the behaviour it has always had.

    Returns:
        The child's answer, and its structured value when a schema was asked
        for.
    """
    schema = request.schema
    if schema is not None:
        schema = validate_workflow_schema(schema)
    task = request.task
    if schema is not None:
        task = f"{task}\n\n{structured_output_instruction(schema)}"

    if check_policy:
        denial = _delegation_denial(context, request)
        if denial is not None:
            return WorkflowChildOutcome(
                agent_id="",
                label=request.label,
                text="",
                stop_reason="denied",
                diagnostic=denial,
            )

    with managed_db_session() as child_db:
        set_db_tenant_context(child_db, context.auth.tenant_id)
        child = new_child_record(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            parent_run_id=context.run_id,
            parent_agent_id=context.parent_agent_id,
            origin="subagent",
            mode=request.mode,
            label=request.label,
            delegation_depth=context.depth + 1,
            agent_provider=request.provider,
            agent_model=request.model,
            reasoning_effort=None,
            persona=request.persona,
            tool_filter={
                "allow": [str(name) for name in (request.tool_allow or [])],
                "deny": [str(name) for name in (request.tool_deny or [])],
            },
            inherited_history=None,
        )
        child_db.add(child)
        child_db.commit()

        child_context = replace(
            context,
            db=child_db,
            provider_selection=ProviderSelectionService(child_db, context.settings),
            native_executor=_child_executor(context, tool_lock),
        )
        outcome = await run_child_turn(context=child_context, child=child, task=task)
        child.result_text = outcome.text
        child.stop_reason = outcome.stop_reason
        child.turn_count = 1
        child.status = "idle" if outcome.stop_reason == "completed" else outcome.stop_reason
        child.last_error = outcome.diagnostic
        child_db.commit()

    value: Any = None
    diagnostic = outcome.diagnostic
    if schema is not None and outcome.stop_reason == "completed":
        value, failure = parse_structured_answer(outcome.text, schema)
        if failure is not None:
            diagnostic = failure
    return WorkflowChildOutcome(
        agent_id=str(child.id),
        label=request.label,
        text=outcome.text,
        stop_reason=outcome.stop_reason,
        diagnostic=diagnostic,
        value=value,
        structured=schema is not None,
        called_tools=list(outcome.called_tools),
    )


def _child_executor(context: ToolContext, tool_lock: asyncio.Lock | None) -> Any:
    """Build the executor one workflow child's tool calls run through."""

    async def execute(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if context.native_executor is None:
            raise RuntimeError("The workflow child has no tool executor.")
        if tool_lock is None:
            return await context.native_executor(tool_name, arguments)
        async with tool_lock:
            return await context.native_executor(tool_name, arguments)

    return execute


def _delegation_denial(context: ToolContext, request: WorkflowChildRequest) -> str | None:
    """Report why this conversation may not start the child, when it may not."""
    policy = getattr(context, "tool_policy", None)
    if policy is None:
        return None
    decision = policy.before_tool(
        "subagent",
        {
            "description": request.label,
            "task": request.task,
            "mode": request.mode,
            "provider": request.provider,
            "model": request.model,
        },
    )
    if getattr(decision, "allowed", True):
        return None
    if getattr(decision, "requires_approval", False):
        return (
            "Starting a child agent needs a direct tool call so DeepSpace can show its "
            "approval request. No child was started."
        )
    return str(getattr(decision, "reason", "") or "The child agent is blocked by DeepSpace policy.")


__all__ = [
    "WorkflowChildOutcome",
    "WorkflowChildRequest",
    "run_workflow_child",
]
