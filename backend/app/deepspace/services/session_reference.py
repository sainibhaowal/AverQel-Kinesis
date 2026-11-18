"""Session reference service for DeepSpace.

Provides cross-session snapshot references and durable untrusted model context.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import SessionEventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionSnapshotRef:
    """A reference to a session snapshot."""

    conversation_id: uuid.UUID
    turn: int
    summary: str
    token_count: int
    created_at: datetime
    event_types: list[str]
    key_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionReference:
    """A cross-session reference."""

    reference_id: str
    source_conversation_id: uuid.UUID
    target_conversation_id: uuid.UUID
    snapshot: SessionSnapshotRef
    created_at: datetime
    access_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionReferenceService:
    """Service for managing cross-session references."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.event_store = SessionEventStore(db)

    def create_snapshot_ref(
        self,
        conversation_id: uuid.UUID,
        turn: int,
        max_tokens: int = 4000,
    ) -> SessionSnapshotRef | None:
        """Create a snapshot reference for a conversation at a specific turn."""
        # Get events up to this turn
        events = self.event_store.events(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            conversation_id=conversation_id,
            limit=2000,
        )

        # Filter events up to the turn
        filtered_events = [e for e in events if (e.turn or 0) <= turn]
        if not filtered_events:
            return None

        # Build snapshot summary
        event_types = list({e.event_type for e in filtered_events})
        decisions = []
        questions = []

        for event in filtered_events:
            payload = event.payload or {}
            if event.event_type == "plan/reviewed" and payload.get("decision") == "approved":
                decisions.append(payload.get("plan_name", "Unknown plan"))
            elif event.event_type == "feedback/given":
                feedback = payload.get("feedback", "")
                if feedback:
                    questions.append(f"Feedback: {feedback[:100]}")

        # Build summary text
        summary_parts = [
            f"Conversation {str(conversation_id)[:8]} - Turn {turn}",
            f"Events: {len(filtered_events)}",
            f"Types: {', '.join(sorted(event_types))}",
        ]
        if decisions:
            summary_parts.append(f"Decisions: {', '.join(decisions)}")
        if questions:
            summary_parts.append(f"Open questions: {len(questions)}")

        summary = "\n".join(summary_parts)

        return SessionSnapshotRef(
            conversation_id=conversation_id,
            turn=turn,
            summary=summary,
            token_count=len(summary.split()) * 1.3,  # rough estimate
            created_at=datetime.now(UTC),
            event_types=event_types,
            key_decisions=decisions,
            open_questions=questions,
        )

    def create_cross_session_reference(
        self,
        source_conversation_id: uuid.UUID,
        target_conversation_id: uuid.UUID,
        source_turn: int,
        metadata: dict[str, Any] | None = None,
    ) -> SessionReference:
        """Create a cross-session reference."""
        snapshot = self.create_snapshot_ref(source_conversation_id, source_turn)
        if not snapshot:
            raise ValueError("Failed to create snapshot for source conversation")

        reference = SessionReference(
            reference_id=str(uuid.uuid4()),
            source_conversation_id=source_conversation_id,
            target_conversation_id=target_conversation_id,
            snapshot=snapshot,
            created_at=datetime.now(UTC),
            metadata=metadata or {},
        )

        # Store in session events for durability
        self.event_store.append(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            conversation_id=target_conversation_id,
            run_id=None,
            event_type="session/reference",
            payload={
                "reference_id": reference.reference_id,
                "source_conversation_id": str(source_conversation_id),
                "target_conversation_id": str(target_conversation_id),
                "source_turn": snapshot.turn,
                "snapshot_summary": snapshot.summary,
                "metadata": metadata or {},
            },
        )

        return reference

    def get_references_for_conversation(
        self,
        conversation_id: uuid.UUID,
    ) -> list[SessionReference]:
        """Get all references for a conversation."""
        events = self.event_store.events(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            conversation_id=conversation_id,
            limit=500,
        )

        references = []
        for event in events:
            if event.event_type == "session/reference":
                payload = event.payload or {}
                try:
                    snapshot = SessionSnapshotRef(
                        conversation_id=uuid.UUID(payload.get("source_conversation_id", "")),
                        turn=payload.get("source_turn", 0),
                        summary=payload.get("snapshot_summary", ""),
                        token_count=len(payload.get("snapshot_summary", "").split()) * 1.3,
                        created_at=event.created_at,
                        event_types=[],
                    )
                    ref = SessionReference(
                        reference_id=payload.get("reference_id", ""),
                        source_conversation_id=uuid.UUID(payload.get("source_conversation_id", "")),
                        target_conversation_id=uuid.UUID(payload.get("target_conversation_id", "")),
                        snapshot=snapshot,
                        created_at=event.created_at,
                        metadata=payload.get("metadata", {}),
                    )
                    references.append(ref)
                except Exception:
                    continue

        return references

    def resolve_reference(self, reference_id: str) -> SessionReference | None:
        """Resolve a reference by ID across all conversations."""
        # Search across all conversations (this would be more efficient with a dedicated table)
        # For now, we query the event store with a filter
        query = select(DeepSpaceSessionEvent).where(
            DeepSpaceSessionEvent.event_type == "session/reference",
            DeepSpaceSessionEvent.payload.contains({"reference_id": reference_id}),
        )
        result = self.db.execute(query).scalar_one_or_none()
        if not result:
            return None

        payload = result.payload or {}
        try:
            snapshot = SessionSnapshotRef(
                conversation_id=uuid.UUID(payload.get("source_conversation_id", "")),
                turn=payload.get("source_turn", 0),
                summary=payload.get("snapshot_summary", ""),
                token_count=len(payload.get("snapshot_summary", "").split()) * 1.3,
                created_at=result.created_at,
                event_types=[],
            )
            return SessionReference(
                reference_id=payload.get("reference_id", ""),
                source_conversation_id=uuid.UUID(payload.get("source_conversation_id", "")),
                target_conversation_id=uuid.UUID(payload.get("target_conversation_id", "")),
                snapshot=snapshot,
                created_at=result.created_at,
                metadata=payload.get("metadata", {}),
            )
        except Exception:
            return None

    def build_context_injection(self, reference: SessionReference) -> str:
        """Build context injection text for a reference."""
        snap = reference.snapshot
        return (
            f"Cross-session reference from conversation {str(reference.source_conversation_id)[:8]} "
            f"(turn {snap.turn}):\n{snap.summary}"
        )


def create_session_reference_service(db: Session) -> SessionReferenceService:
    """Create a session reference service."""
    return SessionReferenceService(db)


__all__ = [
    "SessionSnapshotRef",
    "SessionReference",
    "SessionReferenceService",
    "create_session_reference_service",
]
