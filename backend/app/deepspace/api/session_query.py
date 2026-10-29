"""The session-query routes for DeepSpace.

The typed session log is the durable record of what the agent did. These
routes read it: search the conversations and the events of one
conversation, read one event with its neighbours, and trace the lineage of
a turn.

Every route is scoped to the tenant and the user that owns the
conversation, and a conversation that is not theirs is reported as not
found rather than as forbidden, so the routes never reveal that it
exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.deepspace.services.session_query import SessionQueryService
from app.platform.database.session import get_db

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-session-query"])


@router.get(
    "/session-query/sessions",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def search_sessions(
    query: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Search the caller's conversations for work they did before."""
    service = SessionQueryService(db)
    return {
        "sessions": service.search_sessions(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            query=query,
            limit=limit,
        )
    }


@router.get(
    "/{conversation_id}/session-query/events",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def search_session_events(
    conversation_id: uuid.UUID,
    query: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Search the events of one conversation."""
    service = SessionQueryService(db)
    events = service.search_events(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        query=query,
        limit=limit,
    )
    if events is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"events": events}


@router.get(
    "/{conversation_id}/session-query/events/{seq}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def read_session_event(
    conversation_id: uuid.UUID,
    seq: int,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read one event of a conversation, with its neighbours."""
    service = SessionQueryService(db)
    event = service.read_event(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        seq=seq,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return event


@router.get(
    "/{conversation_id}/session-query/trace",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def trace_session(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read the timeline of one conversation."""
    service = SessionQueryService(db)
    trace = service.trace_session(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return trace


@router.get(
    "/{conversation_id}/session-query/trace/{seq}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def trace_session_event(
    conversation_id: uuid.UUID,
    seq: int,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Trace the lineage of one event."""
    service = SessionQueryService(db)
    lineage = service.trace_event(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        seq=seq,
    )
    if lineage is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return lineage


__all__ = ["router"]
