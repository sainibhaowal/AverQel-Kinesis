"""The plan-mode state of one DeepSpace conversation.

Plan mode has two parts.  The mode itself is a per-conversation setting that
says the agent should design before it acts.  The plan is the design the
agent produced, and the review is the user's decision about it.

Both parts are durable.  The mode is read from the conversation's current
plan, and the plan and its review are stored rows, so the state survives a
new turn, a compaction, and a restart.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.deepspace.models.plan import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_PROPOSED,
    PLAN_STATUS_REJECTED,
    PLAN_STATUS_REVISION_REQUESTED,
)
from app.deepspace.services.plan_store import (
    MAX_FEEDBACK_CHARS,
    DeepSpacePlanStore,
    PlanRecord,
    PlanStoreError,
)
from app.deepspace.services.session_events import (
    PLAN_MODE,
    PLAN_REVIEWED,
    PLAN_SUBMITTED,
    SessionEventStore,
)

#: The guidance the agent receives while a conversation is in plan mode.
PLAN_MODE_INSTRUCTION = """Plan mode is active for this turn.
- Design the work before performing it. Read, research, and inspect as much as you need, but do not change anything.
- Do not start the work, do not create a task plan, and do not perform the action the request describes.
- When the design is complete, call plan_submit with a Markdown plan that starts with a heading, lists the steps in order, and states what each step does and how it will be verified.
- After plan_submit, the plan goes to the user for review. Wait for their decision; do not continue working.
- If the user asks for changes, read the current plan with plan_read, revise it, and submit the revision.
""".strip()

#: The instruction the agent receives after the user approved a plan.
PLAN_APPROVED_INSTRUCTION = (
    "The user approved your plan. Carry the approved plan out now, and report what you did."
)

#: The instruction the agent receives after the user asked for changes.
PLAN_REVISION_INSTRUCTION = (
    "The user asked for changes to your plan. Read the current plan with plan_read, "
    "revise it, and submit the revision with plan_submit."
)


class PlanModeService:
    """The plan mode and the plan under review for one conversation."""

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.store = DeepSpacePlanStore(db)
        self.events = SessionEventStore(db)

    def active(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        """Report whether the conversation is in plan mode.

        The mode is its own durable state: the last mode change the
        conversation recorded decides it.  A conversation that has a plan
        under review but never recorded a mode change is in plan mode too,
        because the agent is waiting for the review.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            True when the agent should design before it acts.
        """
        recorded = self._last_mode_state(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if recorded is not None:
            return recorded
        return self.store.plan_mode_active(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )

    def _last_mode_state(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool | None:
        """Read the last recorded plan-mode state of a conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded state, or None when the conversation never recorded
            one.
        """
        from sqlalchemy import select

        from app.deepspace.models.session_event import DeepSpaceSessionEvent

        events = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == PLAN_MODE,
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
        return bool(payload.get("active"))

    def recorded_mode(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool | None:
        """Return the conversation's explicit plan-mode choice, if any.

        A preset may provide a default only while the conversation has not
        recorded its own choice.  Callers use this distinction to avoid
        turning plan mode back on after the user explicitly disabled it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The explicit state, or None before the first mode event.
        """
        return self._last_mode_state(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )

    def current_plan(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> PlanRecord | None:
        """Return the conversation's current plan.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The current plan, or None when the conversation has none.
        """
        return self.store.current_plan(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )

    def set_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        active: bool,
    ) -> dict[str, Any]:
        """Turn plan mode on or off for one conversation.

        Turning plan mode off rejects a plan that is still under review, so a
        conversation never keeps a plan the user is no longer being asked
        about.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            active: Whether plan mode should be on.

        Returns:
            The new plan-mode state.
        """
        current = self.store.current_plan(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if not active and current is not None:
            self.store.close_open_plan(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLAN_MODE,
            payload={"active": bool(active)},
        )
        self.db.commit()
        current = self.current_plan(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        return {"active": bool(active), "plan": current.as_dict() if current else None}

    def submit(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID | None,
        title: str,
        content: str,
    ) -> PlanRecord:
        """Record a proposed plan and log the proposal.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the plan belongs to.
            run_id: The run that produced the plan.
            title: The plan's title.
            content: The plan's Markdown body.

        Returns:
            The stored plan.

        Raises:
            PlanStoreError: When the title or body is missing or too long.
        """
        plan = self.store.submit_plan(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            content=content,
        )
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            event_type=PLAN_SUBMITTED,
            payload={"plan_id": str(plan.id), "title": plan.title},
        )
        # A submitted plan starts a planning exchange, so the conversation is
        # in plan mode until the user decides.
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLAN_MODE,
            payload={"active": True},
        )
        self.db.commit()
        return plan

    def review(
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
        plan = self.store.review_plan(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            plan_id=plan_id,
            decision=decision,
            feedback=feedback,
        )
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLAN_REVIEWED,
            payload={
                "plan_id": str(plan.id),
                "decision": decision,
                "feedback": plan.feedback,
            },
        )
        # A revision request keeps the conversation in plan mode; an approval
        # or a rejection ends it.
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLAN_MODE,
            payload={"active": decision == PLAN_STATUS_REVISION_REQUESTED},
        )
        self.db.commit()
        return plan

    def revise(
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
        plan = self.store.revise_plan(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            plan_id=plan_id,
            title=title,
            content=content,
        )
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLAN_SUBMITTED,
            payload={"plan_id": str(plan.id), "title": plan.title, "revision": True},
        )
        self.db.commit()
        return plan


__all__ = [
    "MAX_FEEDBACK_CHARS",
    "PLAN_APPROVED_INSTRUCTION",
    "PLAN_MODE_INSTRUCTION",
    "PLAN_REVISION_INSTRUCTION",
    "PLAN_STATUS_APPROVED",
    "PLAN_STATUS_PROPOSED",
    "PLAN_STATUS_REJECTED",
    "PLAN_STATUS_REVISION_REQUESTED",
    "PlanModeService",
    "PlanStoreError",
]
