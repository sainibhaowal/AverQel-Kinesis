"""Goal tools for DeepSpace.

A goal is the standing objective of a conversation.  It outlives a turn and a
compaction, so it is where an agent records what it is working toward when
that is more than the current task.

Phase model: active, paused, blocked, complete
Revision tracking for compare-and-set mutations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select

from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    object_property,
    require_db,
    string_property,
)

#: The largest goal statement one call may carry.
MAX_GOAL_CHARS = 100_000


class GoalReadArguments(ToolArguments):
    """Read the goals of the current conversation."""

    name: str | None = Field(default=None, max_length=120)


class GoalSetArguments(ToolArguments):
    """Create, update, or finish a goal."""

    name: str = Field(min_length=1, max_length=120)
    content: str | None = Field(default=None, max_length=MAX_GOAL_CHARS)
    phase: Literal["active", "paused", "blocked", "complete"] = "active"
    note: str | None = Field(default=None, max_length=2_000)
    revision: int | None = Field(
        default=None, ge=1, description="Expected revision for compare-and-set"
    )
    max_goal_rounds: int | None = Field(
        default=None, ge=1, le=1000, description="Maximum continuation rounds"
    )
    blocked_reason: dict[str, str] | None = Field(
        default=None, description="Reason when phase is blocked"
    )


DEFAULT_BLOCKED_THRESHOLD = 3
MODEL_BLOCKED_CODE = "model-reported"
GOAL_ROUND_PHASES = frozenset({GoalPhase.COMPLETE, GoalPhase.BLOCKED})


def _serialize(goal: DeepSpaceGoal) -> dict[str, Any]:
    """Project one goal for a model-facing result."""
    return {
        "name": goal.name,
        "content": goal.content,
        "phase": goal.phase,
        "revision": goal.revision,
        "max_goal_rounds": goal.max_goal_rounds,
        "rounds_started": goal.rounds_started,
        "blocked_reason": goal.blocked_reason,
        "note": goal.note,
        "activation": goal.activation,
        "updated_at": goal.updated_at.isoformat() if goal.updated_at else None,
        "created_at": goal.created_at.isoformat() if goal.created_at else None,
        # Legacy compatibility
        "status": goal.status,
    }


def _blocked_threshold(context: ToolContext) -> int:
    """Return the configured number of rounds before model blocking is allowed."""
    try:
        value = int(
            getattr(context.settings, "deepspace_goal_blocked_threshold", DEFAULT_BLOCKED_THRESHOLD)
        )
    except (TypeError, ValueError):
        value = DEFAULT_BLOCKED_THRESHOLD
    return max(1, value)


def _is_goal_round(context: ToolContext) -> bool:
    """Whether the latest user message was an automatic goal round."""
    from app.deepspace.models.message import Message

    row = (
        require_db(context)
        .execute(
            select(Message.metadata_json)
            .where(
                Message.conversation_id == context.conversation_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    source = row.get("source") if isinstance(row, dict) else None
    return (
        isinstance(source, dict)
        and source.get("kind") == "goal"
        and int(source.get("round", 0) or 0) > 0
    )


def _authority_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "message": message, **extra}


async def _goal_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the goals of the current conversation."""
    filters = [
        DeepSpaceGoal.tenant_id == context.auth.tenant_id,
        DeepSpaceGoal.user_id == context.auth.user_id,
        DeepSpaceGoal.conversation_id == context.conversation_id,
    ]
    name = arguments.get("name")
    if name:
        filters.append(DeepSpaceGoal.name == str(name))
    rows = (
        require_db(context)
        .execute(select(DeepSpaceGoal).where(*filters).order_by(DeepSpaceGoal.updated_at.desc()))
        .scalars()
        .all()
    )
    goals = [_serialize(goal) for goal in rows]
    return {"status": "ok", "goal_count": len(goals), "goals": goals}


