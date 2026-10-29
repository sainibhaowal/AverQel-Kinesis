"""Plan-mode tools for DeepSpace.

While a conversation is in plan mode the agent explores and designs instead
of acting.  ``plan_submit`` presents the finished design for review, and
``plan_read`` reads the conversation's current plan and its review state.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.plan_mode import PlanModeService
from app.deepspace.services.plan_store import (
    MAX_PLAN_CHARS,
    MAX_PLAN_TITLE_CHARS,
    PlanStoreError,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    require_db,
    string_property,
)


class PlanSubmitArguments(ToolArguments):
    """Present a finished plan for the user to review."""

    title: str = Field(min_length=1, max_length=MAX_PLAN_TITLE_CHARS)
    plan: str = Field(min_length=1, max_length=MAX_PLAN_CHARS)


class PlanReadArguments(ToolArguments):
    """Read the conversation's current plan."""


def _store(context: ToolContext) -> PlanModeService:
    """Return the plan-mode service for the calling request."""
    return PlanModeService(require_db(context))


async def _plan_submit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Present a finished plan for the user to review."""
    service = _store(context)
    try:
        plan = service.submit(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            run_id=context.run_id,
            title=str(arguments["title"]),
            content=str(arguments["plan"]),
        )
    except PlanStoreError as exc:
        return {"status": "error", "message": str(exc)}
    return {
        "status": "proposed",
        "plan": plan.as_dict(),
        "message": (
            "The plan is with the user for review. Wait for their decision; do not "
            "start the work until the plan is approved."
        ),
    }


async def _plan_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the conversation's current plan and its review state."""
    service = _store(context)
    plan = service.current_plan(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    if plan is None:
        return {
            "status": "ok",
            "plan": None,
            "plan_mode_active": service.active(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
            ),
            "message": "This conversation has no plan yet.",
        }
    return {
        "status": "ok",
        "plan": plan.as_dict(),
        "plan_mode_active": service.active(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        ),
    }


PLAN_SUBMIT_DESCRIPTION = (
    "Present a finished plan for the user to review. Use this at the end of a planning "
    "turn: the plan is Markdown that starts with a heading, names the steps in order, and "
    "states what each step will do and how it will be verified. The user approves it, asks "
    "for changes, or rejects it. Do not begin the work before the plan is approved."
)
PLAN_READ_DESCRIPTION = (
    "Read this conversation's current plan and its review state, including the user's "
    "feedback when they asked for changes."
)

PLAN_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="plan_submit",
        description=PLAN_SUBMIT_DESCRIPTION,
        properties={
            "title": string_property(
                "The plan's title.", min_length=1, max_length=MAX_PLAN_TITLE_CHARS
            ),
            "plan": string_property(
                "The plan itself, as Markdown that starts with a heading.",
                min_length=1,
                max_length=MAX_PLAN_CHARS,
            ),
        },
        required=["title", "plan"],
        arguments_model=PlanSubmitArguments,
        mode="write",
        handler=_plan_submit,
    ),
    define_native_tool(
        name="plan_read",
        description=PLAN_READ_DESCRIPTION,
        properties={},
        arguments_model=PlanReadArguments,
        mode="read",
        handler=_plan_read,
    ),
]

__all__ = ["PLAN_TOOLS", "PlanReadArguments", "PlanSubmitArguments"]
