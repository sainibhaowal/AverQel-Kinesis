"""Conversation fork API and service for DeepSpace.

Allows creating a divergent copy of a conversation from any turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import Settings, get_settings
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.services.session_events import SessionEventStore

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConversationForkConfig:
    """Conversation fork configuration."""

    enabled: bool = True
    max_fork_depth: int = 10
    copy_messages: bool = True
    copy_events: bool = True
    copy_files: bool = True
    fork_suffix: str = " (fork)"


# ──────────────────────────────────────────────────────────────────────
# Fork Service
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ForkResult:
    """Result of a fork operation."""

    conversation_id: UUID
    forked_from: UUID
    forked_from_turn: int
    forked_from_message_id: UUID | None
    title: str
    created_at: datetime


class ConversationForkService:
    """Service for forking conversations."""

    def __init__(self, config: ConversationForkConfig, settings: Settings):
        self.config = config
        self.settings = settings

    async def fork(
        self,
        db: Session,
        *,
        auth: AuthContext,
        conversation_id: UUID,
        turn: int | None = None,
        message_id: UUID | None = None,
        title: str | None = None,
        copy_files: bool | None = None,
    ) -> ForkResult:
        """Fork a conversation at a specific turn or message."""
        if not self.config.enabled:
            raise ValueError("Conversation forks are disabled for this deployment.")
        repo = DeepSpaceChatRepository(db)
        source = repo.get_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        if not source:
            raise ValueError("Source conversation not found")
        source_depth = self._fork_depth(
            db, tenant_id=auth.tenant_id, user_id=auth.user_id, conversation_id=conversation_id
        )
        if source_depth >= self.config.max_fork_depth:
            raise ValueError(
                "The conversation has reached the maximum fork depth of "
                f"{self.config.max_fork_depth}."
            )
        # A request may turn file copying off for one fork, but it may not
        # override a deployment that has disabled it globally.
        copy_library_files = self.config.copy_files and (
            self.config.copy_files if copy_files is None else copy_files
        )
        new_conversation = repo.fork_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            source_conversation_id=conversation_id,
            title=title or f"{source.title or 'Conversation'}{self.config.fork_suffix}",
            copy_files=copy_library_files,
            copy_messages=self.config.copy_messages,
            copy_events=self.config.copy_events,
            fork_turn=turn,
            fork_message_id=message_id,
        )
        if new_conversation is None:
            raise ValueError("Source conversation not found")
        SessionEventStore(db).append(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=new_conversation.id,
            event_type="conversation/forked",
            payload={
                "source_conversation_id": str(conversation_id),
                "fork_depth": source_depth + 1,
                "forked_from_turn": turn,
                "forked_from_message_id": str(message_id) if message_id else None,
            },
        )
        db.commit()

        return ForkResult(
            conversation_id=new_conversation.id,
            forked_from=conversation_id,
            forked_from_turn=turn if turn is not None else source_depth,
            forked_from_message_id=message_id,
            title=new_conversation.title,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _fork_depth(
        db: Session,
        *,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
    ) -> int:
        """Read the durable fork lineage marker from a conversation."""
        events = db.execute(
            select(DeepSpaceSessionEvent.payload).where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
                DeepSpaceSessionEvent.event_type == "conversation/forked",
            )
        ).all()
        depths = [
            int(payload.get("fork_depth", 0))
            for (payload,) in events
            if isinstance(payload, dict) and str(payload.get("fork_depth", "")).isdigit()
        ]
        return max(depths, default=0)


# ──────────────────────────────────────────────────────────────────────
# Service Factory
# ──────────────────────────────────────────────────────────────────────

_FORK_SERVICE: ConversationForkService | None = None


def get_conversation_fork_service(settings: Settings | None = None) -> ConversationForkService:
    """Get or create the global conversation fork service."""
    global _FORK_SERVICE
    if _FORK_SERVICE is None:
        cfg = ConversationForkConfig(
            enabled=getattr(settings or get_settings(), "deepspace_fork_enabled", True),
            max_fork_depth=getattr(settings or get_settings(), "deepspace_fork_max_depth", 10),
            copy_messages=getattr(settings or get_settings(), "deepspace_fork_copy_messages", True),
            copy_events=getattr(settings or get_settings(), "deepspace_fork_copy_events", True),
            copy_files=getattr(settings or get_settings(), "deepspace_fork_copy_files", True),
            fork_suffix=getattr(settings or get_settings(), "deepspace_fork_suffix", " (fork)"),
        )
        _FORK_SERVICE = ConversationForkService(cfg, settings or get_settings())
    return _FORK_SERVICE


def init_conversation_fork_service(settings: Settings) -> ConversationForkService:
    """Initialize the conversation fork service."""
    global _FORK_SERVICE
    cfg = ConversationForkConfig(
        enabled=getattr(settings, "deepspace_fork_enabled", True),
        max_fork_depth=getattr(settings, "deepspace_fork_max_depth", 10),
        copy_messages=getattr(settings, "deepspace_fork_copy_messages", True),
        copy_events=getattr(settings, "deepspace_fork_copy_events", True),
        copy_files=getattr(settings, "deepspace_fork_copy_files", True),
        fork_suffix=getattr(settings, "deepspace_fork_suffix", " (fork)"),
    )
    _FORK_SERVICE = ConversationForkService(cfg, settings)
    return _FORK_SERVICE


__all__ = [
    "ConversationForkConfig",
    "ForkResult",
    "ConversationForkService",
    "get_conversation_fork_service",
    "init_conversation_fork_service",
]
