"""Session feedback routes for DeepSpace conversations."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import get_settings
from app.deepspace.services.session_feedback import get_session_feedback_service
from app.platform.database.session import get_db

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-session-feedback"])


class SessionFeedbackRequest(BaseModel):
    """The user rating and optional explanation for one conversation."""

    rating: Literal["positive", "negative", "neutral"]
    categories: list[str] = Field(default_factory=list, max_length=6)
    comment: str | None = Field(default=None, max_length=2000)
    turn: int | None = Field(default=None, ge=0)


def _error(exc: ValueError) -> HTTPException:
    """Translate service validation failures into stable API errors."""
    status = 404 if str(exc) == "Conversation not found" else 400
    return HTTPException(status_code=status, detail=str(exc))


@router.post(
    "/{conversation_id}/feedback",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def submit_session_feedback(
    conversation_id: uuid.UUID,
    payload: SessionFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Persist a rating as a durable ``feedback/given`` session event."""
    service = get_session_feedback_service(get_settings())
    try:
        feedback = await service.submit_feedback(
            db,
            auth=auth,
            conversation_id=conversation_id,
            rating=payload.rating,
            categories=payload.categories,
            comment=payload.comment,
            turn=payload.turn,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return {"feedback": feedback.as_dict()}


@router.get(
    "/{conversation_id}/feedback",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def list_session_feedback(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read the caller's durable feedback for one conversation."""
    service = get_session_feedback_service(get_settings())
    try:
        feedback = await service.get_feedback(
            db,
            auth=auth,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return {"feedback": [item.as_dict() for item in feedback]}


@router.get(
    "/{conversation_id}/feedback/stats",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def session_feedback_stats(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read feedback totals after verifying conversation ownership."""
    service = get_session_feedback_service(get_settings())
    try:
        await service.get_feedback(db, auth=auth, conversation_id=conversation_id)
    except ValueError as exc:
        raise _error(exc) from exc
    return await service.get_aggregate_stats(
        db,
        auth=auth,
        conversation_id=conversation_id,
    )


__all__ = ["router"]
