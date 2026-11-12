"""Live work that keeps a workspace from being unlinked.

A workspace is a project, not a disposable label.  Removing it while an
agent turn, a queued turn, a delegated child agent, or a terminal session is
still using its directory would strand that work, so the catalogue asks this
module before it deletes a registration.

The checks cover the durable work records as well as the in-process terminal
sessions.  A caller may close terminal sessions and retry; agent work must
finish or be stopped through its normal cancellation path first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.deepspace.models.agent_runtime import DeepSpaceAgentRun
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.runtime_store import ACTIVE_RUN_STATUSES
from app.deepspace.services.terminals import terminal_manager

#: Queued-turn states that represent work in flight.
ACTIVE_QUEUED_TURN_STATUSES = ("queued", "running")

#: Subagent states that represent work in flight.
ACTIVE_SUBAGENT_STATUSES = ("running",)


@dataclass(frozen=True, slots=True)
class WorkspaceActivity:
    """One reason a workspace is still in use.

    Attributes:
        kind: ``agent-run``, ``queued-turn``, ``subagent``, or ``terminal``.
        conversation_id: The conversation that owns the work.
        detail: A short user-facing description.
    """

    #: The kind of work.
    kind: str
    #: The conversation that owns it.
    conversation_id: str
    #: A short description.
    detail: str


def workspace_activities(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_ids: list[uuid.UUID],
    include_terminals: bool = True,
) -> list[WorkspaceActivity]:
    """Return the live work that belongs to a set of conversations.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_ids: The conversations that work in the workspace.
        include_terminals: Whether to inspect live terminal sessions.

    Returns:
        The activities, one entry per kind and conversation.
    """
    if not conversation_ids:
        return []
    identifiers = list(dict.fromkeys(conversation_ids))
    activities: list[WorkspaceActivity] = []

    runs = db.execute(
        select(DeepSpaceAgentRun.conversation_id, DeepSpaceAgentRun.status).where(
            DeepSpaceAgentRun.tenant_id == tenant_id,
            DeepSpaceAgentRun.user_id == user_id,
            DeepSpaceAgentRun.conversation_id.in_(identifiers),
            DeepSpaceAgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    ).all()
    for conversation_id, status in runs:
        activities.append(
            WorkspaceActivity(
                kind="agent-run",
                conversation_id=str(conversation_id),
                detail=f"an agent turn is {status}",
            )
        )

    queued = db.execute(
        select(DeepSpaceQueuedTurn.conversation_id, DeepSpaceQueuedTurn.status).where(
            DeepSpaceQueuedTurn.tenant_id == tenant_id,
            DeepSpaceQueuedTurn.user_id == user_id,
            DeepSpaceQueuedTurn.conversation_id.in_(identifiers),
            DeepSpaceQueuedTurn.status.in_(ACTIVE_QUEUED_TURN_STATUSES),
        )
    ).all()
    for conversation_id, status in queued:
        activities.append(
            WorkspaceActivity(
                kind="queued-turn",
                conversation_id=str(conversation_id),
                detail=f"a queued turn is {status}",
            )
        )

    subagents = db.execute(
        select(DeepSpaceSubagentRun.conversation_id).where(
            DeepSpaceSubagentRun.tenant_id == tenant_id,
            DeepSpaceSubagentRun.user_id == user_id,
            DeepSpaceSubagentRun.conversation_id.in_(identifiers),
            or_(
                DeepSpaceSubagentRun.status.in_(ACTIVE_SUBAGENT_STATUSES),
                DeepSpaceSubagentRun.busy.is_(True),
            ),
        )
    ).scalars()
    for conversation_id in subagents:
        activities.append(
            WorkspaceActivity(
                kind="subagent",
                conversation_id=str(conversation_id),
                detail="a delegated agent is running",
            )
        )

    if include_terminals:
        manager = terminal_manager()
        for conversation_id in identifiers:
            sessions = manager.list(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if sessions:
                activities.append(
                    WorkspaceActivity(
                        kind="terminal",
                        conversation_id=str(conversation_id),
                        detail=f"{len(sessions)} terminal session(s) are live",
                    )
                )
    return activities


def activity_summary(activities: list[WorkspaceActivity]) -> str:
    """Return one user-facing sentence describing the blocking work.

    Args:
        activities: The activities returned by :func:`workspace_activities`.

    Returns:
        The sentence.
    """
    kinds = sorted({activity.kind for activity in activities})
    labels = {
        "agent-run": "an agent turn",
        "queued-turn": "a queued turn",
        "subagent": "a delegated agent",
        "terminal": "a terminal session",
    }
    described = ", ".join(labels.get(kind, kind) for kind in kinds)
    return (
        f"This workspace is still in use: {described}. "
        "Stop that work or close its terminal sessions before removing the workspace."
    )


async def close_workspace_terminals(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_ids: list[uuid.UUID],
) -> int:
    """Close every live terminal session of the given conversations.

    Args:
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_ids: The conversations that work in the workspace.

    Returns:
        The number of sessions closed.
    """
    manager = terminal_manager()
    closed = 0
    for conversation_id in dict.fromkeys(conversation_ids):
        closed += await manager.close_all_for_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    return closed


def activities_as_dict(activities: list[WorkspaceActivity]) -> list[dict[str, Any]]:
    """Return the JSON projection of the activities.

    Args:
        activities: The activities.

    Returns:
        The projection.
    """
    return [
        {
            "kind": activity.kind,
            "conversation_id": activity.conversation_id,
            "detail": activity.detail,
        }
        for activity in activities
    ]


__all__ = [
    "WorkspaceActivity",
    "activities_as_dict",
    "activity_summary",
    "close_workspace_terminals",
    "workspace_activities",
]
