"""The typed event log for a DeepSpace conversation.

``DeepSpaceRunEvent`` stores the opaque SSE frames a client reconnects to.
This table stores the same run as structured records: a turn boundary, a step
boundary, a tool call, a tool result, or a delegation record.  The two are
complementary — the frame log is what the browser replays, and the typed log
is what the projections and the compaction pipeline read.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings
from app.deepspace.services.session_log_crypto import (
    ENVELOPE_MARKER,
    ENVELOPE_VERSION,
    SessionLogCryptoError,
    get_session_log_cipher,
)
from app.platform.database.base import Base


class SessionLogPayload(TypeDecorator[dict[str, object]]):
    """Store an event payload encrypted while readers see its plaintext.

    The column keeps the ``JSON`` type, so the database schema does not
    change.  Writes become an AES-GCM envelope when
    ``deepspace_session_log_encryption`` is true.  Reads decrypt an envelope
    and pass a legacy plaintext object through unchanged.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        del dialect
        if value is None:
            return None
        settings = get_settings()
        if not settings.deepspace_session_log_encryption:
            return value
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return get_session_log_cipher(settings).encrypt(plaintext)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        del dialect
        if value is None:
            return None
        if isinstance(value, Mapping) and value.get(ENVELOPE_MARKER) == ENVELOPE_VERSION:
            plaintext = get_session_log_cipher(get_settings()).decrypt(value)
            decoded = json.loads(plaintext)
            if not isinstance(decoded, dict):
                raise SessionLogCryptoError("session log payload is not a JSON object")
            return decoded
        if isinstance(value, dict):
            return value
        return cast(dict[str, object], value)


class DeepSpaceSessionEvent(Base):
    """One structured event in a DeepSpace conversation's session log."""

    __tablename__ = "deepspace_session_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The run that produced the event, when the event belongs to one.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deepspace_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The delegated child the event belongs to, when it belongs to one.
    subagent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    #: The per-conversation sequence number, monotonic and gap-free.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: The event name, such as ``turn/start`` or ``tool/call``.
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: The turn the event belongs to.
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: The step the event belongs to.
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The tool the event concerns, for a tool call or result.
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: The event's own data, encrypted at rest.
    payload: Mapped[dict[str, object]] = mapped_column(
        SessionLogPayload, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