async def _goal_set(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create, update, or finish a goal."""
    name = str(arguments["name"]).strip()
    expected_revision = arguments.get("revision")
    new_phase = str(arguments.get("phase") or GoalPhase.ACTIVE)
    if new_phase not in (GoalPhase.ACTIVE, GoalPhase.PAUSED, GoalPhase.BLOCKED, GoalPhase.COMPLETE):
        return _authority_error("GOAL_INVALID_PHASE", f"Invalid phase: {new_phase}")

    existing = (
        require_db(context)
        .execute(
            select(DeepSpaceGoal).where(
                DeepSpaceGoal.tenant_id == context.auth.tenant_id,
                DeepSpaceGoal.user_id == context.auth.user_id,
                DeepSpaceGoal.conversation_id == context.conversation_id,
                DeepSpaceGoal.name == name,
            )
        )
        .scalar_one_or_none()
    )

    goal_round = _is_goal_round(context)
    if goal_round and existing is not None and new_phase not in GOAL_ROUND_PHASES:
        return _authority_error(
            "GOAL_ROUND_PHASE_FORBIDDEN",
            "An automatic goal round may only complete or block the goal it is running.",
            phase=new_phase,
        )
    if goal_round and existing is None:
        return _authority_error(
            "GOAL_ROUND_CREATE_FORBIDDEN",
            "An automatic goal round may not create a goal. A person must create it.",
        )

    if existing is None:
        existing = DeepSpaceGoal(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            name=name,
            content="",
            phase=GoalPhase.ACTIVE,
            max_goal_rounds=arguments.get("max_goal_rounds") or 3,
            activation=GoalActivation.ARMED,
        )
        require_db(context).add(existing)
    else:
        # Check revision for compare-and-set
        if expected_revision is not None and existing.revision != expected_revision:
            return {
                "status": "error",
                "message": f"Goal revision mismatch: expected {expected_revision}, found {existing.revision}",
                "current_revision": existing.revision,
                "error_code": "GOAL_REVISION_MISMATCH",
            }
        if new_phase == GoalPhase.BLOCKED and existing.phase != GoalPhase.BLOCKED:
            threshold = _blocked_threshold(context)
            if int(existing.rounds_started or 0) < threshold:
                return _authority_error(
                    "GOAL_BLOCKED_BEFORE_THRESHOLD",
                    f"A goal may only be marked blocked after {threshold} consecutive goal rounds.",
                    threshold=threshold,
                    rounds_started=int(existing.rounds_started or 0),
                )

    if arguments.get("content") is not None:
        existing.content = str(arguments["content"])[:MAX_GOAL_CHARS]

    existing.phase = new_phase
    if new_phase == GoalPhase.COMPLETE:
        existing.status = "reached"
    elif new_phase == GoalPhase.BLOCKED:
        existing.status = "abandoned"
    else:
        existing.status = new_phase
    existing.revision = (existing.revision or 0) + 1  # Increment on every mutation

    if arguments.get("max_goal_rounds") is not None:
        existing.max_goal_rounds = int(arguments["max_goal_rounds"])

    if new_phase == GoalPhase.BLOCKED:
        existing.blocked_reason = arguments.get("blocked_reason") or {
            "code": MODEL_BLOCKED_CODE,
            "message": "The model reported a blocker without naming one.",
        }
    elif new_phase != GoalPhase.BLOCKED:
        existing.blocked_reason = None

    existing.activation = (
        GoalActivation.DISARMED
        if new_phase in (GoalPhase.PAUSED, GoalPhase.BLOCKED, GoalPhase.COMPLETE)
        else GoalActivation.ARMED
    )

    if arguments.get("note"):
        existing.note = str(arguments["note"])[:2_000]

    require_db(context).commit()
    return {"status": "ok", "goal": _serialize(existing)}


GOAL_DESCRIPTION = (
    "Goal tools. A goal is the standing objective of this conversation; it "
    "survives a new turn. Use goal_set to record or update one and goal_read "
    "to see the current ones. Phases: active, paused, blocked, complete. "
    "Revisions increment on every mutation for compare-and-set safety."
)

GOAL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="goal_read",
        description=GOAL_DESCRIPTION,
        properties={
            "name": string_property(
                "Read only the goal with this name. Omit to read every goal.",
                max_length=120,
            )
        },
        arguments_model=GoalReadArguments,
        mode="read",
        handler=_goal_read,
    ),
    define_native_tool(
        name="goal_set",
        description="Create, update, or finish a goal for this conversation.",
        properties={
            "name": string_property(
                "The goal's name, unique within the conversation.",
                min_length=1,
                max_length=120,
            ),
            "content": string_property("The goal's statement.", max_length=8_000),
            "phase": string_property(
                "The goal's phase: active, paused, blocked, or complete.",
                enum=["active", "paused", "blocked", "complete"],
            ),
            "note": string_property(
                "A note explaining the state, especially for an abandoned goal.",
                max_length=2_000,
            ),
            "revision": integer_property(
                "Expected revision for compare-and-set. Omit for create.", minimum=1
            ),
            "max_goal_rounds": integer_property(
                "Maximum continuation rounds (default 3).", minimum=1, maximum=10
            ),
            "blocked_reason": object_property(
                "Reason when phase is blocked. Required for 'blocked' phase.",
                additional_properties=True,
            ),
        },
        required=["name"],
        arguments_model=GoalSetArguments,
        mode="write",
        handler=_goal_set,
    ),
]
