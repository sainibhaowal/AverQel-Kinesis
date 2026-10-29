"""Session log export for DeepSpace.

Ported from NeoSIS @averqel/neosis-session-query/session-log-export.
Provides JSONL export of session events.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.deepspace.api.chats import get_db
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent

router = APIRouter(prefix="/export", tags=["session-export"])


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Options for session log export."""

    format: Literal[jsonl, json] = "jsonl"
    include_events: list[str] | None = None
    start_seq: int | None = None
    end_seq: int | None = None
    max_events: int = 10000


async def export_session_log(
    conversation_id: UUID,
    *,
    db: Session,
    auth: AuthContext,
    options: ExportOptions,
) -> AsyncIterator[bytes]:
    """Stream session log events as JSONL.

    Args:
        conversation_id: The conversation to export.
        db: Database session.
        auth: Authentication context.
        options: Export options.

    Yields:
        JSONL lines (one event per line) or JSON array.
    """
    # Verify conversation ownership
    conv = db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == auth.tenant_id,
            Conversation.user_id == auth.user_id,
        )
    ).scalar_one_or_none()

    if not conv:
        raise ValueError("Conversation not found or access denied")

    # Build query
    query = select(DeepSpaceSessionEvent).where(
        DeepSpaceSessionEvent.conversation_id == conversation_id
    )

    if options.start_seq is not None:
        query = query.where(DeepSpaceSessionEvent.seq >= options.start_seq)
    if options.end_seq is not None:
        query = query.where(DeepSpaceSessionEvent.seq <= options.end_seq)
    if options.include_events:
        query = query.where(DeepSpaceSessionEvent.event_type.in_(options.include_events))

    query = query.order_by(DeepSpaceSessionEvent.seq.asc()).limit(options.max_events)

    # Execute and stream
    first = True
    if options.format == "json":
        yield b"["
        first = True

    for event in db.scalars(query).all():
        event_data = {
            "seq": event.seq,
            "event_type": event.event_type,
            "turn": event.turn,
            "step": event.step,
            "tool_name": event.tool_name,
            "payload": event.payload,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }

        if options.format == "jsonl":
            yield (json.dumps(event_data, ensure_ascii=False) + "\n").encode("utf-8")
        else:
            if not first:
                yield b","
            yield json.dumps(event_data, ensure_ascii=False).encode("utf-8")
            first = False

    if options.format == "json":
        yield b"]"


@router.get("/conversations/{conversation_id}")
async def export_conversation_log(
    conversation_id: UUID,
    format: Literal[jsonl, json] = Query(default="jsonl"),
    include_events: str | None = Query(default=None),
    start_seq: int | None = Query(default=None),
    end_seq: int | None = Query(default=None),
    max_events: int = Query(default=10000, ge=1, le=100000),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Export a conversation's session log as JSONL or JSON.

    The export includes all typed session events for the conversation,
    optionally filtered by event type and sequence range.
    """
    if not getattr(settings, "deepspace_session_export_enabled", True):
        return Response(
            content="Session export is disabled on this deployment.",
            status_code=403,
        )

    include_list = include_events.split(",") if include_events else None

    options = ExportOptions(
        format=format,
        include_events=include_list,
        start_seq=start_seq,
        end_seq=end_seq,
        max_events=max_events,
    )

    media_type = "application/x-ndjson" if format == "jsonl" else "application/json"
    filename = f"conversation-{conversation_id}.{format}"

    return StreamingResponse(
        export_session_log(conversation_id, db=db, auth=auth, options=options),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/conversations/{conversation_id}/metadata")
async def export_conversation_metadata(
    conversation_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Export conversation metadata (title, stats, etc.) as JSON."""
    if not getattr(settings, "deepspace_session_export_enabled", True):
        return Response(
            content="Session export is disabled on this deployment.",
            status_code=403,
        )

    conv = db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == auth.tenant_id,
            Conversation.user_id == auth.user_id,
        )
    ).scalar_one_or_none()

    if not conv:
        return Response(content="Conversation not found", status_code=404)

    # Get event stats
    from sqlalchemy import func

    stats = db.execute(
        select(
            func.count(DeepSpaceSessionEvent.id).label("total_events"),
            func.min(DeepSpaceSessionEvent.seq).label("first_seq"),
            func.max(DeepSpaceSessionEvent.seq).label("last_seq"),
        ).where(DeepSpaceSessionEvent.conversation_id == conversation_id)
    ).first()

    return {
        "conversation_id": str(conv.id),
        "title": conv.title,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        "event_count": stats.total_events or 0,
        "first_seq": stats.first_seq,
        "last_seq": stats.last_seq,
    }
