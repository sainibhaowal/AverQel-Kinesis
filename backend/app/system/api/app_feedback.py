from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.auth.tenancy import TenantContext, get_tenant_context
from app.core.config import get_settings
from app.platform.database.session import get_db, set_db_tenant_context
from app.system.models.app_feedback import AppFeedback, FeedbackCampaign
from app.system.models.app_feedback_message import AppFeedbackMessage
from app.system.schemas.app_feedback import (
    AdminAppFeedbackDetailResponse,
    AdminAppFeedbackMessageResponse,
    AdminAppFeedbackResponse,
    AdminFeedbackMessageCreate,
    AppFeedbackCreate,
    AppFeedbackDetailResponse,
    AppFeedbackMessageCreate,
    AppFeedbackMessageResponse,
    AppFeedbackResponse,
    FeedbackCampaignResponse,
)
from app.system.services.audit_service import AuditService
from app.system.services.rate_limit_service import RateLimitService
from app.system.services.user_notifications import add_user_notification, notify_role_assignees

router = APIRouter(prefix="/app-feedback", tags=["app-feedback"])


def _feedback_for_user(
    db: Session, *, feedback_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> AppFeedback:
    feedback = db.execute(
        select(AppFeedback).where(
            AppFeedback.id == feedback_id,
            AppFeedback.tenant_id == tenant_id,
            AppFeedback.user_id == user_id,
        )
    ).scalar_one_or_none()
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return feedback


def _feedback_messages(
    db: Session, *, feedback: AppFeedback, include_internal: bool
) -> list[AppFeedbackMessage]:
    query = select(AppFeedbackMessage).where(
        AppFeedbackMessage.feedback_id == feedback.id,
        AppFeedbackMessage.tenant_id == feedback.tenant_id,
    )
    if not include_internal:
        query = query.where(AppFeedbackMessage.is_internal.is_(False))
    return list(
        db.execute(query.order_by(AppFeedbackMessage.created_at, AppFeedbackMessage.id)).scalars()
    )


def _detail(
    db: Session, feedback: AppFeedback, *, include_internal: bool, email: str | None = None
) -> AppFeedbackDetailResponse:
    response = AppFeedbackResponse.model_validate(feedback)
    response.email = email
    return AppFeedbackDetailResponse(
        **response.model_dump(),
        messages=[
            AppFeedbackMessageResponse.model_validate(item)
            for item in _feedback_messages(db, feedback=feedback, include_internal=include_internal)
        ],
    )


def _admin_detail(db: Session, feedback: AppFeedback) -> AdminAppFeedbackDetailResponse:
    return AdminAppFeedbackDetailResponse(
        **AdminAppFeedbackResponse.model_validate(feedback).model_dump(),
        messages=[
            AdminAppFeedbackMessageResponse.model_validate(item)
            for item in _feedback_messages(db, feedback=feedback, include_internal=True)
        ],
    )


@router.post("/submit", response_model=AppFeedbackResponse)
def submit_app_feedback(
    payload: AppFeedbackCreate,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> AppFeedbackResponse:
    RateLimitService(get_settings()).enforce_feedback_submission_limit(
        tenant_id=str(tenant.tenant_id), user_id=str(auth.user_id)
    )
    campaign = None
    if payload.campaign_id:
        campaign = db.execute(
            select(FeedbackCampaign).where(
                FeedbackCampaign.id == payload.campaign_id,
                FeedbackCampaign.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=404, detail="Feedback campaign not found")
    feedback = AppFeedback(
        tenant_id=tenant.tenant_id,
        user_id=auth.user_id,
        campaign_id=payload.campaign_id,
        subject=payload.subject.strip(),
        content=payload.content.strip(),
        category=payload.category,
        status="new",
    )
    db.add(feedback)
    db.flush()
    add_user_notification(
        db,
        tenant_id=tenant.tenant_id,
        recipient_user_id=auth.user_id,
        event_domain="feedback",
        event_type="feedback_received",
        title="Feedback received",
        message=f"Thanks for sharing: {feedback.subject}",
        href=f"/dashboard/feedback?feedback={feedback.id}",
        resource_id=str(feedback.id),
        idempotency_key=f"feedback:received:{feedback.id}",
    )
    notify_role_assignees(
        db,
        role_name="feedback_manager",
        tenant_id=None,
        event_domain="feedback",
        event_type="feedback_submitted",
        title="New user feedback",
        message="A user submitted feedback for review.",
        href=f"/dashboard/admin/feedback?submission={feedback.id}",
        resource_id=str(feedback.id),
    )
    db.commit()
    return AppFeedbackResponse.model_validate(feedback)


@router.get("/mine", response_model=list[AppFeedbackResponse])
def list_my_feedback(
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> list[AppFeedbackResponse]:
    items = (
        db.execute(
            select(AppFeedback)
            .where(AppFeedback.tenant_id == tenant.tenant_id, AppFeedback.user_id == auth.user_id)
            .order_by(desc(AppFeedback.updated_at), desc(AppFeedback.id))
            .limit(100)
        )
        .scalars()
        .all()
    )
    return [AppFeedbackResponse.model_validate(item) for item in items]


@router.get("/mine/{feedback_id}", response_model=AppFeedbackDetailResponse)
def get_my_feedback(
    feedback_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> AppFeedbackDetailResponse:
    feedback = _feedback_for_user(
        db, feedback_id=feedback_id, tenant_id=tenant.tenant_id, user_id=auth.user_id
    )
    return _detail(db, feedback, include_internal=False)


@router.post("/mine/{feedback_id}/messages", response_model=AppFeedbackDetailResponse)
def add_user_feedback_message(
    feedback_id: uuid.UUID,
    payload: AppFeedbackMessageCreate,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> AppFeedbackDetailResponse:
    feedback = _feedback_for_user(
        db, feedback_id=feedback_id, tenant_id=tenant.tenant_id, user_id=auth.user_id
    )
    db.add(
        AppFeedbackMessage(
            tenant_id=tenant.tenant_id,
            feedback_id=feedback.id,
            author_user_id=auth.user_id,
            author_role="user",
            body=payload.body.strip(),
        )
    )
    feedback.updated_at = datetime.now(UTC)
    notify_role_assignees(
        db,
        role_name="feedback_manager",
        tenant_id=None,
        event_domain="feedback",
        event_type="feedback_user_reply",
        title="User added feedback details",
        message="A user added details to a feedback submission.",
        href=f"/dashboard/admin/feedback?submission={feedback.id}",
        resource_id=f"{feedback.id}:user-reply:{uuid.uuid4()}",
    )
    db.flush()
    detail = _detail(db, feedback, include_internal=False)
    db.commit()
    return detail


@router.get("/campaigns", response_model=list[FeedbackCampaignResponse])
def list_active_campaigns(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[FeedbackCampaignResponse]:
    campaigns = (
        db.execute(
            select(FeedbackCampaign)
            .where(FeedbackCampaign.is_active)
            .order_by(desc(FeedbackCampaign.created_at))
        )
        .scalars()
        .all()
    )
    return [FeedbackCampaignResponse.model_validate(campaign) for campaign in campaigns]


# ---------------------------------------------------------------------------
# The feedback review surface
#
# Review staff read feedback across tenants, so every route here requires
# the ``admin:feedback:read`` or ``admin:feedback:write`` permission that
# the ``feedback_manager`` role carries. The responses carry no account
# identifier and no contact detail, so a reviewer sees the report and not
# the person who sent it.
# ---------------------------------------------------------------------------


def _feedback_admin_scope(db: Session = Depends(get_db)) -> Session:
    """Apply the cross-tenant scope the feedback review surface needs.

    Review staff read feedback across tenants, so the row-level security
    context is the deployment's own bypass. The caller's tenant still
    decides which audit records the read is written to.

    Args:
        db: The database session.

    Returns:
        The database session.
    """
    set_db_tenant_context(db, "bypass")
    return db


def _admin_feedback(db: Session, *, feedback_id: uuid.UUID) -> AppFeedback:
    """Return one feedback submission for the review surface.

    Args:
        db: The database session.
        feedback_id: The submission the request named.

    Returns:
        The submission.

    Raises:
        HTTPException: When the submission does not exist.
    """
    feedback = db.execute(
        select(AppFeedback).where(AppFeedback.id == feedback_id)
    ).scalar_one_or_none()
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return feedback


@router.get(
    "/admin/submissions",
    response_model=list[AdminAppFeedbackResponse],
    dependencies=[Depends(require_permissions("admin:feedback:read"))],
)
def list_feedback_submissions(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    status: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_feedback_admin_scope),
) -> list[AdminAppFeedbackResponse]:
    """List the feedback submissions for review."""
    query = select(AppFeedback)
    if status:
        query = query.where(AppFeedback.status == status)
    items = (
        db.execute(
            query.order_by(desc(AppFeedback.created_at), desc(AppFeedback.id))
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.feedback.submissions.list.read",
        resource_type="app_feedback",
    )
    return [AdminAppFeedbackResponse.model_validate(item) for item in items]


@router.get(
    "/admin/submissions/{feedback_id}",
    response_model=AdminAppFeedbackDetailResponse,
    dependencies=[Depends(require_permissions("admin:feedback:read"))],
)
def get_feedback_submission(
    feedback_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_feedback_admin_scope),
) -> AdminAppFeedbackDetailResponse:
    """Read one feedback submission, including its internal notes."""
    feedback = _admin_feedback(db, feedback_id=feedback_id)
    detail = _admin_detail(db, feedback)
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.feedback.submission.read",
        resource_type="app_feedback",
        resource_id=str(feedback.id),
    )
    return detail


@router.post(
    "/admin/submissions/{feedback_id}/messages",
    response_model=AdminAppFeedbackDetailResponse,
    dependencies=[Depends(require_permissions("admin:feedback:write"))],
)
def add_feedback_review_message(
    feedback_id: uuid.UUID,
    payload: AdminFeedbackMessageCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_feedback_admin_scope),
) -> AdminAppFeedbackDetailResponse:
    """Reply to one feedback submission, publicly or as an internal note."""
    feedback = _admin_feedback(db, feedback_id=feedback_id)
    is_internal = payload.visibility == "internal"
    now = datetime.now(UTC)
    db.add(
        AppFeedbackMessage(
            tenant_id=feedback.tenant_id,
            feedback_id=feedback.id,
            author_user_id=auth.user_id,
            author_role="admin",
            kind="reply",
            body=payload.body.strip(),
            is_internal=is_internal,
        )
    )
    if not is_internal:
        # A public reply reaches the user, who is told about it.
        feedback.status = "triaged" if feedback.status == "new" else feedback.status
        add_user_notification(
            db,
            tenant_id=feedback.tenant_id,
            recipient_user_id=feedback.user_id,
            event_domain="feedback",
            event_type="feedback_reply",
            title="A reviewer replied to your feedback",
            message=f"A reviewer replied to {feedback.subject}.",
            href=f"/dashboard/feedback?feedback={feedback.id}",
            resource_id=str(feedback.id),
            idempotency_key=f"feedback:reply:{feedback.id}:{now.isoformat()}",
        )
    feedback.updated_at = now
    db.add(feedback)
    db.flush()
    detail = _admin_detail(db, feedback)
    db.commit()
    return detail
