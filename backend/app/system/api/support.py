from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.models.user import User
from app.auth.rbac import require_permissions
from app.auth.tenancy import TenantContext, get_tenant_context
from app.core.config import get_settings
from app.ingestion.services.security.malware_scan_service import MalwareScanService
from app.platform.database.session import get_db, set_db_tenant_context
from app.system.models.support_ticket import SupportTicket
from app.system.models.support_ticket_attachment import SupportTicketAttachment
from app.system.models.support_ticket_message import SupportTicketMessage
from app.system.schemas.support import (
    AdminSupportListResponse,
    AdminSupportMessageCreate,
    AdminSupportQueueItem,
    AdminSupportQueueResponse,
    SupportTicketAttachmentResponse,
    SupportTicketCreate,
    SupportTicketDetailResponse,
    SupportTicketMessageCreate,
    SupportTicketMessageResponse,
    SupportTicketResponse,
    SupportTicketUpdate,
    UserSupportSummary,
)
from app.system.services.audit_service import AuditService
from app.system.services.rate_limit_service import RateLimitService
from app.system.services.storage_quota import StorageQuotaService
from app.system.services.storage_service import StorageService, StorageServiceError
from app.system.services.user_notifications import add_user_notification, notify_platform_admins

router = APIRouter(prefix="/support", tags=["support"])
MAX_SUPPORT_ATTACHMENT_BYTES = 5 * 1024 * 1024
SUPPORT_ATTACHMENT_FILE = File(...)
QUEUE_LIMIT = Query(default=50, ge=1, le=100)
QUEUE_OFFSET = Query(default=0, ge=0, le=100_000)
QUEUE_SEARCH = Query(default=None, max_length=120)
QUEUE_ASSIGNEE = Query(default=None)
QUEUE_UNASSIGNED = Query(default=False)
_SUPPORT_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
}


def _checked_attachment(filename: str, payload: bytes) -> tuple[str, str]:
    from pathlib import PurePath

    safe_name = PurePath(filename.replace("\\", "/")).name.strip()[:255]
    extension = PurePath(safe_name).suffix.lower()
    content_type = _SUPPORT_MIME_BY_EXTENSION.get(extension)
    valid = bool(payload)
    if extension == ".pdf":
        valid = valid and payload.startswith(b"%PDF-")
    elif extension == ".png":
        valid = valid and payload.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = valid and payload.startswith(b"\xff\xd8\xff")
    elif extension == ".txt":
        try:
            payload.decode("utf-8", errors="strict")
            valid = valid and b"\x00" not in payload
        except UnicodeDecodeError:
            valid = False
    else:
        valid = False
    if not valid or not content_type:
        raise HTTPException(
            status_code=415, detail="Attachment must be a valid PDF, PNG, JPEG, or UTF-8 text file"
        )
    return safe_name, content_type


def _attachment_response(row: SupportTicketAttachment) -> SupportTicketAttachmentResponse:
    return SupportTicketAttachmentResponse(
        id=row.id,
        ticket_id=row.ticket_id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        download_url=f"/api/v1/support/tickets/{row.ticket_id}/attachments/{row.id}",
    )


