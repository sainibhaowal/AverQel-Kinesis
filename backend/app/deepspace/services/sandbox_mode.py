"""The sandbox mode of one DeepSpace conversation.

The sandbox mode says how much of the host a shell command may reach.  It
is a per-conversation state rather than a deployment-wide one, so two
conversations of the same user never share a boundary, and a user can
tighten one conversation without changing the deployment.

The state is recorded in the typed session log as a ``sandbox/mode`` event,
so it survives a restart by replay and cannot be changed out of band.  The
effective mode for a call is the per-call grant when there is one, then
the conversation's last recorded mode, then the deployment's default.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import SANDBOX_MODE, SessionEventStore

#: The sandbox modes a conversation may run in.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: The mode that keeps a command from changing anything.
READ_ONLY = "read-only"

#: The mode that lets a command change the workspace and nothing else.
WORKSPACE_WRITE = "workspace-write"

#: The mode that removes the inspection and the cage.
DANGER_FULL_ACCESS = "danger-full-access"


class SandboxModeError(ValueError):
    """A sandbox mode was rejected."""


def validate_mode(mode: str) -> str:
    """Check one sandbox mode.

    Args:
        mode: The mode to check.

    Returns:
        The normalized mode.

    Raises:
        SandboxModeError: When the mode is not one the product accepts.
    """
    normalized = str(mode or "").strip().lower()
    if normalized not in SANDBOX_MODES:
        raise SandboxModeError(
            "A sandbox mode must be read-only, workspace-write, or danger-full-access."
        )
    return normalized


class SandboxModeService:
    """The sandbox mode of one conversation."""

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.events = SessionEventStore(db)

    def recorded_mode(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> str | None:
        """Read the conversation's last recorded sandbox mode.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded mode, or None when the conversation never recorded
            one.
        """
        events = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == SANDBOX_MODE,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        if not events:
            return None
        payload: object = events[0].payload
        if not isinstance(payload, dict):
            return None
        mode = str(payload.get("mode") or "")
        return mode if mode in SANDBOX_MODES else None

    def effective_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        deployment_default: str,
        explicit: str | None = None,
    ) -> str:
        """Resolve the mode one call runs in.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the call belongs to.
            deployment_default: The deployment's configured mode.
            explicit: The mode granted for this call, when there is one.

        Returns:
            The effective mode.
        """
        if explicit:
            return validate_mode(explicit)
        recorded = self.recorded_mode(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if recorded:
            return recorded
        return validate_mode(deployment_default)

    def set_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        mode: str,
    ) -> dict[str, Any]:
        """Record the conversation's sandbox mode.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            mode: The mode to record.

        Returns:
            The recorded state.

        Raises:
            SandboxModeError: When the mode is not one the product accepts.
        """
        normalized = validate_mode(mode)
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=SANDBOX_MODE,
            payload={"mode": normalized},
        )
        self.db.commit()
        return {"mode": normalized}


__all__ = [
    "DANGER_FULL_ACCESS",
    "READ_ONLY",
    "SANDBOX_MODES",
    "WORKSPACE_WRITE",
    "SandboxModeError",
    "SandboxModeService",
    "validate_mode",
]
