"""Session-level feedback and log-backed feedback events for DeepSpace.

Allows users to rate sessions and provide feedback that is recorded in the session log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import Settings, get_settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import FEEDBACK_GIVEN, SessionEventStore

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionFeedbackConfig:
    """Session feedback configuration."""

    enabled: bool = True
    require_comment_on_negative: bool = True
    max_comment_length: int = 2000
    categories: tuple[str, ...] = (
        "accuracy",
        "helpfulness",
        "clarity",
        "completeness",
        "safety",
        "other",
    )


# ──────────────────────────────────────────────────────────────────────
# Feedback Models
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionFeedback:
    """Session feedback record."""

    id: UUID
    conversation_id: UUID
    tenant_id: UUID
    user_id: UUID
    rating: Literal["positive", "negative", "neutral"]
    categories: tuple[str, ...]
    comment: str | None
    turn: int | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "rating": self.rating,
            "categories": list(self.categories),
            "comment": self.comment,
            "turn": self.turn,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata,
        }


# ──────────────────────────────────────────────────────────────────────
# Feedback Service
# ──────────────────────────────────────────────────────────────────────


class SessionFeedbackService:
    """Service for managing session feedback."""

    def __init__(self, config: SessionFeedbackConfig, settings: Settings):
        self.config = config
        self.settings = settings

    async def submit_feedback(
        self,
        db: Session,
        *,
        auth: AuthContext,
        conversation_id: UUID,
        rating: Literal["positive", "negative", "neutral"],
        categories: list[str] | None = None,
        comment: str | None = None,
        turn: int | None = None,
    ) -> SessionFeedback:
        """Submit feedback for a conversation."""

        # Verify conversation ownership
        conv = db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == auth.tenant_id,
                Conversation.user_id == auth.user_id,
            )
        ).scalar_one_or_none()

        if not conv:
            raise ValueError("Conversation not found")

        # Validate rating
        if rating not in ("positive", "negative", "neutral"):
            raise ValueError("Rating must be positive, negative, or neutral")

        # Validate categories
        cats = tuple(categories or [])
        for cat in cats:
            if cat not in self.config.categories:
                raise ValueError(f"Invalid category: {cat}")

        # Validate comment
        if comment and len(comment) > self.config.max_comment_length:
            raise ValueError(f"Comment too long (max {self.config.max_comment_length} chars)")

        if self.config.require_comment_on_negative and rating == "negative" and not comment:
            raise ValueError("Comment required for negative ratings")

        # Create feedback record
        feedback_id = uuid.uuid4()
        feedback = SessionFeedback(
            id=feedback_id,
            conversation_id=conversation_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            rating=rating,
            categories=cats,
            comment=comment,
            turn=turn,
            created_at=datetime.now(UTC),
            metadata={
                "source": "user",
                "config": {
                    "require_comment_on_negative": self.config.require_comment_on_negative,
                },
            },
        )

        # Record as session event
        store = SessionEventStore(db)
        store.append(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            event_type=FEEDBACK_GIVEN,
            payload=feedback.as_dict(),
            turn=turn,
        )
        db.commit()

        return feedback

    async def get_feedback(
        self,
        db: Session,
        *,
        auth: AuthContext,
        conversation_id: UUID,
    ) -> list[SessionFeedback]:
        """Get all feedback for a conversation."""

        # Verify ownership
        conv = db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == auth.tenant_id,
                Conversation.user_id == auth.user_id,
            )
        ).scalar_one_or_none()

        if not conv:
            raise ValueError("Conversation not found")

        # Query feedback events
        events = (
            db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.tenant_id == auth.tenant_id,
                    DeepSpaceSessionEvent.user_id == auth.user_id,
                    DeepSpaceSessionEvent.event_type == "feedback/given",
                )
                .order_by(DeepSpaceSessionEvent.seq.asc())
            )
            .scalars()
            .all()
        )

        return [
            SessionFeedback(
                id=uuid.UUID(e.payload.get("id", "00000000-0000-0000-0000-000000000000")),
                conversation_id=conversation_id,
                tenant_id=e.tenant_id,
                user_id=e.user_id,
                rating=e.payload.get("rating", "neutral"),
                categories=tuple(e.payload.get("categories", [])),
                comment=e.payload.get("comment"),
                turn=e.turn,
                created_at=e.created_at,
                metadata=e.payload.get("metadata", {}),
            )
            for e in events
        ]

    async def get_aggregate_stats(
        self,
        db: Session,
        *,
        auth: AuthContext,
        tenant_id: UUID | None = None,
        conversation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Get aggregate feedback statistics."""

        tenant = tenant_id or auth.tenant_id

        conditions = [
            DeepSpaceSessionEvent.tenant_id == tenant,
            DeepSpaceSessionEvent.event_type == "feedback/given",
        ]
        if conversation_id is not None:
            conditions.append(DeepSpaceSessionEvent.conversation_id == conversation_id)
        events = db.execute(select(DeepSpaceSessionEvent).where(*conditions)).scalars().all()

        total = len(events)
        positive = sum(1 for e in events if e.payload.get("rating") == "positive")
        negative = sum(1 for e in events if e.payload.get("rating") == "negative")
        neutral = sum(1 for e in events if e.payload.get("rating") == "neutral")

        # Category breakdown
        categories = {}
        for event in events:
            for cat in event.payload.get("categories", []):
                categories[cat] = categories.get(cat, 0) + 1

        return {
            "total_feedback": total,
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "positive_rate": positive / total if total > 0 else 0,
            "categories": categories,
        }


# ──────────────────────────────────────────────────────────────────────
# Service Factory
# ──────────────────────────────────────────────────────────────────────

_FEEDBACK_SERVICE: SessionFeedbackService | None = None


def get_session_feedback_service(settings: Settings | None = None) -> SessionFeedbackService:
    """Get or create the global session feedback service."""
    global _FEEDBACK_SERVICE
    if _FEEDBACK_SERVICE is None:
        cfg = SessionFeedbackConfig(
            enabled=getattr(settings or get_settings(), "deepspace_feedback_enabled", True),
            require_comment_on_negative=getattr(
                settings or get_settings(), "deepspace_feedback_require_comment", True
            ),
            max_comment_length=getattr(
                settings or get_settings(), "deepspace_feedback_max_comment", 2000
            ),
            categories=getattr(
                settings or get_settings(),
                "deepspace_feedback_categories",
                ("accuracy", "helpfulness", "clarity", "completeness", "safety", "other"),
            ),
        )
        _FEEDBACK_SERVICE = SessionFeedbackService(cfg, settings or get_settings())
    return _FEEDBACK_SERVICE


def init_session_feedback_service(settings: Settings) -> SessionFeedbackService:
    """Initialize the session feedback service."""
    global _FEEDBACK_SERVICE
    cfg = SessionFeedbackConfig(
        enabled=getattr(settings, "deepspace_feedback_enabled", True),
        require_comment_on_negative=getattr(settings, "deepspace_feedback_require_comment", True),
        max_comment_length=getattr(settings, "deepspace_feedback_max_comment", 2000),
        categories=getattr(
            settings,
            "deepspace_feedback_categories",
            ("accuracy", "helpfulness", "clarity", "completeness", "safety", "other"),
        ),
    )
    _FEEDBACK_SERVICE = SessionFeedbackService(cfg, settings)
    return _FEEDBACK_SERVICE


__all__ = [
    "SessionFeedbackConfig",
    "SessionFeedback",
    "SessionFeedbackService",
    "get_session_feedback_service",
    "init_session_feedback_service",
]
