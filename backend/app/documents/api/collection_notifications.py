"""The collection notification routes.

A collection notification tells a user that something happened in a
collection they follow.  The list is scoped to the user, and it honours the
notification preferences: a muted domain hides its entries from the feed
without deleting them, so the history stays intact and the entries return
when the domain is unmuted.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.tenancy import TenantContext, get_tenant_context
from app.documents.repositories.collection_notifications import (
    CollectionNotificationsRepository,
)
from app.documents.schemas.collection import CollectionNotificationResponse
from app.platform.database.session import get_db
from app.system.models.user_notification_preference import UserNotificationPreference

router = APIRouter(prefix="/collections", tags=["collection-notifications"])

#: The notification domain the collection feed belongs to.
COLLECTION_NOTIFICATION_DOMAIN = "collections"


def _muted_domains(db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[str]:
    """Read the notification domains the user muted.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The user whose preferences are read.

    Returns:
        The muted domains.
    """
    muted = (
        db.execute(
            select(UserNotificationPreference.muted_domains).where(
                UserNotificationPreference.tenant_id == tenant_id,
                UserNotificationPreference.user_id == user_id,
            )
        ).scalar_one_or_none()
        or []
    )
    return [str(item) for item in muted]


@router.get("/notifications", response_model=list[CollectionNotificationResponse])
def list_collection_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> list[CollectionNotificationResponse]:
    """List the collection notifications the user can see."""
    if COLLECTION_NOTIFICATION_DOMAIN in _muted_domains(
        db, tenant_id=tenant.tenant_id, user_id=auth.user_id
    ):
        # A muted domain hides its entries from the feed. The rows stay, so
        # unmuting the domain brings the history back.
        return []
    rows = CollectionNotificationsRepository(db).list_for_user(
        user_id=auth.user_id,
        limit=limit,
        offset=offset,
    )
    return [CollectionNotificationResponse.model_validate(row) for row in rows]


@router.post("/notifications/read-all", status_code=204)
def mark_all_collection_notifications_read(
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> Response:
    """Mark every collection notification the user can see as read."""
    CollectionNotificationsRepository(db).mark_all_read_for_user(user_id=auth.user_id)
    db.commit()
    return Response(status_code=204)


@router.delete("/notifications", status_code=204)
def delete_collection_notifications(
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> Response:
    """Remove the collection notifications the user can see."""
    CollectionNotificationsRepository(db).delete_all_for_user(user_id=auth.user_id)
    db.commit()
    return Response(status_code=204)


__all__ = [
    "COLLECTION_NOTIFICATION_DOMAIN",
    "router",
]