def _ticket_for_user(
    db: Session, *, ticket_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> SupportTicket:
    ticket = db.execute(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.tenant_id == tenant_id,
            SupportTicket.user_id == user_id,
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _ticket_messages(
    db: Session, *, ticket: SupportTicket, include_internal: bool
) -> list[SupportTicketMessage]:
    query = select(SupportTicketMessage).where(
        SupportTicketMessage.ticket_id == ticket.id,
        SupportTicketMessage.tenant_id == ticket.tenant_id,
    )
    if not include_internal:
        query = query.where(SupportTicketMessage.is_internal.is_(False))
    return list(
        db.execute(
            query.order_by(SupportTicketMessage.created_at, SupportTicketMessage.id)
        ).scalars()
    )


def _detail(
    db: Session,
    ticket: SupportTicket,
    *,
    include_internal: bool,
    user_email: str | None = None,
) -> SupportTicketDetailResponse:
    attachment_rows = (
        db.execute(
            select(SupportTicketAttachment)
            .where(
                SupportTicketAttachment.ticket_id == ticket.id,
                SupportTicketAttachment.tenant_id == ticket.tenant_id,
            )
            .order_by(SupportTicketAttachment.created_at)
        )
        .scalars()
        .all()
    )
    attachments = []
    for row in attachment_rows:
        item = _attachment_response(row)
        if include_internal:
            item.download_url = f"/api/v1/support/admin/tickets/{ticket.id}/attachments/{row.id}"
        attachments.append(item)
    return SupportTicketDetailResponse(
        **SupportTicketResponse.model_validate(ticket).model_dump(),
        user_email=user_email,
        messages=[
            SupportTicketMessageResponse.model_validate(item)
            for item in _ticket_messages(db, ticket=ticket, include_internal=include_internal)
        ],
        attachments=attachments,
    )


@router.post("/tickets", response_model=SupportTicketResponse)
def create_ticket(
    payload: SupportTicketCreate,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> SupportTicketResponse:
    RateLimitService(get_settings()).enforce_support_submission_limit(
        tenant_id=str(tenant.tenant_id), user_id=str(auth.user_id)
    )
    now = datetime.now(UTC)
    response_hours, resolution_hours = 8, 72
    ticket = SupportTicket(
        tenant_id=tenant.tenant_id,
        user_id=auth.user_id,
        subject=payload.subject.strip(),
        description=payload.description.strip(),
        category=payload.category,
        status="open",
        priority="normal",
        first_response_due_at=now + timedelta(hours=response_hours),
        resolution_due_at=now + timedelta(hours=resolution_hours),
    )
    db.add(ticket)
    db.flush()
    db.add(
        SupportTicketMessage(
            tenant_id=tenant.tenant_id,
            ticket_id=ticket.id,
            author_role="system",
            kind="acknowledgment",
            body="We received your request. Our support team will follow up in this conversation.",
        )
    )
    add_user_notification(
        db,
        tenant_id=tenant.tenant_id,
        recipient_user_id=auth.user_id,
        event_domain="support",
        event_type="ticket_received",
        title="Support request received",
        message=f"We received your request: {ticket.subject}",
        href=f"/dashboard/support?ticket={ticket.id}",
        resource_id=ticket.id,
        idempotency_key=f"support:received:{ticket.id}",
    )
    notify_platform_admins(
        db,
        event_domain="support",
        event_type="ticket_created",
        title="New support request",
        message=f"{ticket.category.replace('_', ' ').title()}: {ticket.subject}",
        href=f"/dashboard/admin/support?ticket={ticket.id}",
        resource_id=ticket.id,
    )
    db.commit()
    return SupportTicketResponse.model_validate(ticket)


@router.get("/tickets", response_model=list[SupportTicketResponse])
def list_my_tickets(
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> list[SupportTicketResponse]:
    tickets = (
        db.execute(
            select(SupportTicket)
            .where(
                SupportTicket.user_id == auth.user_id, SupportTicket.tenant_id == tenant.tenant_id
            )
            .order_by(desc(SupportTicket.updated_at), desc(SupportTicket.id))
            .limit(100)
        )
        .scalars()
        .all()
    )
    return [SupportTicketResponse.model_validate(ticket) for ticket in tickets]


@router.get("/tickets/{ticket_id}", response_model=SupportTicketDetailResponse)
def get_my_ticket(
    ticket_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> SupportTicketDetailResponse:
    ticket = _ticket_for_user(
        db, ticket_id=ticket_id, tenant_id=tenant.tenant_id, user_id=auth.user_id
    )
    return _detail(db, ticket, include_internal=False)


@router.post("/tickets/{ticket_id}/messages", response_model=SupportTicketDetailResponse)
def add_user_ticket_message(
    ticket_id: uuid.UUID,
    payload: SupportTicketMessageCreate,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> SupportTicketDetailResponse:
    ticket = _ticket_for_user(
        db, ticket_id=ticket_id, tenant_id=tenant.tenant_id, user_id=auth.user_id
    )
    if ticket.status in {"resolved", "closed"}:
        old_status = ticket.status
        ticket.status = "open"
        db.add(
            SupportTicketMessage(
                tenant_id=tenant.tenant_id,
                ticket_id=ticket.id,
                author_user_id=auth.user_id,
                author_role="user",
                kind="status_changed",
                body=f"User reopened this request from {old_status.replace('_', ' ')}.",
            )
        )
    db.add(
        SupportTicketMessage(
            tenant_id=tenant.tenant_id,
            ticket_id=ticket.id,
            author_user_id=auth.user_id,
            author_role="user",
            body=payload.body.strip(),
        )
    )
    ticket.updated_at = datetime.now(UTC)
    if ticket.status == "waiting_user":
        ticket.status = "in_progress"
    notify_platform_admins(
        db,
        event_domain="support",
        event_type="ticket_user_reply",
        title="User replied to a support request",
        message=f"New reply: {ticket.subject}",
        href=f"/dashboard/admin/support?ticket={ticket.id}",
        resource_id=f"{ticket.id}:user-reply:{uuid.uuid4()}",
    )
    db.flush()
    detail = _detail(db, ticket, include_internal=False)
    db.commit()
    return detail


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=SupportTicketAttachmentResponse,
    status_code=201,
)
async def upload_ticket_attachment(
    ticket_id: uuid.UUID,
    file: UploadFile = SUPPORT_ATTACHMENT_FILE,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> SupportTicketAttachmentResponse:
    ticket = _ticket_for_user(
        db, ticket_id=ticket_id, tenant_id=tenant.tenant_id, user_id=auth.user_id
    )
    RateLimitService(get_settings()).enforce_support_submission_limit(
        tenant_id=str(tenant.tenant_id), user_id=str(auth.user_id)
    )
    payload = await file.read(MAX_SUPPORT_ATTACHMENT_BYTES + 1)
    if len(payload) > MAX_SUPPORT_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="Support attachments are limited to 5 MiB")
    filename, content_type = _checked_attachment(file.filename or "attachment", payload)
    scan = MalwareScanService().scan_bytes(
        filename=filename, content_type=content_type, payload=payload
    )
    if not scan.is_clean:
        raise HTTPException(
            status_code=422, detail="Attachment could not pass the configured security scan"
        )
    StorageQuotaService(db).ensure_capacity(
        tenant_id=tenant.tenant_id,
        roles=auth.roles,
        user_id=auth.user_id,
        additional_bytes=len(payload),
    )
    row = SupportTicketAttachment(
        tenant_id=tenant.tenant_id,
        ticket_id=ticket.id,
        uploaded_by_user_id=auth.user_id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(payload),
        storage_bucket=get_settings().minio_bucket,
        storage_key=f"{tenant.tenant_id}/pending/{uuid.uuid4()}",
    )
    db.add(row)
    db.flush()
    try:
        from pathlib import PurePath

        stored = StorageService(get_settings()).put_bytes(
            tenant_id=tenant.tenant_id,
            document_id=row.id,
            filename=f"support-{row.id}{PurePath(filename).suffix.lower()}",
            content_type=content_type,
            payload=payload,
        )
        row.storage_bucket = stored.bucket
        row.storage_key = stored.object_key
        db.commit()
    except Exception as exc:
        db.rollback()
        try:
            if "stored" in locals():
                StorageService(get_settings()).delete_tenant_object(
                    tenant_id=tenant.tenant_id, bucket=stored.bucket, object_key=stored.object_key
                )
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Unable to store support attachment") from exc
    return _attachment_response(row)


def _read_ticket_attachment(
    db: Session, *, ticket_id: uuid.UUID, attachment_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[SupportTicketAttachment, bytes]:
    row = db.execute(
        select(SupportTicketAttachment).where(
            SupportTicketAttachment.id == attachment_id,
            SupportTicketAttachment.ticket_id == ticket_id,
            SupportTicketAttachment.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    try:
        data = StorageService(get_settings()).get_tenant_bytes(
            tenant_id=tenant_id, bucket=row.storage_bucket, object_key=row.storage_key
        )
    except StorageServiceError as exc:
        raise HTTPException(status_code=404, detail="Attachment payload is unavailable") from exc
    return row, data


@router.get("/tickets/{ticket_id}/attachments/{attachment_id}")
def download_ticket_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> Response:
    _ticket_for_user(db, ticket_id=ticket_id, tenant_id=tenant.tenant_id, user_id=auth.user_id)
    row, data = _read_ticket_attachment(
        db, ticket_id=ticket_id, attachment_id=attachment_id, tenant_id=tenant.tenant_id
    )
    from urllib.parse import quote

    return Response(
        content=data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# The support management surface
#
# Support staff read tickets across tenants, so every route here requires
# the ``admin:support:read`` or ``admin:support:write`` permission that the
# ``support_manager`` role carries.  The permission is granted by an explicit
# staff assignment, never by a normal or platform-admin account alone.
# ---------------------------------------------------------------------------


def _support_admin_scope(db: Session = Depends(get_db)) -> Session:
    """Apply the cross-tenant scope the support staff surface needs.

    Support staff read tickets across tenants, so the row-level security
    context is the deployment's own bypass.  The caller's tenant still
    decides which audit records the read is written to.

    Args:
        db: The database session.

    Returns:
        The database session.
    """
    set_db_tenant_context(db, "bypass")
    return db


def _admin_ticket(db: Session, *, ticket_id: uuid.UUID) -> SupportTicket:
    """Return one ticket for the support staff surface.

    Args:
        db: The database session.
        ticket_id: The ticket the request named.

    Returns:
        The ticket.

    Raises:
        HTTPException: When the ticket does not exist.
    """
    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _admin_queue_item(
    db: Session, ticket: SupportTicket, *, user_email: str
) -> AdminSupportQueueItem:
    """Project one ticket for the support queue.

    Args:
        db: The database session.
        ticket: The ticket to project.
        user_email: The email of the user who opened the ticket.

    Returns:
        The queue item.
    """
    return AdminSupportQueueItem(
        **SupportTicketResponse.model_validate(ticket).model_dump(),
        user_email=user_email,
    )


def _user_email(db: Session, *, user_id: uuid.UUID) -> str:
    """Read the email of one user.

    Args:
        db: The database session.
        user_id: The user to read.

    Returns:
        The user's email, or an empty string when the user is gone.
    """
    email = db.execute(select(User.email).where(User.id == user_id)).scalar_one_or_none()
    return str(email or "")


@router.get(
    "/admin/queue",
    response_model=AdminSupportQueueResponse,
    dependencies=[Depends(require_permissions("admin:support:read"))],
)
def admin_support_queue(
    limit: int = QUEUE_LIMIT,
    offset: int = QUEUE_OFFSET,
    search: str | None = QUEUE_SEARCH,
    assignee: str | None = QUEUE_ASSIGNEE,
    unassigned: bool = QUEUE_UNASSIGNED,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> AdminSupportQueueResponse:
    """List the support queue across tenants."""
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(SupportTicket.subject.ilike(pattern), SupportTicket.description.ilike(pattern))
        )
    if unassigned:
        filters.append(SupportTicket.assigned_admin_id.is_(None))
    elif assignee:
        try:
            filters.append(SupportTicket.assigned_admin_id == uuid.UUID(assignee))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Assignee is not a valid id") from exc
    where = [*filters] if filters else [SupportTicket.id.is_not(None)]
    total = int(
        db.execute(select(func.count()).select_from(SupportTicket).where(*where)).scalar() or 0
    )
    open_count = int(
        db.execute(
            select(func.count())
            .select_from(SupportTicket)
            .where(*where, SupportTicket.status.notin_(["resolved", "closed"]))
        ).scalar()
        or 0
    )
    now = datetime.now(UTC)
    overdue_count = int(
        db.execute(
            select(func.count())
            .select_from(SupportTicket)
            .where(
                *where,
                SupportTicket.status.notin_(["resolved", "closed"]),
                SupportTicket.first_response_due_at.is_not(None),
                SupportTicket.first_response_due_at < now,
            )
        ).scalar()
        or 0
    )
    tickets = (
        db.execute(
            select(SupportTicket)
            .where(*where)
            .order_by(
                case(
                    (SupportTicket.priority == "urgent", 0),
                    (SupportTicket.priority == "high", 1),
                    (SupportTicket.priority == "normal", 2),
                    else_=3,
                ),
                desc(SupportTicket.created_at),
            )
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    response = AdminSupportQueueResponse(
        items=[
            _admin_queue_item(db, ticket, user_email=_user_email(db, user_id=ticket.user_id))
            for ticket in tickets
        ],
        total=total,
        open_count=open_count,
        overdue_count=overdue_count,
    )
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.support.queue.read",
        resource_type="support_queue",
    )
    return response


@router.get(
    "/admin/tickets",
    response_model=AdminSupportListResponse,
    dependencies=[Depends(require_permissions("admin:support:read"))],
)
def admin_support_history(
    limit: int = QUEUE_LIMIT,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> AdminSupportListResponse:
    """List the users who have support requests and their request counts."""
    rows = db.execute(
        select(
            SupportTicket.user_id,
            func.count(SupportTicket.id).label("ticket_count"),
            func.max(SupportTicket.created_at).label("last_ticket_at"),
        )
        .group_by(SupportTicket.user_id)
        .order_by(desc(func.max(SupportTicket.created_at)))
        .limit(limit)
    ).all()
    items: list[UserSupportSummary] = []
    for row in rows:
        latest = (
            db.execute(
                select(SupportTicket)
                .where(SupportTicket.user_id == row.user_id)
                .order_by(desc(SupportTicket.created_at))
                .limit(5)
            )
            .scalars()
            .all()
        )
        items.append(
            UserSupportSummary(
                user_id=row.user_id,
                email=_user_email(db, user_id=row.user_id),
                ticket_count=int(row.ticket_count or 0),
                last_ticket_at=row.last_ticket_at,
                latest_tickets=[SupportTicketResponse.model_validate(item) for item in latest],
            )
        )
    response = AdminSupportListResponse(items=items)
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.support.history.list.read",
        resource_type="support_history",
    )
    return response


@router.get(
    "/admin/tickets/{ticket_id}",
    response_model=SupportTicketDetailResponse,
    dependencies=[Depends(require_permissions("admin:support:read"))],
)
def admin_support_ticket_detail(
    ticket_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> SupportTicketDetailResponse:
    """Read one ticket, including its internal messages."""
    ticket = _admin_ticket(db, ticket_id=ticket_id)
    detail = _detail(
        db,
        ticket,
        include_internal=True,
        user_email=_user_email(db, user_id=ticket.user_id),
    )
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.support.ticket.read",
        resource_type="support_ticket",
        resource_id=str(ticket.id),
    )
    return detail


@router.patch(
    "/admin/tickets/{ticket_id}",
    response_model=SupportTicketResponse,
    dependencies=[Depends(require_permissions("admin:support:write"))],
)
def admin_support_ticket_update(
    ticket_id: uuid.UUID,
    payload: SupportTicketUpdate,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> SupportTicketResponse:
    """Assign or reclassify one ticket."""
    ticket = _admin_ticket(db, ticket_id=ticket_id)
    now = datetime.now(UTC)
    if payload.status is not None:
        ticket.status = payload.status
        if payload.status == "resolved":
            ticket.resolved_at = now
        if ticket.first_response_at is None:
            ticket.first_response_at = now
    if payload.category is not None:
        ticket.category = payload.category
    if payload.priority is not None:
        ticket.priority = payload.priority
    if payload.assigned_admin_id is not None:
        ticket.assigned_admin_id = payload.assigned_admin_id
    ticket.updated_at = now
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)


@router.post(
    "/admin/tickets/{ticket_id}/messages",
    response_model=SupportTicketDetailResponse,
    dependencies=[Depends(require_permissions("admin:support:write"))],
)
def admin_support_ticket_message(
    ticket_id: uuid.UUID,
    payload: AdminSupportMessageCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> SupportTicketDetailResponse:
    """Reply to one ticket, publicly or as an internal note."""
    ticket = _admin_ticket(db, ticket_id=ticket_id)
    now = datetime.now(UTC)
    is_internal = payload.visibility == "internal"
    db.add(
        SupportTicketMessage(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            author_user_id=auth.user_id,
            author_role="admin",
            kind="reply",
            body=payload.body.strip(),
            is_internal=is_internal,
        )
    )
    if not is_internal:
        # A public reply waits for the user, and the user is told.
        ticket.status = "waiting_user"
        if ticket.first_response_at is None:
            ticket.first_response_at = now
        add_user_notification(
            db,
            tenant_id=ticket.tenant_id,
            recipient_user_id=ticket.user_id,
            event_domain="support",
            event_type="ticket_reply",
            title="Support replied to your request",
            message=f"Support replied to {ticket.subject}.",
            href=f"/dashboard/support?ticket={ticket.id}",
            resource_id=ticket.id,
            idempotency_key=f"support:reply:{ticket.id}:{now.isoformat()}",
        )
    ticket.updated_at = now
    db.add(ticket)
    db.flush()
    detail = _detail(
        db,
        ticket,
        include_internal=True,
        user_email=_user_email(db, user_id=ticket.user_id),
    )
    db.commit()
    return detail


@router.get(
    "/admin/tickets/{ticket_id}/attachments/{attachment_id}",
    dependencies=[Depends(require_permissions("admin:support:read"))],
)
def admin_support_ticket_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(_support_admin_scope),
) -> Response:
    """Download one attachment of a ticket."""
    ticket = _admin_ticket(db, ticket_id=ticket_id)
    row, data = _read_ticket_attachment(
        db, ticket_id=ticket_id, attachment_id=attachment_id, tenant_id=ticket.tenant_id
    )
    # The audit write commits, and a commit ends the transaction the
    # cross-tenant scope belongs to, so it is the last thing the route does.
    AuditService(db).write_admin_read_event(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        action="admin.support.attachment.read",
        resource_type="support_attachment",
        resource_id=str(row.id),
    )
    from urllib.parse import quote

    return Response(
        content=data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
