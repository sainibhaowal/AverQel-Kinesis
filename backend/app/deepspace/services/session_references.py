"""Session reference resolution for DeepSpace.

Ported from NeoSIS @averqel/neosis-session-reference.
Resolves @session mentions into structured snapshots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.session_query import SessionQueryService

logger = logging.getLogger(__name__)


# Session reference URI scheme
SESSION_REFERENCE_SCHEME = "session"


@dataclass(frozen=True, slots=True)
class SessionReferenceCandidate:
    """A session candidate for @session completion."""

    conversation_id: UUID
    title: str | None
    updated_at: int
    snippet: str


@dataclass(frozen=True, slots=True)
class ReferencedSessionData:
    """Structured snapshot of a referenced session."""

    conversation_id: UUID
    title: str | None
    updated_at: int
    turn_count: int
    message_count: int
    snippet: str
    # The actual session data (truncated to budget)
    turns: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SessionReferenceInput:
    """Input for session reference resolution."""

    conversation_id: UUID
    max_turns: int = 10
    max_chars: int = 50_000


@dataclass(frozen=True, slots=True)
class ReferenceRetentionStats:
    """Stats about referenced session retention."""

    total_bytes: int
    turns_included: int
    turns_omitted: int


class SessionReferenceResolver:
    """Resolves @session mentions into structured snapshots."""

    def __init__(
        self,
        session_query: SessionQueryService,
        settings: Settings,
    ):
        self.session_query = session_query
        self.settings = settings
        self._max_bytes = getattr(settings, "deepspace_session_references_max_bytes", 50_000)
        self._max_refs = getattr(settings, "deepspace_session_references_max_refs", 5)

    async def resolve(
        self,
        mentions: list[str],
        auth: AuthContext,
    ) -> list[ReferencedSessionData]:
        """Resolve a list of session mentions.

        Mentions can be:
        - @session:<conversation_id>
        - @session:"title"
        - @session:<conversation_id>:<turns>
        """
        results = []
        for mention in mentions[: self._max_refs]:
            try:
                data = await self._resolve_one(mention, auth)
                if data:
                    results.append(data)
            except Exception as e:
                logger.warning("Failed to resolve session mention %s: %s", mention, e)
        return results

    async def _resolve_one(
        self,
        mention: str,
        auth: AuthContext,
    ) -> ReferencedSessionData | None:
        """Resolve a single session mention."""
        # Parse mention formats:
        # @session:<uuid>
        # @session:"title"
        # @session:<uuid>:N (limit turns)
        parsed = self._parse_mention(mention)
        if not parsed:
            return None

        conversation_id = parsed["conversation_id"]
        max_turns = parsed.get("max_turns", 10)

        # Fetch session trace
        trace = await self.session_query.get_trace(
            conversation_id=conversation_id,
            auth=auth,
            limit=max_turns,
        )

        if not trace:
            return None

        # Build referenced session data
        turns = []
        total_chars = 0
        for turn in trace.get("turns", []):
            turn_data = self._format_turn(turn)
            turn_chars = len(str(turn_data))
            if total_chars + turn_chars > self._max_bytes:
                break
            turns.append(turn_data)
            total_chars += turn_chars

        return ReferencedSessionData(
            conversation_id=conversation_id,
            title=trace.get("title"),
            updated_at=trace.get("updated_at", 0),
            turn_count=len(trace.get("turns", [])),
            message_count=trace.get("message_count", 0),
            snippet=trace.get("snippet", "")[:200],
            turns=turns,
        )

    def _parse_mention(self, mention: str) -> dict[str, Any] | None:
        """Parse a session mention string."""
        # Remove @session: prefix
        if not mention.startswith("session:"):
            return None

        rest = mention[len("session:") :]

        # Check for quoted title
        if rest.startswith('"') and '"' in rest[1:]:
            end_quote = rest.find('"', 1)
            title = rest[1:end_quote]
            # Would need to search by title - not implemented yet
            return None

        # Check for UUID with optional turn limit
        parts = rest.split(":")
        conv_id_str = parts[0]
        try:
            conversation_id = UUID(conv_id_str)
        except ValueError:
            return None

        max_turns = 10
        if len(parts) > 1:
            try:
                max_turns = int(parts[1])
            except ValueError:
                pass

        return {
            "conversation_id": conversation_id,
            "max_turns": max_turns,
        }

    def _format_turn(self, turn: dict[str, Any]) -> dict[str, Any]:
        """Format a turn for inclusion in the reference."""
        return {
            "turn": turn.get("turn"),
            "user_message": turn.get("user_message", "")[:500],
            "assistant_message": turn.get("assistant_message", "")[:1000],
            "tool_calls": [
                {"tool": tc.get("tool"), "status": tc.get("status")}
                for tc in turn.get("tool_calls", [])
            ],
        }


def get_session_reference_resolver(
    session_query: SessionQueryService,
    settings: Settings,
) -> SessionReferenceResolver:
    """Get the session reference resolver instance."""
    return SessionReferenceResolver(session_query, settings)
