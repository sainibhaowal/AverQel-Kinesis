"""The human-facing ``/goal`` command for DeepSpace.

A person controls the conversation's goals without spending a model turn.
The command creates, edits, pauses, resumes, completes, blocks, and clears a
goal, and it shows the current goal's state.

The command runs in the UI command plane.  Its input, its mutations, and its
direct output stay out of the model request, except for the ordinary
conversation history the command's own mutations produce.

The grammar is deliberate: the control words ``clear``, ``pause``, ``resume``,
``list``, ``complete``, ``status``, and ``create`` are recognized only when
they occupy the whole input.  ``edit`` and ``block`` take their argument
inline.  Any other non-empty input is an objective, so ``/goal pause after
verification`` creates that literal objective rather than pausing a goal.

DeepSpace keeps the ``list``, ``complete``, ``block``, ``status``, and
``create`` control words that its own goal bar and its earlier command
surface already used.  The Harness command plane recognizes ``clear``,
``pause``, ``resume``, and ``edit``; the extra words only add commands a
person can run and never change what those four do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.deepspace.models.message import Message
from app.deepspace.services.tools.goal import MAX_GOAL_CHARS

logger = logging.getLogger(__name__)


def _submit_attachments(
    db: Session,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    attachments: list[dict[str, Any]],
) -> None:
    """Submit attachments as a user message for the next goal round to read.

    Mirrors the Harness behavior: attachments are submitted as a user message
    with source kind 'user' and a reference text, so the next goal round can
    read them from ordinary session history.
    """
    if not attachments:
        return
    try:
        message = Message(
            conversation_id=conversation_id,
            role="user",
            content="Reference attachments for the goal objective.",
            metadata_json={
                "source": {"kind": "user"},
                "attachments": attachments,
            },
            created_at=datetime.now(UTC),
        )
        db.add(message)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to submit goal attachments", exc_info=True)


#: The control words the command recognizes on their own.
CONTROL_WORDS = frozenset(
    {
        "clear",
        "pause",
        "resume",
        "list",
        "complete",
        "status",
    }
)

#: The control words that take their argument inline.
INLINE_CONTROL_WORDS = frozenset({"edit", "block", "create"})

#: The stable code a person-requested blocker carries.
HUMAN_BLOCKED_CODE = "human-requested"


class GoalCommandError(Exception):
    """Raised when a goal command cannot carry out what it was asked to do."""

    def __init__(self, message: str, *, code: str = "GOAL_COMMAND_REFUSED") -> None:
        """Create the error.

        Args:
            message: The sentence the person reads.
            code: The stable refusal code.
        """
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True)
class GoalCommandOutcome:
    """What one goal command did.

    Attributes:
        success: Whether the command carried out its request.
        message: The sentence the person reads.
        data: The structured result the UI renders.
    """

    success: bool
    message: str
    data: dict[str, Any]


def parse_goal_command(raw_input: str) -> tuple[str, str]:
    """Split a ``/goal`` input into its control word and its argument.

    A control word is recognized only when it fills the whole input.  ``edit``,
    ``block``, and ``create`` take their argument inline.  Any other non-empty
    input is an objective.

    Args:
        raw_input: The text after the ``/goal`` command name.

    Returns:
        The control word and the remaining argument.  The word is empty only
        for the bare ``/goal`` command, which shows the current goal.
    """
    text = str(raw_input or "").strip()
    if not text:
        return "", ""
    head, _, tail = text.partition(" ")
    word = head.strip().casefold()
    argument = tail.strip()
    if word in CONTROL_WORDS and not argument:
        return word, ""
    if word in INLINE_CONTROL_WORDS:
        return word, argument
    return "create", text


def _goals(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
) -> list[DeepSpaceGoal]:
    """Read the conversation's goals, newest first.

    Args:
        db: The request's database session.
        tenant_id: The tenant that owns the conversation.
        user_id: The user that owns the conversation.
        conversation_id: The conversation the goals belong to.

    Returns:
        The conversation's goals.
    """
    return list(
        db.execute(
            select(DeepSpaceGoal)
            .where(
                DeepSpaceGoal.tenant_id == tenant_id,
                DeepSpaceGoal.user_id == user_id,
                DeepSpaceGoal.conversation_id == conversation_id,
            )
            .order_by(DeepSpaceGoal.updated_at.desc(), DeepSpaceGoal.id.desc())
        )
        .scalars()
        .all()
    )


def _current_goal(goals: list[DeepSpaceGoal]) -> DeepSpaceGoal | None:
    """Return the goal a bare command acts on.

    The current goal is the conversation's most recently touched goal, in any
    phase.  A finished goal is still the current goal until it is replaced or
    cleared, so a person can read why it stopped before recording a new one.

    Args:
        goals: The conversation's goals, newest first.

    Returns:
        The current goal, or None when the conversation has none.
    """
    return goals[0] if goals else None


def _unfinished_goal(goals: list[DeepSpaceGoal]) -> DeepSpaceGoal | None:
    """Return the conversation's most recently touched unfinished goal.

    Args:
        goals: The conversation's goals, newest first.

    Returns:
        The unfinished goal, or None when every goal has finished.
    """
    for goal in goals:
        if goal.phase != GoalPhase.COMPLETE:
            return goal
    return None


def _describe(goal: DeepSpaceGoal) -> str:
    """Render one goal for a person.

    Args:
        goal: The goal to describe.

    Returns:
        The sentence the person reads.
    """
    lines = [
        f"{goal.name} — {goal.phase} ({goal.rounds_started}/{goal.max_goal_rounds} rounds, "
        f"revision {goal.revision}, {goal.activation})"
    ]
    if goal.content:
        lines.append(f"  objective: {goal.content}")
    if goal.phase == GoalPhase.BLOCKED and goal.blocked_reason:
        code = str(goal.blocked_reason.get("code") or "blocked")
        message = str(goal.blocked_reason.get("message") or "")
        lines.append(f"  blocker: {code}{f' — {message}' if message else ''}")
    if goal.note:
        lines.append(f"  note: {goal.note}")
    return "\n".join(lines)


def _goal_payload(goal: DeepSpaceGoal) -> dict[str, Any]:
    """Project one goal for the UI.

    Args:
        goal: The goal to project.

    Returns:
        The goal's fields.
    """
    return {
        "goal_id": str(goal.id),
        "name": goal.name,
        "content": goal.content,
        "phase": goal.phase,
        "revision": goal.revision,
        "rounds_started": goal.rounds_started,
        "max_goal_rounds": goal.max_goal_rounds,
        "blocked_reason": goal.blocked_reason,
        "note": goal.note,
        "activation": goal.activation,
    }


def _next_commands(goal: DeepSpaceGoal | None) -> list[str]:
    """Return the commands that apply to the current goal.

    Args:
        goal: The current goal, when the conversation has one.

    Returns:
        The command names a person can use next.
    """
    if goal is None:
        return ["/goal <objective>", "/goal list"]
    if goal.phase == GoalPhase.ACTIVE:
        if goal.activation == GoalActivation.ARMED:
            return ["/goal edit <objective>", "/goal pause", "/goal complete", "/goal clear"]
        return ["/goal edit <objective>", "/goal resume", "/goal complete", "/goal clear"]
    if goal.phase == GoalPhase.PAUSED:
        return ["/goal resume", "/goal edit <objective>", "/goal clear"]
    if goal.phase == GoalPhase.BLOCKED:
        return ["/goal resume", "/goal edit <objective>", "/goal clear"]
    return ["/goal <objective>", "/goal list"]


def _status(goals: list[DeepSpaceGoal]) -> GoalCommandOutcome:
    """Report the conversation's current goal.

    Args:
        goals: The conversation's goals.

    Returns:
        What the person reads.
    """
    current = _current_goal(goals)
    if current is not None:
        return GoalCommandOutcome(
            success=True,
            message=_describe(current),
            data={
                "action": "goal_status",
                "goal": _goal_payload(current),
                "next_commands": _next_commands(current),
            },
        )
    if not goals:
        return GoalCommandOutcome(
            success=True,
            message=(
                "This conversation has no goal. Use /goal <objective> to record one, "
                "or /goal create <objective>."
            ),
            data={"action": "goal_status", "goal": None, "next_commands": _next_commands(None)},
        )
    return GoalCommandOutcome(
        success=True,
        message="\n".join(_describe(goal) for goal in goals),
        data={
            "action": "goal_status",
            "goal": None,
            "goals": [_goal_payload(goal) for goal in goals],
            "next_commands": _next_commands(None),
        },
    )


def execute_goal_command(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    raw_input: str,
    goal_name: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> GoalCommandOutcome:
    """Carry out one ``/goal`` command.

    Args:
        db: The request's database session.
        tenant_id: The tenant that owns the conversation.
        user_id: The user that owns the conversation.
        conversation_id: The conversation the goals belong to.
        raw_input: The text after the ``/goal`` command name.
        goal_name: The goal a control word acts on.  Without it a control
            word acts on the conversation's current goal.  The slash
            command never names one, so the command grammar stays the same
            everywhere; an interface that shows several goals names the one
            the person chose.
        attachments: Optional list of attachment dicts (from the composer)
            to submit alongside the goal objective.  Only used for ``create``
            and ``edit``.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When the command cannot carry out its request.
    """
    word, argument = parse_goal_command(raw_input)
    goals = _goals(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    named = str(goal_name or "").strip()

    if word == "list":
        if not goals:
            return GoalCommandOutcome(
                success=True,
                message="This conversation has no goals.",
                data={"action": "goal_list", "goals": []},
            )
        return GoalCommandOutcome(
            success=True,
            message="\n".join(_describe(goal) for goal in goals),
            data={
                "action": "goal_list",
                "goals": [_goal_payload(goal) for goal in goals],
            },
        )

    if word == "create":
        return _create(
            db,
            goals=goals,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            objective=argument,
            attachments=attachments,
        )

    if word == "edit":
        return _edit(db, goals=goals, objective=argument, goal_name=named, attachments=attachments)

    if word == "pause":
        return _pause(db, goals=goals, goal_name=named)

    if word == "resume":
        return _resume(db, goals=goals, goal_name=named)

    if word == "complete":
        return _complete(db, goals=goals, argument=argument, goal_name=named)

    if word == "block":
        return _block(db, goals=goals, argument=argument, goal_name=named)

    if word == "clear":
        return _clear(db, goals=goals, goal_name=named)

    if word == "status":
        return _status(goals)

    current = _current_goal(goals)
    if current is not None:
        return GoalCommandOutcome(
            success=True,
            message=_describe(current),
            data={
                "action": "goal_status",
                "goal": _goal_payload(current),
                "next_commands": _next_commands(current),
            },
        )
    if not goals:
        return GoalCommandOutcome(
            success=True,
            message=(
                "This conversation has no goal. Use /goal <objective> to record one, "
                "or /goal create <objective>."
            ),
            data={"action": "goal_status", "goal": None, "next_commands": _next_commands(None)},
        )
    return GoalCommandOutcome(
        success=True,
        message="\n".join(_describe(goal) for goal in goals),
        data={
            "action": "goal_status",
            "goal": None,
            "goals": [_goal_payload(goal) for goal in goals],
            "next_commands": _next_commands(None),
        },
    )


def _create(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    objective: str,
    attachments: list[dict[str, Any]] | None = None,
) -> GoalCommandOutcome:
    """Create a goal, or replace a finished one with a fresh identity.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        tenant_id: The tenant that owns the conversation.
        user_id: The user that owns the conversation.
        conversation_id: The conversation the goal belongs to.
        objective: The goal's statement.
        attachments: Optional list of attachment dicts to submit as a
            user message alongside the goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When the objective is empty or a goal is unfinished.
    """
    text = str(objective or "").strip()
    if not text:
        raise GoalCommandError(
            "A goal needs an objective. Use /goal <objective>.",
            code="GOAL_COMMAND_EMPTY_OBJECTIVE",
        )
    if len(text) > MAX_GOAL_CHARS:
        raise GoalCommandError(
            f"A goal's objective may carry at most {MAX_GOAL_CHARS} characters.",
            code="GOAL_COMMAND_OBJECTIVE_TOO_LONG",
        )
    unfinished = _unfinished_goal(goals)
    if unfinished is not None:
        raise GoalCommandError(
            f"The goal '{unfinished.name}' is still {unfinished.phase}. "
            "Edit, pause, complete, block, or clear it before recording another.",
            code="GOAL_COMMAND_UNFINISHED_GOAL",
        )
    name = text[:120]
    existing = next((goal for goal in goals if goal.name == name), None)
    if existing is not None:
        # A finished goal with the same name is replaced with a fresh
        # identity, so the new objective does not inherit the old counters.
        db.delete(existing)
        db.flush()
    goal = DeepSpaceGoal(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        name=name,
        content=text,
        phase=GoalPhase.ACTIVE,
        activation=GoalActivation.ARMED,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)

    # Submit attachments as a user message for the next goal round to read
    if attachments:
        _submit_attachments(db, tenant_id, user_id, conversation_id, attachments)

    return GoalCommandOutcome(
        success=True,
        message=f"Goal recorded: {goal.name}",
        data={"action": "goal_create", "goal": _goal_payload(goal)},
    )


def _require_current(goals: list[DeepSpaceGoal]) -> DeepSpaceGoal:
    """Return the goal a command acts on.

    Args:
        goals: The conversation's current goals.

    Returns:
        The current goal.

    Raises:
        GoalCommandError: When the conversation has no current goal.
    """
    current = _current_goal(goals)
    if current is None:
        raise GoalCommandError(
            "This conversation has no current goal.",
            code="GOAL_COMMAND_NO_CURRENT_GOAL",
        )
    return current


def _edit(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    objective: str,
    goal_name: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> GoalCommandOutcome:
    """Change a goal's objective without changing its phase.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        objective: The replacement objective.
        goal_name: The goal to edit, empty for the current goal.
        attachments: Optional list of attachment dicts to submit as a
            user message alongside the goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When the objective is empty or the goal is finished.
    """
    text = str(objective or "").strip()
    if not text:
        raise GoalCommandError(
            "An edit needs a replacement objective. Use /goal edit <objective>.",
            code="GOAL_COMMAND_EMPTY_OBJECTIVE",
        )
    if len(text) > MAX_GOAL_CHARS:
        raise GoalCommandError(
            f"A goal's objective may carry at most {MAX_GOAL_CHARS} characters.",
            code="GOAL_COMMAND_OBJECTIVE_TOO_LONG",
        )
    goal = _select_goal(goals, goal_name)
    if goal.phase == GoalPhase.COMPLETE:
        # A finished goal cannot be edited in place.  The replacement keeps
        # the fresh identity a new objective deserves.
        return _create(
            db,
            goals=goals,
            tenant_id=goal.tenant_id,
            user_id=goal.user_id,
            conversation_id=goal.conversation_id,
            objective=text,
            attachments=attachments,
        )
    goal.content = text
    goal.revision = (goal.revision or 0) + 1
    db.commit()
    db.refresh(goal)

    # Submit attachments as a user message for the next goal round to read
    if attachments:
        _submit_attachments(db, goal.tenant_id, goal.user_id, goal.conversation_id, attachments)

    return GoalCommandOutcome(
        success=True,
        message=f"Goal updated: {goal.name}",
        data={"action": "goal_edit", "goal": _goal_payload(goal)},
    )


def _pause(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    goal_name: str = "",
) -> GoalCommandOutcome:
    """Pause a goal and stop its automatic continuation.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        goal_name: The goal to pause, empty for the current goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When there is no current goal, or it is not active.
    """
    goal = _select_goal(goals, goal_name)
    if goal.phase != GoalPhase.ACTIVE:
        raise GoalCommandError(
            f"The goal '{goal.name}' is {goal.phase}, so it cannot be paused.",
            code="GOAL_COMMAND_NOT_ACTIVE",
        )
    goal.phase = GoalPhase.PAUSED
    goal.status = GoalPhase.PAUSED
    goal.activation = GoalActivation.DISARMED
    goal.revision = (goal.revision or 0) + 1
    db.commit()
    db.refresh(goal)
    return GoalCommandOutcome(
        success=True,
        message=f"Goal paused: {goal.name}",
        data={"action": "goal_pause", "goal": _goal_payload(goal)},
    )


def _resume(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    goal_name: str = "",
) -> GoalCommandOutcome:
    """Resume a stopped goal, or arm an active one.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        goal_name: The goal to resume, empty for the current goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When there is no current goal, it is finished, or it
            has spent its round allowance.
    """
    goal = _select_goal(goals, goal_name)
    if goal.phase == GoalPhase.COMPLETE:
        raise GoalCommandError(
            f"The goal '{goal.name}' is complete. Record a new goal instead.",
            code="GOAL_COMMAND_GOAL_FINISHED",
        )
    if goal.rounds_started >= goal.max_goal_rounds:
        raise GoalCommandError(
            f"The goal '{goal.name}' has used its {goal.max_goal_rounds}-round allowance. "
            "Edit its round cap before resuming it.",
            code="GOAL_COMMAND_ROUND_CAP_SPENT",
        )
    goal.phase = GoalPhase.ACTIVE
    goal.status = GoalPhase.ACTIVE
    goal.activation = GoalActivation.ARMED
    goal.blocked_reason = None
    goal.revision = (goal.revision or 0) + 1
    db.commit()
    db.refresh(goal)
    return GoalCommandOutcome(
        success=True,
        message=f"Goal resumed: {goal.name}",
        data={"action": "goal_resume", "goal": _goal_payload(goal)},
    )


def _complete(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    argument: str,
    goal_name: str = "",
) -> GoalCommandOutcome:
    """Complete a goal.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        argument: The name of the goal, when the person named one.
        goal_name: The goal to complete, empty for the current goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When the named goal is unknown or already finished.
    """
    goal = _select_goal(goals, argument or goal_name)
    if goal.phase == GoalPhase.COMPLETE:
        raise GoalCommandError(
            f"The goal '{goal.name}' is already complete.",
            code="GOAL_COMMAND_GOAL_FINISHED",
        )
    goal.phase = GoalPhase.COMPLETE
    goal.status = "reached"
    goal.activation = GoalActivation.DISARMED
    goal.blocked_reason = None
    goal.revision = (goal.revision or 0) + 1
    db.commit()
    db.refresh(goal)
    return GoalCommandOutcome(
        success=True,
        message=f"Goal completed: {goal.name}",
        data={"action": "goal_complete", "goal": _goal_payload(goal)},
    )


def _block(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    argument: str,
    goal_name: str = "",
) -> GoalCommandOutcome:
    """Block a goal with a human-requested blocker.

    A person stops a goal immediately.  The model's blocked threshold does
    not apply to a person's request.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        argument: The reason the goal is blocked.
        goal_name: The goal to block, empty for the current goal.

    Returns:
        What the command did.

    Raises:
        GoalCommandError: When the current goal is already finished.
    """
    goal = _select_goal(goals, goal_name)
    if goal.phase == GoalPhase.COMPLETE:
        raise GoalCommandError(
            f"The goal '{goal.name}' is complete. Record a new goal instead.",
            code="GOAL_COMMAND_GOAL_FINISHED",
        )
    goal.phase = GoalPhase.BLOCKED
    goal.status = "abandoned"
    goal.activation = GoalActivation.DISARMED
    goal.blocked_reason = {
        "code": HUMAN_BLOCKED_CODE,
        "message": str(argument or "").strip() or "A person blocked this goal.",
    }
    goal.revision = (goal.revision or 0) + 1
    db.commit()
    db.refresh(goal)
    return GoalCommandOutcome(
        success=True,
        message=f"Goal blocked: {goal.name}",
        data={"action": "goal_block", "goal": _goal_payload(goal)},
    )


def _clear(
    db: Session,
    *,
    goals: list[DeepSpaceGoal],
    goal_name: str = "",
) -> GoalCommandOutcome:
    """Remove the current goal and keep its durable history.

    Args:
        db: The request's database session.
        goals: The conversation's current goals.
        goal_name: The goal to clear, empty for the current goal.

    Returns:
        What the command did.
    """
    goal = _select_goal(goals, goal_name) if goal_name else _current_goal(goals)
    if goal is None:
        return GoalCommandOutcome(
            success=True,
            message="There is no goal to clear.",
            data={"action": "goal_clear", "goal_id": None},
        )
    db.delete(goal)
    db.commit()
    return GoalCommandOutcome(
        success=True,
        message=f"Goal cleared: {goal.name}",
        data={"action": "goal_clear", "goal_id": str(goal.id)},
    )


def _select_goal(goals: list[DeepSpaceGoal], name: str) -> DeepSpaceGoal:
    """Return the goal a person named, or the current goal.

    Args:
        goals: The conversation's current goals.
        name: The name the person gave, empty when they named none.

    Returns:
        The goal the command acts on.

    Raises:
        GoalCommandError: When the named goal is unknown.
    """
    if not name:
        return _require_current(goals)
    for goal in goals:
        if goal.name == name:
            return goal
    raise GoalCommandError(
        f"There is no goal named '{name}' in this conversation.",
        code="GOAL_COMMAND_UNKNOWN_GOAL",
    )


__all__ = [
    "CONTROL_WORDS",
    "HUMAN_BLOCKED_CODE",
    "GoalCommandError",
    "GoalCommandOutcome",
    "execute_goal_command",
    "parse_goal_command",
]
