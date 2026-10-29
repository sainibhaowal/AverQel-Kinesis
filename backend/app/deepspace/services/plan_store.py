"""The plan-mode store for DeepSpace conversations.

Plan mode is a per-conversation mode.  While it is active the agent
explores and designs instead of acting, and it presents its finished design
through the ``plan_submit`` tool for the user to approve or revise.  The
mode survives a new turn and a restart, because both the mode and the plan
are durable rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.plan import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_PROPOSED,
    PLAN_STATUS_REJECTED,
    PLAN_STATUS_REVISION_REQUESTED,
    PLAN_STATUSES,
    DeepSpacePlan,
)

#: The largest number of characters a plan body may carry.
MAX_PLAN_CHARS = 40_000

#: The largest number of characters a plan title may carry.
MAX_PLAN_TITLE_CHARS = 255

#: The largest number of characters the user's feedback may carry.
MAX_FEEDBACK_CHARS = 4_000


class PlanStoreError(ValueError):
    """A plan operation was rejected."""


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """One plan and its review state."""

    id: uuid.UUID
    title: str
    content: str
    status: str
    feedback: str | None
    revision_count: int
    created_at: datetime | None
    updated_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this plan."""
        return {
            "id": str(self.id),
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "feedback": self.feedback,
            "revision_count": self.revision_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _serialize(plan: DeepSpacePlan) -> PlanRecord:
    """Project one plan row."""
    return PlanRecord(
        id=plan.id,
        title=plan.title,
        content=plan.content,
        status=plan.status,
        feedback=plan.feedback,
        revision_count=int(plan.revision_count or 0),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


class DeepSpacePlanStore:
    """The durable plan and plan-mode state of one conversation."""

    def __init__(self, db: Session) -> None:
        """Create the store.

        Args:
            db: The database session.
        """
        self.db = db

    def _plans(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[DeepSpacePlan]:
        return list(
            self.db.execute(
                select(DeepSpacePlan)
                .where(
                    DeepSpacePlan.tenant_id == tenant_id,
                    DeepSpacePlan.user_id == user_id,
                    DeepSpacePlan.conversation_id == conversation_id,
                )
                .order_by(DeepSpacePlan.created_at.asc())
            )
            .scalars()
            .all()
        )

    def current_plan(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> PlanRecord | None:
        """Return the conversation's most recent plan.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The most recent plan, or None when the conversation has none.
        """
        plans = self._plans(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return _serialize(plans[-1]) if plans else None

    def plan_mode_active(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        """Report whether the conversation is in plan mode.

        Plan mode is active while the newest plan still needs the user's
        attention: the agent has proposed a design and the user has not
        answered, or the user asked for changes and the agent owes a
        revision.  An approved or rejected plan ends the mode.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            True when the conversation is in plan mode.
        """
        current = self.current_plan(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        return current is not None and current.status in {
            PLAN_STATUS_PROPOSED,
            PLAN_STATUS_REVISION_REQUESTED,
        }

    def submit_plan(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
        content: str,
    ) -> PlanRecord:
        """Record a proposed plan for the user to review.

        A new proposal supersedes the previous one, so a conversation holds
        at most one plan under review.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the plan belongs to.
            title: The plan's title.
            content: The plan's Markdown body.

        Returns:
            The stored plan.

        Raises:
            PlanStoreError: When the title or body is missing or too long.
        """
        normalized_title = str(title or "").strip()
        normalized_content = str(content or "").strip()
        if not normalized_title:
            raise PlanStoreError("A plan needs a title.")
        if len(normalized_title) > MAX_PLAN_TITLE_CHARS:
            raise PlanStoreError("The plan title is too long.")
        if not normalized_content:
            raise PlanStoreError("A plan needs a body.")
        if len(normalized_content) > MAX_PLAN_CHARS:
            raise PlanStoreError("The plan body is too long.")
        existing = self._plans(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        plan = DeepSpacePlan(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            title=normalized_title,
            content=normalized_content,
            status=PLAN_STATUS_PROPOSED,
        )
        for previous in existing:
            if previous.status in {PLAN_STATUS_PROPOSED, PLAN_STATUS_REVISION_REQUESTED}:
                previous.status = PLAN_STATUS_REJECTED
                previous.feedback = "Superseded by a newer plan."
        self.db.add(plan)
        self.db.commit()
        return _serialize(plan)

    def review_plan(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        plan_id: uuid.UUID,
        decision: str,
        feedback: str | None = None,
    ) -> PlanRecord:
        """Record the user's decision about a proposed plan.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the plan belongs to.
            plan_id: The plan under review.
            decision: ``approved``, ``revision_requested``, or ``rejected``.
            feedback: The user's feedback, when they asked for changes.

        Returns:
            The reviewed plan.

        Raises:
            PlanStoreError: When the plan is unknown to this conversation or
                the decision is not one the product accepts.
        """
        if decision not in {
            PLAN_STATUS_APPROVED,
            PLAN_STATUS_REVISION_REQUESTED,
            PLAN_STATUS_REJECTED,
        }:
            raise PlanStoreError("A plan review must approve it, request changes, or reject it.")
        plan = self.db.execute(
            select(DeepSpacePlan).where(
                DeepSpacePlan.id == plan_id,
                DeepSpacePlan.tenant_id == tenant_id,
                DeepSpacePlan.user_id == user_id,
                DeepSpacePlan.conversation_id == conversation_id,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise PlanStoreError("That plan does not belong to this conversation.")
        if plan.status != PLAN_STATUS_PROPOSED:
            raise PlanStoreError("That plan has already been reviewed.")
        plan.status = decision
        if feedback and feedback.strip():
            plan.feedback = feedback.strip()[:MAX_FEEDBACK_CHARS]
        if decision == PLAN_STATUS_REVISION_REQUESTED:
            plan.revision_count = int(plan.revision_count or 0) + 1
        self.db.commit()
        return _serialize(plan)

    def revise_plan(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        plan_id: uuid.UUID,
        title: str,
        content: str,
    ) -> PlanRecord:
        """Replace a plan the user asked to change.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the plan belongs to.
            plan_id: The plan to revise.
            title: The plan's new title.
            content: The plan's new body.

        Returns:
            The revised plan.

        Raises:
            PlanStoreError: When the plan is unknown to this conversation or
                its body is missing.
        """
        normalized_title = str(title or "").strip()
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise PlanStoreError("A plan needs a body.")
        if len(normalized_content) > MAX_PLAN_CHARS:
            raise PlanStoreError("The plan body is too long.")
        plan = self.db.execute(
            select(DeepSpacePlan).where(
                DeepSpacePlan.id == plan_id,
                DeepSpacePlan.tenant_id == tenant_id,
                DeepSpacePlan.user_id == user_id,
                DeepSpacePlan.conversation_id == conversation_id,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise PlanStoreError("That plan does not belong to this conversation.")
        plan.title = normalized_title[:MAX_PLAN_TITLE_CHARS] or plan.title
        plan.content = normalized_content
        plan.status = PLAN_STATUS_PROPOSED
        self.db.commit()
        return _serialize(plan)

    def close_open_plan(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> PlanRecord | None:
        """Close a plan that is still open for review.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the plan belongs to.

        Returns:
            The closed plan, or None when no plan was open.
        """
        current = self.current_plan(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if current is None or current.status not in {
            PLAN_STATUS_PROPOSED,
            PLAN_STATUS_REVISION_REQUESTED,
        }:
            return None
        plan = self.db.execute(
            select(DeepSpacePlan).where(
                DeepSpacePlan.id == current.id,
                DeepSpacePlan.tenant_id == tenant_id,
                DeepSpacePlan.user_id == user_id,
                DeepSpacePlan.conversation_id == conversation_id,
            )
        ).scalar_one_or_none()
        if plan is None:
            return None
        plan.status = PLAN_STATUS_REJECTED
        plan.feedback = "Plan mode was turned off."
        self.db.commit()
        return _serialize(plan)

    def delete_plans(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> int:
        """Remove every plan of one conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to clear.

        Returns:
            The number of plans removed.
        """
        plans = self._plans(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        for plan in plans:
            self.db.delete(plan)
        self.db.commit()
        return len(plans)


__all__ = [
    "MAX_FEEDBACK_CHARS",
    "MAX_PLAN_CHARS",
    "MAX_PLAN_TITLE_CHARS",
    "PLAN_STATUSES",
    "DeepSpacePlanStore",
    "PlanRecord",
    "PlanStoreError",
]
