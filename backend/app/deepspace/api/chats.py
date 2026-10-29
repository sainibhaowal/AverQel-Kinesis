"""DeepSpace productivity chat, note, history, and memory endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated, Any

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    API_KEY_PREFIX,
    AuthContext,
    build_auth_context_from_api_key,
    build_auth_context_from_jwt,
    decode_access_token,
    get_auth_context,
)
from app.auth.rbac import require_permissions, resolve_permissions
from app.auth.roles import is_admin_role
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.repositories.request_metrics import DeepSpaceRequestMetricsRepository
from app.deepspace.schemas.chats import (
    ApprovalDecisionRequest,
    BulkDeleteRequest,
    ChatHistoryResponse,
    ConversationAppendContentRequest,
    ConversationCreateRequest,
    ConversationForkRequest,
    ConversationListResponse,
    ConversationRetentionProtectionRequest,
    ConversationRetentionSchema,
    ConversationSchema,
    ConversationUpdate,
    MemoryFactSchema,
    MemoryPreferencesSchema,
    MemoryPreferencesUpdateRequest,
    MemoryRetentionReportSchema,
    MemoryUpdateRequest,
    MemoryWriteRequest,
    MessageEditRequest,
    MessageSchema,
    MessageVersionSchema,
    PlanModeRequest,
    PlanReviewRequest,
    QueuedTurnSchema,
    QueueStateSchema,
    QueueTurnEditRequest,
    QueueTurnRequest,
    RegenerateRequest,
    RetryFailedTurnRequest,
    SandboxModeRequest,
    VisualBlockUpdateRequest,
)
from app.deepspace.services.agent_team import AgentTeamService
from app.deepspace.services.chat_service import DeepSpaceChatService, sse
from app.deepspace.services.context_cache import DeepSpaceContextCache
from app.deepspace.services.conversation_fork import get_conversation_fork_service
from app.deepspace.services.deployment_persona import deployment_persona_state
from app.deepspace.services.plan_mode import PlanModeService
from app.deepspace.services.plan_store import PlanStoreError
from app.deepspace.services.presets import PresetStateService, resolve_preset
from app.deepspace.services.reasoning_privacy import (
    REASONING_REDACTION_VERSION,
    redact_reasoning_text,
)
from app.deepspace.services.run_events import (
    cancellation_key,
    channel_name,
    decode_live_event,
    event_name_from_frame,
    frames_after,
    is_terminal_event,
    latest_sequence,
    load_events,
    timeline_events,
)
from app.deepspace.services.runtime_store import DeepSpaceRuntimeStore
from app.deepspace.services.sandbox_mode import (
    SANDBOX_MODES,
    SandboxModeError,
    SandboxModeService,
)
from app.deepspace.services.session_projections import (
    delegation_tree,
    tool_timeline,
    turn_boundary,
)
from app.deepspace.services.session_surfaces import (
    changed_file_projection,
    declared_deliverable_projection,
    goal_projection,
    job_projection,
    subagent_catalog_projection,
    trajectory_projection,
    workflow_run_projection,
)
from app.deepspace.services.tools.skill import workspace_skills
from app.deepspace.services.tools.team import TEAM_TOOLS
from app.deepspace.services.turn_queue import (
    DeepSpaceTurnQueueStore,
    QueueTurnEditConflictError,
    QueueTurnNotEditableError,
)
from app.deepspace.workers.tasks import dispatch_deepspace_turn_queue, run_deepspace_task
from app.platform.database.session import get_db, managed_db_session, set_db_tenant_context
from app.providers.services.reasoning_capabilities import normalize_reasoning_effort_value
from app.query.services.visual_blocks import validate_visual_block_payload
from app.realtime.event_bus import publish_event
from app.system.models.storage_lifecycle import StorageArchiveManifest, StorageLifecycleItem
from app.system.services.cache_service import get_redis_client
from app.system.services.rate_limit_service import RateLimitService
from app.system.services.storage_lifecycle import StorageLifecycleService
from app.system.services.storage_quota import StorageQuotaExceededError
from app.system.services.storage_service import StorageService

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-chats"])
logger = logging.getLogger(__name__)
CONVERSATION_KIND = "deepspace"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _record_cancellation_marker(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    client_request_id: str,
) -> None:
    """Best-effort fast cancellation signal for a live provider stream.

    The durable run and queue records are authoritative. Redis makes an
    in-flight provider read react faster, but an unavailable cache must never
    make a successfully persisted Stop request look like an HTTP failure.
    """
    client = None
    try:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        await client.set(
            cancellation_key(tenant_id, user_id, client_request_id),
            "1",
            ex=60 * 60 * 24,
        )
    except Exception:  # noqa: BLE001 - durable cancellation remains authoritative
        logger.warning(
            "DeepSpace cancellation marker could not be written; worker will poll durable state",
            exc_info=True,
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best-effort connection cleanup
                logger.debug("Unable to close DeepSpace cancellation Redis client", exc_info=True)


def _attachment_file_ids(value: Any) -> list[str]:
    """Normalize a small list of opaque Library references before queueing."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:10]:
        try:
            parsed = str(uuid.UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            raise ApiError(
                code="INVALID_ATTACHMENT",
                message="An attachment reference is invalid.",
                status_code=422,
            ) from None
        if parsed not in result:
            result.append(parsed)
    return result


def _regeneration_source_snapshot(
    db: Session,
    *,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
) -> tuple[str, list[str]]:
    """Read the owner-scoped prompt and attachments for one assistant reply."""
    set_db_tenant_context(db, auth.tenant_id)
    repo = DeepSpaceChatRepository(db)
    assistant = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=assistant_message_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    )
    if assistant is None or assistant.role != "assistant":
        raise ApiError(
            code="MESSAGE_NOT_FOUND", message="DeepSpace turn not found.", status_code=404
        )
    messages = list(
        repo.get_messages(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
    )
    assistant_index = next(
        (index for index, item in enumerate(messages) if item.id == assistant.id),
        -1,
    )
    if assistant_index < 0:
        raise ApiError(
            code="MESSAGE_NOT_FOUND", message="DeepSpace turn not found.", status_code=404
        )
    source_user = next(
        (item for item in reversed(messages[:assistant_index]) if item.role == "user"),
        None,
    )
    if source_user is None:
        raise ApiError(
            code="MESSAGE_NOT_FOUND",
            message="DeepSpace source prompt not found.",
            status_code=404,
        )
    prompt = str(
        source_user.active_version.content
        if source_user.active_version is not None
        else source_user.content
    )
    metadata = source_user.metadata_json if isinstance(source_user.metadata_json, dict) else {}
    attachments = metadata.get("attachments")
    attachment_ids = _attachment_file_ids(
        [item.get("id") for item in attachments if isinstance(item, dict) and item.get("id")]
        if isinstance(attachments, list)
        else []
    )
    return prompt, attachment_ids


def _dispatch_deepspace_queue(
    *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    """Best-effort wake-up; the durable queue poll retries broker failures."""
    try:
        dispatch_deepspace_turn_queue.apply_async(
            kwargs={
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to dispatch a DeepSpace queue; the durable queue will retry",
            extra={"tenant_id": str(tenant_id), "conversation_id": str(conversation_id)},
        )


async def _publish_conversation_event(
    *,
    auth: AuthContext,
    event_type: str,
    conversation_id: uuid.UUID,
    data: dict[str, Any] | None = None,
) -> None:
    """Best-effort realtime notification; REST remains authoritative."""
    try:
        await publish_event(
            get_settings(),
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            event_type=event_type,
            resource="conversations",
            data={"conversation_id": str(conversation_id), **(data or {})},
        )
    except Exception:  # noqa: BLE001
        logger.warning("Failed to publish conversation realtime event", exc_info=True)


@router.get(
    "/operational-summary",
    response_model=dict[str, Any],
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def operational_summary(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tenant/user-scoped, redacted provider latency and failure summary."""
    return DeepSpaceRequestMetricsRepository(db).summary(
        tenant_id=auth.tenant_id, user_id=auth.user_id
    )


def _safe_history_metadata(value: Any) -> dict[str, Any]:
    """Never return legacy raw reasoning from a history/reload response."""
    metadata = dict(value or {})
    # Continuation blobs are encrypted provider protocol data, never history
    # or activity content for a client.
    metadata.pop("_deepspace_provider_continuations", None)
    thinking = metadata.get("thinking")
    if not isinstance(thinking, dict):
        return metadata
    if metadata.get("thinking_redaction_version") != REASONING_REDACTION_VERSION:
        metadata.pop("thinking", None)
        return metadata
    metadata["thinking"] = {
        **thinking,
        "content": redact_reasoning_text(str(thinking.get("content") or "")),
    }
    return metadata


def _serialize_message(message: Any) -> MessageSchema:
    versions = [
        MessageVersionSchema.model_validate(item).model_copy(
            update={"metadata_json": _safe_history_metadata(item.metadata_json)}
        )
        for item in message.versions
    ]
    active_version = message.active_version
    return MessageSchema(
        id=message.id,
        role=message.role,
        content=(active_version.content if active_version is not None else message.content),
        metadata_json=_safe_history_metadata(
            active_version.metadata_json if active_version is not None else message.metadata_json
        ),
        created_at=message.created_at,
        active_version_id=message.active_version_id,
        active_version_index=(active_version.version_index if active_version is not None else 1),
        version_count=max(len(versions), 1),
        versions=versions,
    )


_VISUAL_FENCE_CONTENT_RE = re.compile(
    r"(?P<open>```(?P<kind>chart|card|graph|gallery)[^\r\n]*\r?\n)"
    r"(?P<body>[\s\S]*?)(?P<close>\r?\n```)",
    re.IGNORECASE,
)


def _replace_visual_fence_content(content: str, *, block_id: str, block: dict[str, Any]) -> str:
    """Refresh the Markdown source for a visual edit so chat reloads its model."""
    source_markdown = block.get("source_markdown")

    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return match.group(0)
        if not isinstance(parsed, dict):
            return match.group(0)
        id_matches = parsed.get("id") == block_id or parsed.get("block_id") == block_id
        source_matches = (
            isinstance(source_markdown, str) and body.strip() == source_markdown.strip()
        )
        if not id_matches and not source_matches:
            return match.group(0)
        updated = dict(block)
        updated["id"] = block_id
        updated["block_id"] = block_id
        # Once the canonical id is present, subsequent edits find the block by
        # id. Keeping the original source string would become stale.
        updated["source_markdown"] = None
        return f"{match.group('open')}{json.dumps(updated, ensure_ascii=False, indent=2)}{match.group('close')}"

    return _VISUAL_FENCE_CONTENT_RE.sub(replace, content, count=1)


@router.patch(
    "/{conversation_id}/messages/{message_id}/visual-block",
    response_model=MessageSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def update_visual_block(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: VisualBlockUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MessageSchema:
    """Persist an interactive chart/diagram/card edit as a message version.

    Visual edits use the same immutable message-version mechanism as text edits.
    The optional revision guard prevents an older browser tab from overwriting a
    newer edit after a reload or cross-device change.
    """

    try:
        validate_visual_block_payload(payload.block)
    except ValueError as exc:
        raise ApiError(code="INVALID_REQUEST", message=str(exc), status_code=422) from exc
    repo = DeepSpaceChatRepository(db)
    conversation = repo.get_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        for_update=True,
    )
    if conversation is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    message = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if message is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    if message.role != "assistant":
        raise ApiError(
            code="INVALID_MESSAGE_ROLE",
            message="Only assistant visual blocks can be edited.",
            status_code=400,
        )
    metadata = dict(message.metadata_json or {})
    stored = metadata.get("visual_blocks")
    visual_blocks = dict(stored) if isinstance(stored, dict) else {}
    current = visual_blocks.get(payload.block_id)
    current_record = dict(current) if isinstance(current, dict) else {}
    current_revision = int(current_record.get("revision") or 0)
    if payload.expected_revision is not None and payload.expected_revision != current_revision:
        raise ApiError(
            code="VISUAL_BLOCK_EDIT_CONFLICT",
            message="This visual block changed in another session. Reload and apply your edit again.",
            status_code=409,
            details={"revision": current_revision, "block_id": payload.block_id},
        )
    next_revision = current_revision + 1
    previous_history = current_record.get("history")
    history = list(previous_history) if isinstance(previous_history, list) else []
    history.append({"revision": next_revision, "source": "chat", "at": str(monotonic())})
    next_block = {
        **payload.block,
        "id": payload.block.get("id") or payload.block_id,
        "block_id": payload.block_id,
        "revision": next_revision,
        "schema_version": int(payload.block.get("schema_version") or 1),
        "history": history,
    }
    visual_blocks[payload.block_id] = {
        "revision": next_revision,
        "schema_version": int(payload.block.get("schema_version") or 1),
        "block": next_block,
        "history": history,
    }
    next_metadata = {**metadata, "visual_blocks": visual_blocks}
    content = (
        message.active_version.content if message.active_version is not None else message.content
    )
    content = _replace_visual_fence_content(
        content,
        block_id=payload.block_id,
        block=next_block,
    )
    updated = repo.create_message_version(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=auth.user_id,
        content=content,
        source_type="visual_edit",
        activate=True,
        metadata_json=next_metadata,
    )
    if updated is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    db.commit()
    refreshed = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if refreshed is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    await _publish_conversation_event(
        auth=auth,
        event_type="conversation.visual_block_updated",
        conversation_id=conversation_id,
        data={
            "message_id": str(message_id),
            "block_id": payload.block_id,
            "revision": next_revision,
        },
    )
    return _serialize_message(refreshed)


@router.patch(
    "/{conversation_id}/visual-block",
    response_model=MessageSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def update_visual_block_by_id(
    conversation_id: uuid.UUID,
    payload: VisualBlockUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MessageSchema:
    """Update a visual block when a note/editor only knows its stable block id.

    The lookup is owner and tenant scoped, and the same revision guard used by
    the message-addressed endpoint prevents stale browser tabs from winning.
    """
    repo = DeepSpaceChatRepository(db)
    conversation = repo.get_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        for_update=True,
    )
    if conversation is None:
        raise ApiError(
            code="VISUAL_BLOCK_NOT_FOUND",
            message="The visual block is no longer available in this conversation.",
            status_code=404,
        )
    _conversation_messages = repo.get_messages(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    )
    for item in reversed(list(_conversation_messages)):
        if item.role != "assistant":
            continue
        metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
        stored = metadata.get("visual_blocks")
        if isinstance(stored, dict) and payload.block_id in stored:
            return await update_visual_block(
                conversation_id=conversation_id,
                message_id=item.id,
                payload=payload,
                auth=auth,
                db=db,
            )
        active = item.active_version.content if item.active_version is not None else item.content
        structured_blocks = metadata.get("blocks")
        has_structured_block = isinstance(structured_blocks, list) and any(
            isinstance(block, dict)
            and (block.get("id") == payload.block_id or block.get("block_id") == payload.block_id)
            for block in structured_blocks
        )
        source_markdown = payload.block.get("source_markdown")
        source_is_present = isinstance(source_markdown, str) and source_markdown.strip() in str(
            active
        )
        if has_structured_block or source_is_present or payload.block_id in str(active):
            return await update_visual_block(
                conversation_id=conversation_id,
                message_id=item.id,
                payload=payload,
                auth=auth,
                db=db,
            )
    raise ApiError(
        code="VISUAL_BLOCK_NOT_FOUND",
        message="The visual block is no longer available in this conversation.",
        status_code=404,
    )


async def _authenticate_websocket_auth_context(
    websocket: Any,
    *,
    db: Session,
    settings: Settings,
) -> AuthContext:
    """Shared authentication helper for client storage and collection sockets."""
    ticket = str(websocket.query_params.get("ticket") or "").strip()
    if ticket:
        raw = None
        try:
            redis_client = get_redis_client()
            key = f"collection_ws_ticket:{ticket}"
            raw = redis_client.get(key)
            if raw is not None:
                redis_client.delete(key)
        except Exception:  # noqa: BLE001
            logger.warning("Collection WebSocket ticket lookup failed", exc_info=True)
        if raw is None:
            raise ApiError(
                code="AUTH_REQUIRED",
                message="The WebSocket ticket is invalid or expired.",
                status_code=401,
            )
        try:
            payload = json.loads(raw)
            return AuthContext(
                user_id=uuid.UUID(str(payload["user_id"])),
                tenant_id=uuid.UUID(str(payload["tenant_id"])),
                roles=frozenset(str(role) for role in payload.get("roles", [])),
                permissions=frozenset(
                    str(permission) for permission in payload.get("permissions", [])
                ),
                token_id=str(payload.get("token_id") or "collection-ws-ticket"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError(
                code="AUTH_REQUIRED",
                message="The WebSocket ticket is invalid.",
                status_code=401,
            ) from exc
    token = str(websocket.query_params.get("token") or "").strip()
    tenant_id = str(websocket.query_params.get("tenant_id") or "").strip() or None

    is_sqlite = False
    try:
        bind = getattr(db, "bind", None) or db.get_bind()
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"
    except Exception:
        pass

    if is_sqlite and (not token or token in {"local-token", "null", "undefined"}):
        local_tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        local_user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        return AuthContext(
            user_id=local_user_id,
            tenant_id=local_tenant_id,
            roles=frozenset(
                {"admin", "staff", "feedback_manager", "collection_moderator", "support_manager"}
            ),
            permissions=frozenset({"*"}),
            token_id="local-token",
            auth_type="local",
        )

    if not token:
        raise ApiError(
            code="AUTH_REQUIRED",
            message="Bearer access token or API key is required.",
            status_code=401,
        )

    if token.startswith(API_KEY_PREFIX):
        from app.auth.repositories.api_keys import ApiKeysRepository

        repo = ApiKeysRepository(db)
        api_key = repo.get_by_hash(key_hash=repo.hash_key(token))
        if api_key is None:
            raise ApiError(
                code="INVALID_API_KEY",
                message="API key is invalid or revoked.",
                status_code=401,
            )
        requested_tenant_id = None
        if tenant_id:
            try:
                requested_tenant_id = uuid.UUID(tenant_id)
            except ValueError as exc:
                raise ApiError(
                    code="INVALID_TENANT_ID",
                    message="tenant_id must be a valid UUID.",
                    status_code=400,
                ) from exc
        return build_auth_context_from_api_key(
            api_key=api_key,
            requested_tenant_id=requested_tenant_id,
            db=db,
        )

    return build_auth_context_from_jwt(
        claims=decode_access_token(token, settings),
        x_tenant_id=tenant_id,
        db=db,
    )


def _require_websocket_permissions(auth: AuthContext) -> None:
    granted = resolve_permissions(
        roles=frozenset(auth.roles),
        direct_permissions=getattr(auth, "permissions", frozenset()),
    )
    if "queries:run" not in granted:
        raise ApiError(
            code="FORBIDDEN",
            message="Insufficient permissions for requested operation.",
            status_code=403,
            details={"missing_permissions": ["queries:run"]},
        )


@router.get(
    "",
    response_model=ConversationListResponse,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def list_conversations(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: bool = False,
) -> ConversationListResponse:
    from app.deepspace.integrations.client_proxy import client_proxy_registry

    if client_proxy_registry.is_storage_connected(str(auth.tenant_id), str(auth.user_id)):
        data = await client_proxy_registry.db_proxy_call(
            str(auth.tenant_id),
            str(auth.user_id),
            "db.chats.list_conversations",
            {
                "limit": limit,
                "offset": offset,
                "user_id": str(auth.user_id),
                "kind": CONVERSATION_KIND,
                "include_archived": include_archived,
            },
            channel="storage",
        )
        return ConversationListResponse(
            items=[ConversationSchema.model_validate(item) for item in data],
            total=len(data),
        )

    repo = DeepSpaceChatRepository(db)
    items = repo.list_conversations(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )
    return ConversationListResponse(
        items=[ConversationSchema.model_validate(item) for item in items],
        total=len(items),
    )


@router.get(
    "/archived",
    response_model=ConversationListResponse,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def list_archived_conversations(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListResponse:
    """List archived DeepSpace conversations without exposing other sources."""
    repo = DeepSpaceChatRepository(db)
    items = repo.list_archived_conversations(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        limit=limit,
        offset=offset,
    )
    return ConversationListResponse(
        items=[ConversationSchema.model_validate(item) for item in items],
        total=len(items),
    )


def _conversation_retention_item(
    db: Session, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> StorageLifecycleItem | None:
    return db.execute(
        select(StorageLifecycleItem).where(
            StorageLifecycleItem.tenant_id == tenant_id,
            StorageLifecycleItem.category == "chat_history",
            StorageLifecycleItem.source_type == "conversation",
            StorageLifecycleItem.source_id == str(conversation_id),
        )
    ).scalar_one_or_none()


def _conversation_retention_response(
    item: StorageLifecycleItem, *, restored_at: object | None = None
) -> ConversationRetentionSchema:
    # The caller may supply the restored timestamp through the item only when
    # needed; the archive manifest is queried by the route for full fidelity.
    return ConversationRetentionSchema(
        conversation_id=uuid.UUID(item.source_id),
        state=item.state,
        archived_at=item.archived_at,
        restored_at=restored_at if hasattr(restored_at, "isoformat") else None,
        pinned=bool(item.pinned),
        legal_hold=bool(item.legal_hold),
        admin_exempt=bool(item.admin_exempt),
        protection_reason=item.protection_reason,
    )


@router.get(
    "/retention/{conversation_id}",
    response_model=ConversationRetentionSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def get_conversation_retention(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationRetentionSchema:
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="NOT_FOUND", message="DeepSpace conversation not found.", status_code=404
        )
    item = _conversation_retention_item(
        db, tenant_id=auth.tenant_id, conversation_id=conversation_id
    )
    if item is None:
        raise ApiError(
            code="NOT_FOUND", message="Retention state is not available.", status_code=404
        )
    manifest = db.execute(
        select(StorageArchiveManifest).where(
            StorageArchiveManifest.tenant_id == auth.tenant_id,
            StorageArchiveManifest.lifecycle_item_id == item.id,
        )
    ).scalar_one_or_none()
    return _conversation_retention_response(
        item, restored_at=manifest.restored_at if manifest else None
    )


@router.post(
    "/{conversation_id}/restore",
    response_model=ConversationRetentionSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def restore_conversation(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationRetentionSchema:
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="NOT_FOUND", message="DeepSpace conversation not found.", status_code=404
        )
    item = _conversation_retention_item(
        db, tenant_id=auth.tenant_id, conversation_id=conversation_id
    )
    if item is None:
        raise ApiError(
            code="NOT_FOUND", message="Archived conversation was not found.", status_code=404
        )
    try:
        manifest = StorageLifecycleService(db).restore_item(
            tenant_id=auth.tenant_id,
            item_id=item.id,
            user_id=auth.user_id,
            is_admin=is_admin_role(auth.roles),
        )
        db.commit()
        return _conversation_retention_response(item, restored_at=manifest.restored_at)
    except ValueError as exc:
        db.rollback()
        raise ApiError(code="INVALID_REQUEST", message=str(exc), status_code=422) from exc


@router.patch(
    "/{conversation_id}/retention/protection",
    response_model=ConversationRetentionSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def update_conversation_retention_protection(
    conversation_id: uuid.UUID,
    payload: ConversationRetentionProtectionRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationRetentionSchema:
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="NOT_FOUND", message="DeepSpace conversation not found.", status_code=404
        )
    if (payload.legal_hold is not None or payload.admin_exempt is not None) and not is_admin_role(
        auth.roles
    ):
        raise ApiError(
            code="FORBIDDEN",
            message="Only tenant admins can change legal holds or exemptions.",
            status_code=403,
        )
    item = _conversation_retention_item(
        db, tenant_id=auth.tenant_id, conversation_id=conversation_id
    )
    if item is None:
        raise ApiError(
            code="NOT_FOUND", message="Retention state is not available.", status_code=404
        )
    try:
        item = StorageLifecycleService(db).set_source_protection(
            tenant_id=auth.tenant_id,
            category="chat_history",
            source_type="conversation",
            source_id=str(conversation_id),
            pinned=payload.pinned,
            legal_hold=payload.legal_hold,
            admin_exempt=payload.admin_exempt,
            reason=payload.reason,
        )
        db.commit()
        return _conversation_retention_response(item)
    except ValueError as exc:
        db.rollback()
        raise ApiError(code="INVALID_REQUEST", message=str(exc), status_code=422) from exc


@router.post(
    "",
    response_model=ConversationSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def create_conversation(
    payload: ConversationCreateRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationSchema:
    from app.deepspace.integrations.client_proxy import client_proxy_registry

    data = payload or ConversationCreateRequest()
    if client_proxy_registry.is_storage_connected(str(auth.tenant_id), str(auth.user_id)):
        conversation = await client_proxy_registry.db_proxy_call(
            str(auth.tenant_id),
            str(auth.user_id),
            "db.chats.create_conversation",
            {
                "user_id": str(auth.user_id),
                "title": data.title,
                "content_html": data.content_html,
                "kind": CONVERSATION_KIND,
            },
            channel="storage",
        )
        repo = DeepSpaceChatRepository(db)
        conversation_id = uuid.UUID(str(conversation["id"]))
        if (
            repo.get_conversation(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                kind=CONVERSATION_KIND,
            )
            is None
        ):
            repo.create_conversation(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                kind=CONVERSATION_KIND,
                title=str(conversation.get("title") or data.title),
            )
            db.commit()
        result = ConversationSchema.model_validate(conversation)
        await _publish_conversation_event(
            auth=auth, event_type="conversation.created", conversation_id=conversation_id
        )
        return result

    repo = DeepSpaceChatRepository(db)
    conversation = repo.create_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        title=data.title,
        content_html=data.content_html,
    )
    db.commit()
    result = ConversationSchema.model_validate(conversation)
    await _publish_conversation_event(
        auth=auth, event_type="conversation.created", conversation_id=conversation.id
    )
    return result


@router.post(
    "/{conversation_id}/fork",
    response_model=ConversationSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def fork_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationForkRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ConversationSchema:
    """Fork a conversation, optionally copying files and history."""
    try:
        result = await get_conversation_fork_service(settings).fork(
            db,
            auth=auth,
            conversation_id=conversation_id,
            turn=payload.turn,
            message_id=payload.message_id,
            title=payload.title,
            copy_files=payload.copy_files,
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    new_conversation = DeepSpaceChatRepository(db).get_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=result.conversation_id,
    )
    if new_conversation is None:
        raise HTTPException(status_code=500, detail="Fork was created but could not be reloaded")
    result = ConversationSchema.model_validate(new_conversation)
    await _publish_conversation_event(
        auth=auth, event_type="conversation.forked", conversation_id=new_conversation.id
    )
    return result


@router.get(
    "/{conversation_id}/messages",
    response_model=ChatHistoryResponse,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def get_chat_history(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ChatHistoryResponse:
    from app.deepspace.integrations.client_proxy import (
        INTERACTIVE_STORAGE_RPC_TIMEOUT_SECONDS,
        client_proxy_registry,
    )

    proxied_messages: list[MessageSchema] | None = None
    if client_proxy_registry.is_storage_connected(str(auth.tenant_id), str(auth.user_id)):
        try:
            data = await client_proxy_registry.db_proxy_call(
                str(auth.tenant_id),
                str(auth.user_id),
                "db.chats.get_chat_history",
                {"conversation_id": str(conversation_id), "user_id": str(auth.user_id)},
                timeout=INTERACTIVE_STORAGE_RPC_TIMEOUT_SECONDS,
                channel="storage",
            )
            proxied_messages = []
            for item in data:
                message = MessageSchema.model_validate(item)
                proxied_messages.append(
                    message.model_copy(
                        update={
                            "metadata_json": _safe_history_metadata(message.metadata_json),
                            "versions": [
                                version.model_copy(
                                    update={
                                        "metadata_json": _safe_history_metadata(
                                            version.metadata_json
                                        )
                                    }
                                )
                                for version in message.versions
                            ],
                        }
                    )
                )
        except Exception:  # noqa: BLE001
            # A suspended browser-side storage proxy must not hide the
            # server's durable chat history. Fall through to PostgreSQL,
            # which also contains the run/event journal used for reconnect.
            logger.warning(
                "DeepSpace storage proxy history read failed; using server history",
                exc_info=True,
            )

    # A connected proxy can be alive but briefly return an empty snapshot while
    # the server-side worker has already persisted the turn. Prefer the local
    # durable snapshot in that case when the conversation exists here.
    if proxied_messages == []:
        local_repo = DeepSpaceChatRepository(db)
        if (
            local_repo.get_conversation(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                kind=CONVERSATION_KIND,
            )
            is not None
        ):
            proxied_messages = None

    if proxied_messages is None:
        repo = DeepSpaceChatRepository(db)
        conversation = repo.get_conversation(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
        if conversation is None:
            raise ApiError(
                code="CONVERSATION_NOT_FOUND",
                message="Conversation not found",
                status_code=404,
            )
        messages = repo.get_messages(
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            kind=CONVERSATION_KIND,
        )
    else:
        messages = None
    runtime = DeepSpaceRuntimeStore(db)
    serialized_messages = proxied_messages or []
    if messages is not None:
        serialized_messages = [_serialize_message(item) for item in messages]
    elif proxied_messages is not None:
        # The browser storage proxy can be briefly stale while the worker has
        # already committed its assistant row and event journal. Merge the
        # server copy into the proxy snapshot before enrichment so a reload
        # cannot hide a completed thought timeline behind an older local row.
        try:
            server_repo = DeepSpaceChatRepository(db)
            server_conversation = server_repo.get_conversation(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                kind=CONVERSATION_KIND,
            )
            if server_conversation is not None:
                server_messages = [
                    _serialize_message(item)
                    for item in server_repo.get_messages(
                        tenant_id=auth.tenant_id,
                        conversation_id=conversation_id,
                        user_id=auth.user_id,
                        kind=CONVERSATION_KIND,
                    )
                ]
                server_by_id = {str(item.id): item for item in server_messages}
                merged: list[MessageSchema] = []
                for proxy_message in proxied_messages:
                    durable = server_by_id.pop(str(proxy_message.id), None)
                    if durable is None:
                        merged.append(proxy_message)
                        continue
                    # Keep client-owned text/versions when present, but let
                    # the server win for durable assistant metadata and any
                    # content that the proxy has not received yet.
                    merged.append(
                        proxy_message.model_copy(
                            update={
                                "content": (
                                    durable.content
                                    if durable.role == "assistant" and durable.content.strip()
                                    else proxy_message.content
                                ),
                                "metadata_json": {
                                    **(proxy_message.metadata_json or {}),
                                    **(durable.metadata_json or {}),
                                },
                                "versions": proxy_message.versions or durable.versions,
                                "active_version_id": proxy_message.active_version_id
                                or durable.active_version_id,
                                "active_version_index": proxy_message.active_version_index
                                or durable.active_version_index,
                                "version_count": proxy_message.version_count
                                or durable.version_count,
                            }
                        )
                    )
                merged.extend(server_by_id.values())
                serialized_messages = sorted(
                    merged,
                    key=lambda item: (str(item.created_at), str(item.id)),
                )
        except Exception:  # noqa: BLE001
            # A proxy-backed conversation may intentionally not exist in the
            # server database. Its local snapshot remains a valid fallback.
            logger.debug("DeepSpace durable history merge skipped", exc_info=True)
    for serialized in serialized_messages:
        if serialized.role == "assistant":
            try:
                assistant_message_id = uuid.UUID(str(serialized.id))
            except (TypeError, ValueError):
                # A malformed proxy record must not prevent the rest of the
                # conversation from loading.
                continue
            live_run = runtime.live_worker_run_for_message(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
            )
            if serialized.metadata_json.get("status") == "streaming":
                serialized.metadata_json = {
                    **serialized.metadata_json,
                    "runtime_active": live_run is not None,
                }
                if live_run is None:
                    serialized.metadata_json["status"] = "ready"
                    serialized.metadata_json["runtime_state"] = "expired"
            durable_steps = runtime.history_steps_for_message(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
            )
            if durable_steps:
                serialized.metadata_json = {
                    **serialized.metadata_json,
                    "agent_steps": durable_steps,
                }
            request_ids = [
                str(serialized.metadata_json.get("client_request_id") or "").strip(),
                str(serialized.metadata_json.get("retry_of_request_id") or "").strip(),
            ]
            request_ids = list(
                dict.fromkeys(request_id for request_id in request_ids if request_id)
            )
            if request_ids:
                durable_events = []
                for request_id in request_ids:
                    durable_events.extend(
                        load_events(
                            db,
                            tenant_id=auth.tenant_id,
                            user_id=auth.user_id,
                            conversation_id=conversation_id,
                            client_request_id=request_id,
                        )
                    )
                ordered_events = timeline_events(
                    sorted(durable_events, key=lambda event: event.created_at)
                )
                if ordered_events:
                    serialized.metadata_json = {
                        **serialized.metadata_json,
                        "timeline_events": ordered_events,
                    }
        # The list is already populated for both the proxy and PostgreSQL
        # paths; this loop only enriches each message in place.
    return ChatHistoryResponse(messages=serialized_messages)


@router.patch(
    "/{conversation_id}",
    response_model=ConversationSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationSchema:
    repo = DeepSpaceChatRepository(db)
    if payload.title is None and payload.content_html is None:
        raise ApiError(
            code="INVALID_REQUEST",
            message="A title or note body is required.",
            status_code=400,
        )
    updated = repo.update_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        title=payload.title,
        content_html=payload.content_html,
        kind=CONVERSATION_KIND,
    )
    if not updated:
        raise ApiError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation not found",
            status_code=404,
        )
    db.commit()
    conversation = repo.get_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    )
    if conversation is None:
        raise ApiError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation not found after update",
            status_code=404,
        )
    result = ConversationSchema.model_validate(conversation)
    await _publish_conversation_event(
        auth=auth, event_type="conversation.updated", conversation_id=conversation_id
    )
    return result


@router.post(
    "/{conversation_id}/append-content",
    response_model=ConversationSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def append_conversation_content(
    conversation_id: uuid.UUID,
    payload: ConversationAppendContentRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ConversationSchema:
    """Append document text to the current user's active DeepSpace note."""
    repo = DeepSpaceChatRepository(db)
    conversation = repo.append_conversation_content(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        content_html=payload.content_html,
        title=payload.title,
        kind=CONVERSATION_KIND,
    )
    if conversation is None:
        raise ApiError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation not found",
            status_code=404,
        )
    db.commit()
    result = ConversationSchema.model_validate(conversation)
    await _publish_conversation_event(
        auth=auth, event_type="conversation.updated", conversation_id=conversation_id
    )
    return result


@router.delete(
    "/{conversation_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    repo = DeepSpaceChatRepository(db)
    conversation = repo.get_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
        for_update=True,
    )
    if conversation is None:
        raise ApiError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation not found",
            status_code=404,
        )

    DeepSpaceRuntimeStore(db).request_cancel(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        commit=False,
    )
    DeepSpaceTurnQueueStore(db).cancel_all_for_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        commit=False,
    )
    DeepSpaceContextCache().invalidate_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if not repo.delete_conversation(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    ):
        raise ApiError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation not found",
            status_code=404,
        )
    db.commit()
    StorageService(settings).prune_unreferenced_blobs(tenant_id=auth.tenant_id)
    await _publish_conversation_event(
        auth=auth, event_type="conversation.deleted", conversation_id=conversation_id
    )
    return Response(status_code=204)


@router.post(
    "/{conversation_id}/cancel",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def cancel_deepspace_chat(
    conversation_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    request_id = ""
    try:
        payload = await request.json()
        request_id = str(payload.get("client_request_id") or "").strip()
    except Exception:  # noqa: BLE001, B110 - malformed optional JSON is treated as no request id
        request_id = ""
    cancelled = DeepSpaceRuntimeStore(db).request_cancel(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if request_id:
        cancelled = (
            DeepSpaceTurnQueueStore(db).request_cancel(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                client_request_id=request_id,
            )
            or cancelled
        )
    if request_id:
        await _record_cancellation_marker(
            settings=settings,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            client_request_id=request_id,
        )
    if cancelled:
        # This is harmless while a worker is still running (the active-run
        # guard prevents a second claim), but it is required when cancellation
        # releases a paused approval/question turn whose worker already ended.
        try:
            dispatch_deepspace_turn_queue.apply_async(
                kwargs={
                    "tenant_id": str(auth.tenant_id),
                    "user_id": str(auth.user_id),
                    "conversation_id": str(conversation_id),
                }
            )
        except Exception:  # noqa: BLE001 - persisted worker sees the cancellation flag
            logger.exception(
                "DeepSpace cancellation dispatch failed after durable cancellation",
                extra={"conversation_id": str(conversation_id)},
            )
    return Response(
        status_code=204,
        headers={"X-DeepSpace-Cancel-Requested": "1" if cancelled else "0"},
    )


@router.post(
    "/queued/{client_request_id}/cancel",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def cancel_queued_deepspace_run(
    client_request_id: str,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Response:
    """Cancel a queued run before its worker has created a conversation run row."""
    queue_store = DeepSpaceTurnQueueStore(db)
    conversation_id = queue_store.conversation_id_for_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        client_request_id=client_request_id,
    )
    cancelled = queue_store.request_cancel_by_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        client_request_id=client_request_id,
    )
    await _record_cancellation_marker(
        settings=settings,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        client_request_id=client_request_id,
    )
    if cancelled and conversation_id is not None:
        try:
            dispatch_deepspace_turn_queue.apply_async(
                kwargs={
                    "tenant_id": str(auth.tenant_id),
                    "user_id": str(auth.user_id),
                    "conversation_id": str(conversation_id),
                }
            )
        except Exception:  # noqa: BLE001 - queue cancellation is already durable
            logger.exception(
                "DeepSpace queued cancellation dispatch failed after durable cancellation",
                extra={"conversation_id": str(conversation_id)},
            )
    return Response(
        status_code=204,
        headers={"X-DeepSpace-Cancel-Requested": "1" if cancelled else "0"},
    )


@router.get(
    "/{conversation_id}/queue/state",
    response_model=QueueStateSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_deepspace_queue_state(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> QueueStateSchema:
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="CONVERSATION_NOT_FOUND", message="Conversation not found", status_code=404
        )
    queue_store = DeepSpaceTurnQueueStore(db)
    # The state reflects reality, so a claim whose worker died is reconciled
    # before the state is reported.
    queue_store.recover_stale_claims(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    state = queue_store.state(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    # A paused queue never starts the work behind it.
    next_request_id = (
        queue_store.next_queued_request_id(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        if not state.paused
        else None
    )
    if next_request_id:
        _dispatch_deepspace_queue(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    active_request_id = queue_store.active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    ) or DeepSpaceRuntimeStore(db).active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueueStateSchema(
        paused=state.paused,
        reason=state.reason,
        failed_request_id=state.failed_request_id,
        active_request_id=active_request_id,
        paused_at=state.paused_at,
    )


@router.post(
    "/{conversation_id}/queue/pause",
    response_model=QueueStateSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def pause_deepspace_queue(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> QueueStateSchema:
    queue_store = DeepSpaceTurnQueueStore(db)
    queue_store.pause(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    active_request_id = queue_store.active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if active_request_id:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.set(
                cancellation_key(auth.tenant_id, auth.user_id, active_request_id),
                "1",
                ex=60 * 60 * 24,
            )
        finally:
            await client.close()
        DeepSpaceRuntimeStore(db).request_cancel(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        queue_store.request_cancel(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            client_request_id=active_request_id,
        )
    state = queue_store.state(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueueStateSchema(
        paused=state.paused,
        reason=state.reason,
        failed_request_id=state.failed_request_id,
        paused_at=state.paused_at,
    )


@router.post(
    "/{conversation_id}/queue/resume",
    response_model=QueueStateSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def resume_deepspace_queue(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> QueueStateSchema:
    queue_store = DeepSpaceTurnQueueStore(db)
    queue_store.recover_stale_claims(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    state = queue_store.resume(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    next_request_id = (
        queue_store.next_queued_request_id(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        if not state.paused
        else None
    )
    if next_request_id:
        _dispatch_deepspace_queue(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    active_request_id = queue_store.active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    ) or DeepSpaceRuntimeStore(db).active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueueStateSchema(
        paused=state.paused,
        reason=state.reason,
        failed_request_id=state.failed_request_id,
        active_request_id=active_request_id,
        paused_at=state.paused_at,
    )


@router.get(
    "/{conversation_id}/queue",
    response_model=list[QueuedTurnSchema],
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_deepspace_turn_queue(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[QueuedTurnSchema]:
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="CONVERSATION_NOT_FOUND", message="Conversation not found", status_code=404
        )
    queue_store = DeepSpaceTurnQueueStore(db)
    recovered = queue_store.recover_stale_claims(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if (
        recovered
        and not queue_store.state(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        ).paused
    ):
        _dispatch_deepspace_queue(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    return [
        QueuedTurnSchema.model_validate(turn)
        for turn in queue_store.list_open(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    ]


@router.post(
    "/{conversation_id}/queue/clear",
    response_model=QueueStateSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def clear_deepspace_queue(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> QueueStateSchema:
    """Clear pending queue work without deleting conversation messages.

    A currently running turn receives the normal cancellation signal; queued,
    paused, failed, and retry-checkpoint rows are retained as audit history but
    become terminal and disappear from the visible queue.
    """
    repo = DeepSpaceChatRepository(db)
    if (
        repo.get_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            kind=CONVERSATION_KIND,
        )
        is None
    ):
        raise ApiError(
            code="CONVERSATION_NOT_FOUND", message="Conversation not found", status_code=404
        )

    queue_store = DeepSpaceTurnQueueStore(db)
    active_request_id = queue_store.active_request_id(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if active_request_id:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.set(
                cancellation_key(auth.tenant_id, auth.user_id, active_request_id),
                "1",
                ex=60 * 60 * 24,
            )
        finally:
            await client.close()
        DeepSpaceRuntimeStore(db).request_cancel(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            commit=False,
        )

    queue_store.clear_for_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if active_request_id:
        dispatch_deepspace_turn_queue.apply_async(
            kwargs={
                "tenant_id": str(auth.tenant_id),
                "user_id": str(auth.user_id),
                "conversation_id": str(conversation_id),
            }
        )
    state = queue_store.state(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueueStateSchema(
        paused=state.paused,
        reason=state.reason,
        failed_request_id=state.failed_request_id,
        paused_at=state.paused_at,
    )


@router.post(
    "/{conversation_id}/queue",
    response_model=QueuedTurnSchema,
    status_code=202,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def enqueue_deepspace_turn(
    conversation_id: uuid.UUID,
    payload: QueueTurnRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> QueuedTurnSchema:
    RateLimitService(settings).enforce_deepspace_user_limit(
        request=request, user_id=str(auth.user_id)
    )
    request_id = str(payload.client_request_id or "").strip() or str(uuid.uuid4())
    prompt = payload.message
    attachment_file_ids = [str(file_id) for file_id in payload.attachment_file_ids]
    if payload.regenerate_message_id is not None:
        prompt, attachment_file_ids = _regeneration_source_snapshot(
            db,
            auth=auth,
            conversation_id=conversation_id,
            assistant_message_id=payload.regenerate_message_id,
        )
    try:
        turn = DeepSpaceTurnQueueStore(db).enqueue(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            client_request_id=request_id,
            prompt=prompt,
            regenerate_message_id=payload.regenerate_message_id,
            thinking_enabled=payload.thinking_enabled,
            reasoning_effort=payload.reasoning_effort,
            roles=sorted(auth.roles),
            permissions=sorted(auth.permissions),
            steer=payload.steer,
            attachment_file_ids=attachment_file_ids,
        )
    except StorageQuotaExceededError as exc:
        raise ApiError(
            code="STORAGE_QUOTA_EXCEEDED",
            message="Your workspace storage limit has been reached.",
            status_code=413,
            details={
                "plan": exc.plan.id,
                "storage_limit_bytes": exc.plan.storage_limit_bytes,
                "usage_bytes": exc.usage_bytes,
                "requested_bytes": exc.requested_bytes,
            },
        ) from exc
    except ValueError as exc:
        raise ApiError(code="CONVERSATION_NOT_FOUND", message=str(exc), status_code=404) from exc
    except OverflowError as exc:
        raise ApiError(code="DEEPSPACE_QUEUE_FULL", message=str(exc), status_code=409) from exc
    _dispatch_deepspace_queue(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueuedTurnSchema.model_validate(turn)


@router.patch(
    "/{conversation_id}/queue/{client_request_id}",
    response_model=QueuedTurnSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def edit_deepspace_queued_turn(
    conversation_id: uuid.UUID,
    client_request_id: str,
    payload: QueueTurnEditRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> QueuedTurnSchema:
    """Update a still-pending prompt while preserving its durable queue slot."""
    RateLimitService(settings).enforce_deepspace_user_limit(
        request=request, user_id=str(auth.user_id)
    )
    queue_store = DeepSpaceTurnQueueStore(db)
    try:
        turn = queue_store.update_queued_prompt(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            expected_prompt=payload.expected_prompt,
            prompt=payload.message,
        )
    except StorageQuotaExceededError as exc:
        raise ApiError(
            code="STORAGE_QUOTA_EXCEEDED",
            message="Your workspace storage limit has been reached.",
            status_code=413,
            details={
                "plan": exc.plan.id,
                "storage_limit_bytes": exc.plan.storage_limit_bytes,
                "usage_bytes": exc.usage_bytes,
                "requested_bytes": exc.requested_bytes,
            },
        ) from exc
    except QueueTurnEditConflictError as exc:
        raise ApiError(code="QUEUE_TURN_EDIT_CONFLICT", message=str(exc), status_code=409) from exc
    except QueueTurnNotEditableError as exc:
        raise ApiError(code="QUEUE_TURN_NOT_EDITABLE", message=str(exc), status_code=409) from exc
    except ValueError as exc:
        raise ApiError(code="CONVERSATION_NOT_FOUND", message=str(exc), status_code=404) from exc
    if turn is None:
        raise ApiError(
            code="QUEUE_TURN_NOT_FOUND", message="Queued prompt not found.", status_code=404
        )
    return QueuedTurnSchema.model_validate(turn)


@router.post(
    "/{conversation_id}/queue/{client_request_id}/steer",
    response_model=QueuedTurnSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def steer_deepspace_queued_turn(
    conversation_id: uuid.UUID,
    client_request_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> QueuedTurnSchema:
    runtime_store = DeepSpaceRuntimeStore(db)
    runtime_store.request_cancel(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    runtime_store.finish_requested_cancellations(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    queue_store = DeepSpaceTurnQueueStore(db)
    queue_store.recover_stale_claims(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    turn = queue_store.promote(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        client_request_id=client_request_id,
    )
    if turn is None:
        raise ApiError(
            code="QUEUE_TURN_NOT_EDITABLE",
            message=(
                "This message has already started or is no longer pending. "
                "Refresh the queue before steering it."
            ),
            status_code=409,
        )
    if queue_store.state(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    ).paused:
        queue_store.resume(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    _dispatch_deepspace_queue(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return QueuedTurnSchema.model_validate(turn)


@router.post(
    "/{conversation_id}/queue/{client_request_id}/retry",
    response_model=QueuedTurnSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
def retry_deepspace_failed_turn(
    conversation_id: uuid.UUID,
    client_request_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    payload: RetryFailedTurnRequest | None = None,
) -> QueuedTurnSchema:
    queue_store = DeepSpaceTurnQueueStore(db)
    try:
        turn = queue_store.retry_failed(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            failed_request_id=client_request_id,
            client_request_id=payload.client_request_id if payload else None,
        )
    except StorageQuotaExceededError as exc:
        raise ApiError(
            code="STORAGE_QUOTA_EXCEEDED",
            message="Your workspace storage limit has been reached.",
            status_code=413,
            details={
                "plan": exc.plan.id,
                "storage_limit_bytes": exc.plan.storage_limit_bytes,
                "usage_bytes": exc.usage_bytes,
                "requested_bytes": exc.requested_bytes,
            },
        ) from exc
    except ValueError as exc:
        raise ApiError(code="QUEUE_TURN_NOT_FOUND", message=str(exc), status_code=404) from exc
    except OverflowError as exc:
        raise ApiError(code="DEEPSPACE_QUEUE_FULL", message=str(exc), status_code=409) from exc
    # A retry is an explicit user action, so the retried prompt runs even
    # while the queue is paused for the other turns.  The queue stays paused
    # for those, so a retry never silently starts the work behind it.
    _dispatch_deepspace_queue(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        client_request_id=turn.client_request_id,
    )
    return QueuedTurnSchema.model_validate(turn)


@router.post(
    "/{conversation_id}/approvals/{approval_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def resolve_deepspace_approval(
    conversation_id: uuid.UUID,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = DeepSpaceRuntimeStore(db).resolve_approval(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        approval_id=approval_id,
        decision=payload.decision,
    )
    if resolved is None:
        raise ApiError(
            code="APPROVAL_NOT_FOUND",
            message="Approval request not found.",
            status_code=404,
        )
    if resolved.get("status") == "already_resolved":
        raise ApiError(
            code="APPROVAL_ALREADY_RESOLVED",
            message="Approval request was already resolved.",
            status_code=409,
        )
    return {
        "approval_id": approval_id,
        "decision": payload.decision,
        "tool_name": resolved.get("tool_name"),
        "status": "resolved",
    }


@router.get(
    "/{conversation_id}/plan",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_deepspace_plan(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read the conversation's plan mode and its current plan."""
    service = PlanModeService(db)
    plan = service.current_plan(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return {
        "active": service.active(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        ),
        "plan": plan.as_dict() if plan else None,
    }


@router.put(
    "/{conversation_id}/plan/mode",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_deepspace_plan_mode(
    conversation_id: uuid.UUID,
    payload: PlanModeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Turn plan mode on or off for the conversation."""
    return PlanModeService(db).set_mode(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        active=payload.active,
    )


@router.post(
    "/{conversation_id}/plan/review",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def review_deepspace_plan(
    conversation_id: uuid.UUID,
    payload: PlanReviewRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record the user's decision about the conversation's proposed plan."""
    service = PlanModeService(db)
    plan = service.current_plan(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if plan is None:
        raise ApiError(
            code="PLAN_NOT_FOUND",
            message="This conversation has no plan to review.",
            status_code=404,
        )
    try:
        reviewed = service.review(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            plan_id=plan.id,
            decision=payload.decision,
            feedback=payload.feedback,
        )
    except PlanStoreError as exc:
        raise ApiError(code="PLAN_REVIEW_REJECTED", message=str(exc), status_code=409) from exc
    if payload.decision == "approved":
        # Approving the plan ends plan mode, so the next turn acts on the
        # plan the user just accepted.
        service.set_mode(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            active=False,
        )
    return {"plan": reviewed.as_dict(), "status": "resolved"}


@router.get(
    "/{conversation_id}/sandbox-mode",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_deepspace_sandbox_mode(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the sandbox mode the conversation runs in."""
    service = SandboxModeService(db)
    deployment_default = str(
        getattr(settings, "deepspace_sandbox_policy", None)
        or getattr(settings, "deepspace_permission_mode", "workspace-write")
    )
    recorded = service.recorded_mode(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return {
        "mode": service.effective_mode(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            deployment_default=deployment_default,
        ),
        "recorded_mode": recorded,
        "deployment_default": deployment_default,
        "available_modes": list(SANDBOX_MODES),
    }


@router.put(
    "/{conversation_id}/sandbox-mode",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_deepspace_sandbox_mode(
    conversation_id: uuid.UUID,
    payload: SandboxModeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Set the sandbox mode the conversation runs in."""
    try:
        return SandboxModeService(db).set_mode(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            mode=payload.mode,
        )
    except SandboxModeError as exc:
        raise ApiError(code="SANDBOX_MODE_REJECTED", message=str(exc), status_code=400) from exc


@router.post(
    "/bulk-delete",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def bulk_delete_conversations(
    payload: BulkDeleteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    runtime_store = DeepSpaceRuntimeStore(db)
    queue_store = DeepSpaceTurnQueueStore(db)
    for cid in payload.conversation_ids:
        runtime_store.request_cancel(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=cid,
            commit=False,
        )
        queue_store.cancel_all_for_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=cid,
            commit=False,
        )
    repo = DeepSpaceChatRepository(db)
    count = repo.bulk_delete_conversations(
        tenant_id=auth.tenant_id,
        conversation_ids=payload.conversation_ids,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    )
    db.commit()
    StorageService(settings).prune_unreferenced_blobs(tenant_id=auth.tenant_id)
    for conversation_id in payload.conversation_ids:
        await _publish_conversation_event(
            auth=auth, event_type="conversation.deleted", conversation_id=conversation_id
        )
    return Response(status_code=204, headers={"X-Deleted-Count": str(count)})


@router.delete(
    "/{conversation_id}/messages/{message_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def delete_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    repo = DeepSpaceChatRepository(db)
    message = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if message is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    if message.role != "assistant":
        raise ApiError(
            code="INVALID_MESSAGE_ROLE",
            message="Only assistant messages can be deleted.",
            status_code=400,
        )
    repo.delete_message(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    db.commit()
    return Response(status_code=204)


@router.patch(
    "/{conversation_id}/messages/{message_id}",
    response_model=MessageSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def edit_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: MessageEditRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MessageSchema:
    repo = DeepSpaceChatRepository(db)
    user_message, assistant_message = repo.get_latest_turn_pair(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        kind=CONVERSATION_KIND,
    )
    if user_message is None or assistant_message is None or user_message.id != message_id:
        raise ApiError(
            code="MESSAGE_EDIT_NOT_ALLOWED",
            message="Only the latest user message can be edited.",
            status_code=409,
        )
    repo.create_message_version(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        content=payload.content,
        metadata_json=user_message.metadata_json,
        source_type="user_edit",
        activate=True,
    )
    db.commit()
    message = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if message is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    return _serialize_message(message)


@router.patch(
    "/{conversation_id}/messages/{message_id}/versions/{version_id}/activate",
    response_model=MessageSchema,
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def activate_message_version(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    version_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MessageSchema:
    repo = DeepSpaceChatRepository(db)
    message = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if message is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    repo.activate_message_version(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        version_id=version_id,
        user_id=auth.user_id,
    )
    db.commit()
    refreshed = repo.get_message_by_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=CONVERSATION_KIND,
    )
    if refreshed is None:
        raise ApiError(code="MESSAGE_NOT_FOUND", message="Message not found.", status_code=404)
    return _serialize_message(refreshed)


@router.post(
    "/stream",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def stream_deepspace_chat(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    raw_payload = await request.json()
    if not isinstance(raw_payload, dict):
        raise ApiError(
            code="INVALID_CHAT_REQUEST", message="Invalid chat request.", status_code=400
        )
    return await _stream_deepspace_chat_payload(
        raw_payload=raw_payload, request=request, auth=auth, settings=settings
    )


async def _stream_deepspace_chat_payload(
    *, raw_payload: dict[str, Any], request: Request, auth: AuthContext, settings: Settings
) -> StreamingResponse:
    prompt = str(raw_payload.get("message", ""))
    conversation_id_raw = raw_payload.get("conversation_id")
    conversation_id = uuid.UUID(str(conversation_id_raw)) if conversation_id_raw else None
    regenerate_message_id_raw = str(raw_payload.get("regenerate_message_id") or "").strip()
    regenerate_message_id: uuid.UUID | None = None
    regeneration_attachment_ids: list[str] = []
    if regenerate_message_id_raw:
        try:
            regenerate_message_id = uuid.UUID(regenerate_message_id_raw)
        except ValueError:
            raise ApiError(
                code="MESSAGE_NOT_FOUND", message="DeepSpace turn not found.", status_code=404
            ) from None
        if conversation_id is None:
            raise ApiError(
                code="REGENERATE_CONVERSATION_REQUIRED",
                message="A conversation is required to regenerate this response.",
                status_code=400,
            )
        with managed_db_session() as source_db:
            prompt, regeneration_attachment_ids = _regeneration_source_snapshot(
                source_db,
                auth=auth,
                conversation_id=conversation_id,
                assistant_message_id=regenerate_message_id,
            )
    resume_approval_id = str(raw_payload.get("resume_approval_id") or "").strip() or None
    resume_user_question_id = str(raw_payload.get("resume_user_question_id") or "").strip() or None
    if (
        not prompt.strip()
        and not resume_approval_id
        and not resume_user_question_id
        and regenerate_message_id is None
    ):

        async def empty_stream() -> AsyncIterator[str]:
            yield sse(
                "error",
                {"code": "EMPTY_MESSAGE", "message": "Message cannot be empty."},
            )

        return StreamingResponse(
            empty_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )
    client_request_id = str(raw_payload.get("client_request_id") or "").strip() or str(uuid.uuid4())
    reconnect = bool(raw_payload.get("reconnect", False))
    try:
        after_sequence = max(0, int(raw_payload.get("after_sequence") or 0))
    except (TypeError, ValueError):
        after_sequence = 0
    thinking_enabled = bool(raw_payload.get("thinking_enabled", False))
    run_now = bool(raw_payload.get("run_now", False))
    reasoning_effort = normalize_reasoning_effort_value(
        str(raw_payload.get("reasoning_effort") or "").strip()
    )
    if run_now and conversation_id is not None:
        with managed_db_session() as run_now_db:
            set_db_tenant_context(run_now_db, auth.tenant_id)
            queue_store = DeepSpaceTurnQueueStore(run_now_db)
            queue_state = queue_store.state(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
            if not queue_state.paused:
                raise ApiError(
                    code="DEEPSPACE_QUEUE_NOT_PAUSED",
                    message="Run-now chat is available only while the queue is paused.",
                    status_code=409,
                )
            if queue_store.active_request_id(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            ):
                raise ApiError(
                    code="DEEPSPACE_QUEUE_STILL_RUNNING",
                    message="Wait for the active queued turn to stop before running a separate chat.",
                    status_code=409,
                )
    # Clarification answers are claimed synchronously, before a worker is
    # scheduled.  This is the ownership boundary: exactly one request may
    # advance an awaiting question; retries merely attach to that same run.
    question_claimed = False
    if not reconnect and not resume_approval_id and not resume_user_question_id:
        RateLimitService(settings).enforce_deepspace_user_limit(
            request=request,
            user_id=str(auth.user_id),
        )
        if conversation_id is not None:
            try:
                with managed_db_session() as queue_db:
                    set_db_tenant_context(queue_db, auth.tenant_id)
                    queue_store = DeepSpaceTurnQueueStore(queue_db)
                    queue_store.enqueue(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        client_request_id=client_request_id,
                        prompt=prompt,
                        regenerate_message_id=regenerate_message_id,
                        thinking_enabled=thinking_enabled,
                        reasoning_effort=reasoning_effort,
                        roles=sorted(auth.roles),
                        permissions=sorted(auth.permissions),
                        attachment_file_ids=(
                            regeneration_attachment_ids
                            if regenerate_message_id is not None
                            else _attachment_file_ids(raw_payload.get("attachment_file_ids"))
                        ),
                    )
            except StorageQuotaExceededError as exc:
                raise ApiError(
                    code="STORAGE_QUOTA_EXCEEDED",
                    message="Your workspace storage limit has been reached.",
                    status_code=413,
                    details={
                        "plan": exc.plan.id,
                        "storage_limit_bytes": exc.plan.storage_limit_bytes,
                        "usage_bytes": exc.usage_bytes,
                        "requested_bytes": exc.requested_bytes,
                    },
                ) from exc
            except ValueError as exc:
                raise ApiError(
                    code="CONVERSATION_NOT_FOUND", message=str(exc), status_code=404
                ) from exc
            except OverflowError as exc:
                raise ApiError(
                    code="DEEPSPACE_QUEUE_FULL", message=str(exc), status_code=409
                ) from exc
    elif conversation_id is not None and resume_user_question_id:
        with managed_db_session() as resume_db:
            set_db_tenant_context(resume_db, auth.tenant_id)
            runtime = DeepSpaceRuntimeStore(resume_db)
            claim = runtime.claim_user_question(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                question_id=resume_user_question_id,
                answer=prompt,
            )
            if claim is None:
                # A stale card must never create another worker or show a
                # provider failure. The browser will refresh durable history.
                active_request_id = DeepSpaceTurnQueueStore(resume_db).active_request_id(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                if active_request_id:
                    client_request_id = active_request_id
            elif claim.claimed:
                DeepSpaceChatRepository(resume_db).add_message(
                    tenant_id=auth.tenant_id,
                    conversation_id=conversation_id,
                    role="user",
                    content=prompt,
                    metadata_json={
                        "answer_to_question_id": resume_user_question_id,
                        "client_request_id": client_request_id,
                    },
                )
                resumed_request_id = DeepSpaceTurnQueueStore(resume_db).resume_paused(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    commit=False,
                )
                if resumed_request_id:
                    client_request_id = resumed_request_id
                    # A paused run deliberately keeps its request id. Start
                    # this new browser subscription after the old terminal
                    # `awaiting_user` frame; replaying it would immediately
                    # close the SSE reader before continuation events arrive.
                    after_sequence = max(
                        after_sequence,
                        latest_sequence(
                            resume_db,
                            tenant_id=auth.tenant_id,
                            user_id=auth.user_id,
                            conversation_id=conversation_id,
                            client_request_id=client_request_id,
                        ),
                    )
                question_claimed = True
                resume_db.commit()
            else:
                active_request_id = DeepSpaceTurnQueueStore(resume_db).active_request_id(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                if active_request_id:
                    client_request_id = active_request_id
    elif conversation_id is not None and resume_approval_id:
        # Keep the paused turn's stream key and queue slot when a user answers
        # a clarification or approves an action.  A new id would leave the
        # original paused record active forever and block following messages.
        with managed_db_session() as queue_db:
            set_db_tenant_context(queue_db, auth.tenant_id)
            resumed_request_id = DeepSpaceTurnQueueStore(queue_db).resume_paused(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
            if resumed_request_id:
                client_request_id = resumed_request_id
                # See the clarification-resume cursor above. Approval
                # continuations use the same durable stream and must not
                # replay their old terminal pause event into a new live
                # subscription.
                after_sequence = max(
                    after_sequence,
                    latest_sequence(
                        queue_db,
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        client_request_id=client_request_id,
                    ),
                )

    async def iterator() -> AsyncIterator[str]:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        last_sequence = after_sequence
        try:
            await pubsub.subscribe(channel_name(client_request_id))
            try:
                if (
                    conversation_id is not None
                    and not run_now
                    and not resume_approval_id
                    and not resume_user_question_id
                ):
                    dispatch_deepspace_turn_queue.apply_async(
                        kwargs={
                            "tenant_id": str(auth.tenant_id),
                            "user_id": str(auth.user_id),
                            "conversation_id": str(conversation_id),
                        }
                    )
                elif resume_user_question_id and not question_claimed:
                    # The answer was already accepted by another request.
                    # Subscribe/replay its durable stream; never execute a
                    # second continuation merely to report stale state.
                    yield sse(
                        "done",
                        {
                            "conversation_id": str(conversation_id),
                            "status": "running",
                            "reconnected": True,
                        },
                    )
                    return
                else:
                    run_deepspace_task.apply_async(
                        kwargs={
                            "tenant_id": str(auth.tenant_id),
                            "user_id": str(auth.user_id),
                            "roles": sorted(auth.roles),
                            "permissions": sorted(auth.permissions),
                            "conversation_id": (str(conversation_id) if conversation_id else None),
                            "prompt": prompt,
                            "client_request_id": client_request_id,
                            "thinking_enabled": thinking_enabled,
                            "reasoning_effort": reasoning_effort,
                            "attachment_file_ids": _attachment_file_ids(
                                raw_payload.get("attachment_file_ids")
                            ),
                            "resume_approval_id": resume_approval_id,
                            "resume_user_question_id": resume_user_question_id,
                        }
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to enqueue detached DeepSpace run")
                yield sse(
                    "error",
                    {
                        "code": "DEEPSPACE_QUEUE_UNAVAILABLE",
                        "message": "DeepSpace could not start this response. Please retry.",
                    },
                )
                return

            async def read_replay() -> list[tuple[int, str]]:
                """Read durable gaps without holding a DB session open."""
                if conversation_id is None:
                    return []
                # PostgreSQL is the replay source of truth. Materialize plain
                # tuples while the session is open; ORM rows are detached when
                # managed_db_session() exits.
                with managed_db_session() as replay_db:
                    stored = load_events(
                        replay_db,
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        client_request_id=client_request_id,
                        after_sequence=last_sequence,
                    )
                    replay_frames = frames_after(stored, after_sequence=last_sequence)
                return replay_frames

            terminal = False
            last_replay_at = 0.0
            replay_interval_seconds = 1.0
            replay_frames = await read_replay()
            last_replay_at = monotonic()
            for sequence, frame in replay_frames:
                last_sequence = sequence
                yield frame
                terminal = is_terminal_event(event_name_from_frame(frame))
                if terminal:
                    return

            while not terminal:
                # Redis is the low-latency path. A low-frequency durable gap
                # check preserves recovery if Redis drops a message while the
                # stream is otherwise quiet, without querying PostgreSQL for
                # every provider chunk.
                if (
                    conversation_id is not None
                    and monotonic() - last_replay_at >= replay_interval_seconds
                ):
                    replay_frames = await read_replay()
                    last_replay_at = monotonic()
                    for sequence, frame in replay_frames:
                        last_sequence = sequence
                        yield frame
                        terminal = is_terminal_event(event_name_from_frame(frame))
                        if terminal:
                            return
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    if (
                        conversation_id is not None
                        and monotonic() - last_replay_at >= replay_interval_seconds
                    ):
                        replay_frames = await read_replay()
                        last_replay_at = monotonic()
                        for sequence, frame in replay_frames:
                            last_sequence = sequence
                            yield frame
                            terminal = is_terminal_event(event_name_from_frame(frame))
                            if terminal:
                                return
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0)
                    continue
                decoded = decode_live_event(str(message.get("data") or ""))
                if decoded is None:
                    continue
                sequence, frame = decoded
                if sequence <= last_sequence:
                    continue
                if is_terminal_event(event_name_from_frame(frame)):
                    # Terminal frames are published only after durable flush by
                    # the worker. Replay any Redis gap before exposing the
                    # terminal frame to the browser.
                    replay_frames = await read_replay()
                    last_replay_at = monotonic()
                    for replay_sequence, replay_frame in replay_frames:
                        last_sequence = replay_sequence
                        yield replay_frame
                        terminal = is_terminal_event(event_name_from_frame(replay_frame))
                        if terminal:
                            return
                last_sequence = sequence
                yield frame
                terminal = is_terminal_event(event_name_from_frame(frame))
        finally:
            try:
                await pubsub.unsubscribe(channel_name(client_request_id))
                await pubsub.close()
                await redis_client.close()
            except Exception:  # noqa: BLE001
                logger.debug("DeepSpace detached stream cleanup failed", exc_info=True)

    return StreamingResponse(iterator(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post(
    "/{conversation_id}/messages/{message_id}/regenerate/stream",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def regenerate_message_stream(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: RegenerateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    return await _stream_deepspace_chat_payload(
        raw_payload={
            "message": "",
            "conversation_id": str(conversation_id),
            "client_request_id": payload.client_request_id,
            "thinking_enabled": payload.thinking_enabled,
            "reasoning_effort": payload.reasoning_effort,
            "regenerate_message_id": str(message_id),
        },
        request=request,
        auth=auth,
        settings=settings,
    )


@router.post(
    "/{conversation_id}/messages/{message_id}/edit-and-regenerate/stream",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def edit_and_regenerate_stream(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    raw_payload = await request.json()
    content = str(raw_payload.get("content", ""))
    client_request_id = str(raw_payload.get("client_request_id") or "").strip() or None
    repo = DeepSpaceChatRepository(db)
    edited = repo.create_message_version(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=auth.user_id,
        content=content,
    )
    if edited is None:
        raise ApiError(
            code="MESSAGE_NOT_FOUND",
            message="DeepSpace message not found.",
            status_code=404,
        )
    latest_user, assistant = repo.get_latest_turn_pair(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
    )
    if latest_user is None or assistant is None or latest_user.id != message_id:
        raise ApiError(
            code="MESSAGE_EDIT_NOT_ALLOWED",
            message="Only the latest user message can be edited.",
            status_code=409,
        )
    repo.create_message_version(
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        message_id=assistant.id,
        user_id=auth.user_id,
        content="",
        source_type="edit_regenerate",
        activate=True,
    )
    db.commit()
    service = DeepSpaceChatService(db=db, settings=settings)

    async def iterator() -> AsyncIterator[str]:
        async for chunk in service.stream_turn(
            auth=auth,
            conversation_id=conversation_id,
            prompt=content,
            existing_assistant_message_id=assistant.id,
            client_request_id=client_request_id,
            thinking_enabled=bool(raw_payload.get("thinking_enabled", True)),
            request=request,
        ):
            yield chunk

    return StreamingResponse(iterator(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/memory/search", response_model=dict[str, Any])
async def search_memories(
    query: str = Query(..., min_length=1),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.deepspace.memory.memory_service import MemoryService

    results = await MemoryService(db).search_memories(
        tenant_id=auth.tenant_id, user_id=auth.user_id, query=query
    )
    return {"results": results}


@router.get("/memory", response_model=list[MemoryFactSchema])
async def list_memories(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Any:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).list_all_memories(tenant_id=auth.tenant_id, user_id=auth.user_id)


@router.post("/memory", response_model=MemoryFactSchema, status_code=201)
async def write_memory(
    payload: MemoryWriteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.deepspace.memory.memory_service import MemoryService

    memory_id = await MemoryService(db).store_fact(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        key=payload.key,
        value=payload.value,
        scope=payload.scope,
        tags=payload.tags,
        importance_score=payload.importance_score,
        confidence_score=payload.confidence_score,
        source="manual_memory",
        metadata_json=payload.metadata,
    )
    memory = await MemoryService(db).get_memory(
        tenant_id=auth.tenant_id, user_id=auth.user_id, memory_id=memory_id
    )
    if memory is None:
        raise ApiError(
            code="MEMORY_NOT_FOUND",
            message="Memory was not available after saving.",
            status_code=500,
        )
    return memory


@router.get("/memory/preferences", response_model=MemoryPreferencesSchema)
async def get_memory_preferences(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).get_preferences(tenant_id=auth.tenant_id, user_id=auth.user_id)


@router.patch("/memory/preferences", response_model=MemoryPreferencesSchema)
async def update_memory_preferences(
    payload: MemoryPreferencesUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).update_preferences(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        automatic_capture_enabled=payload.automatic_capture_enabled,
        review_inferred_memories=payload.review_inferred_memories,
        memory_retrieval_enabled=payload.memory_retrieval_enabled,
    )


@router.patch("/memory/{memory_id}", response_model=MemoryFactSchema)
async def update_memory(
    memory_id: str,
    payload: MemoryUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.deepspace.memory.memory_service import MemoryService

    memory = await MemoryService(db).update_memory(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        memory_id=memory_id,
        value=payload.value,
        scope=payload.scope,
        tags=payload.tags,
        importance_score=payload.importance_score,
        confidence_score=payload.confidence_score,
        metadata_json=payload.metadata,
    )
    if memory is None:
        raise ApiError(code="MEMORY_NOT_FOUND", message="Memory not found.", status_code=404)
    return memory


@router.post("/memory/{memory_id}/approve", response_model=MemoryFactSchema)
async def approve_memory_candidate(
    memory_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.deepspace.memory.memory_service import MemoryService

    memory = await MemoryService(db).approve_memory_candidate(
        tenant_id=auth.tenant_id, user_id=auth.user_id, memory_id=memory_id
    )
    if memory is None:
        raise ApiError(
            code="MEMORY_CANDIDATE_NOT_FOUND",
            message="Memory candidate not found.",
            status_code=404,
        )
    return memory


@router.delete("/memory/{memory_id}/candidate", status_code=204)
async def reject_memory_candidate(
    memory_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    from app.deepspace.memory.memory_service import MemoryService

    deleted = await MemoryService(db).reject_memory_candidate(
        tenant_id=auth.tenant_id, user_id=auth.user_id, memory_id=memory_id
    )
    if not deleted:
        raise ApiError(
            code="MEMORY_CANDIDATE_NOT_FOUND",
            message="Memory candidate not found.",
            status_code=404,
        )
    return Response(status_code=204)


@router.delete("/memory/clear", status_code=204)
async def clear_personal_memory(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    from app.deepspace.memory.memory_service import MemoryService

    await MemoryService(db).clear_personal_memories(tenant_id=auth.tenant_id, user_id=auth.user_id)
    return Response(status_code=204)


@router.delete("/memory/{key}")
async def forget_memory(
    key: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    from app.deepspace.memory.memory_service import MemoryService

    success = await MemoryService(db).forget_memory(
        tenant_id=auth.tenant_id, user_id=auth.user_id, key=key
    )
    if not success:
        raise ApiError(
            code="MEMORY_NOT_FOUND",
            message=f"Memory key '{key}' not found.",
            status_code=404,
        )
    return Response(status_code=204)


@router.post("/memory/cleanup", response_model=dict[str, Any])
async def cleanup_memories(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Any:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).cleanup_duplicate_memories(
        tenant_id=auth.tenant_id, user_id=auth.user_id
    )


@router.post("/memory/cleanup-stale", response_model=dict[str, Any])
async def cleanup_stale_memories(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    retention_days: Annotated[int, Query(ge=1, le=3650)] = 7,
) -> Any:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).cleanup_stale_memories(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        retention_days=retention_days,
    )


@router.get("/memory/retention", response_model=MemoryRetentionReportSchema)
async def evaluate_memory_retention(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    retention_days: Annotated[int, Query(ge=1, le=3650)] = 7,
) -> Any:
    from app.deepspace.memory.memory_service import MemoryService

    return await MemoryService(db).evaluate_memory_retention(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        retention_days=retention_days,
    )


@router.get("/memory/evaluation", response_model=MemoryRetentionReportSchema)
async def evaluate_memory_quality(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    sample_queries: Annotated[list[str] | None, Query()] = None,
) -> MemoryRetentionReportSchema:
    from app.deepspace.memory.memory_service import MemoryService

    report = await MemoryService(db).evaluate_memory_quality(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        sample_queries=sample_queries or None,
    )
    retention_days = report.get("session_retention_days") or 7
    report["retention_policy"] = {
        "session_retention_days": retention_days,
        "decay_half_life_days": report.get("retention_policy", {}).get(
            "decay_half_life_days", 120.0
        ),
    }
    report.setdefault("session_retention_days", retention_days)
    return MemoryRetentionReportSchema.model_validate(report)


@router.get(
    "/{conversation_id}/projection",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_deepspace_projection(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    tool_limit: int = Query(default=200, ge=1, le=2_000),
) -> dict[str, Any]:
    """Read the conversation's turn, tool, and delegation read models.

    The three projections answer what the frontend needs without replaying
    every SSE frame: which turn and step are open, which tools ran, and which
    child agents exist under the conversation.
    """
    boundary = turn_boundary(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return {
        "turn_boundary": {
            "open_turn": boundary.open_turn,
            "last_turn": boundary.last_turn,
            "open_step": boundary.open_step,
            "last_completed_step": boundary.last_completed_step,
            "completed_turns": boundary.completed_turns,
        },
        "tool_timeline": [
            entry.as_dict()
            for entry in tool_timeline(
                db,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                limit=tool_limit,
            )
        ],
        "delegation_tree": delegation_tree(
            db,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        ),
    }


@router.get(
    "/{conversation_id}/agent-team",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_agent_team(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    message_limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """Read the live named team and delegated children for one conversation.

    The endpoint is a read model for the DeepSpace Agent Team panel.  It uses
    the same durable team service as the model-facing tools and keeps the
    delegated-child catalogue alongside members and the shared task board.
    It does not create members or dispatch work; those actions remain owned by
    the team tools and their worker lifecycle.

    Args:
        conversation_id: The conversation whose team is shown.
        auth: The authenticated tenant and user.
        db: The request database session.
        message_limit: The number of recent team messages to include.

    Returns:
        The team feature state, lead descriptor, named members, messages,
        tasks, and delegated child runs.
    """
    service = AgentTeamService(db)
    members = service.list_members(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    tasks = service.list_tasks(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    messages = service.list_messages(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        limit=message_limit,
    )
    children = subagent_catalog_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    settings = get_settings()
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or "standard")
    preset_id, custom_settings = PresetStateService(db).effective_preset_entry(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        deployment_default=deployment_default,
    )
    resolution = resolve_preset(
        preset_id,
        settings=settings,
        custom_settings=custom_settings,
    )
    allowed_tools = resolution.tool_allowlist
    team_tools = [
        {
            "name": tool.name,
            "allowed": allowed_tools is None or tool.name in allowed_tools,
        }
        for tool in TEAM_TOOLS
    ]
    return {
        "enabled": service.is_team_enabled(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            deployment_default=bool(getattr(settings, "deepspace_agent_team_enabled", True)),
        ),
        "preset": preset_id,
        "team_tools": team_tools,
        "team_tools_available": all(tool["allowed"] for tool in team_tools),
        "max_members": int(getattr(settings, "deepspace_agent_team_max_members", 8)),
        "lead": {"name": "lead", "label": "DeepSpace lead", "kind": "lead"},
        "members": [member.as_dict() for member in members],
        "messages": [message.as_dict() for message in messages],
        "tasks": [task.as_dict() for task in tasks],
        "children": children["children"],
        "counts": {
            "members": len(members),
            "running_members": sum(member.state == "running" for member in members),
            "tasks": len(tasks),
            "active_tasks": sum(task.state in {"pending", "in_progress"} for task in tasks),
            "children": children["counts"],
        },
    }


@router.patch(
    "/{conversation_id}/agent-team",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def update_conversation_agent_team(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update conversation-level agent team settings, such as enabling or disabling the team."""
    service = AgentTeamService(db)
    if "enabled" in payload:
        enabled = bool(payload["enabled"])
        service.set_team_enabled(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            enabled=enabled,
        )
    return get_conversation_agent_team(
        conversation_id=conversation_id,
        auth=auth,
        db=db,
    )


@router.post(
    "/{conversation_id}/agent-team/interrupt",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def interrupt_conversation_agent_team_member(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Interrupt a running member of the conversation's agent team."""
    service = AgentTeamService(db)
    target = str(
        payload.get("target") or payload.get("member_name") or payload.get("agent_id") or ""
    ).strip()
    if not target:
        raise HTTPException(status_code=400, detail="Missing target or member_name")
    try:
        member = service.interrupt_member(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            member_name=target,
        )
        return {"status": "ok", "member": member.as_dict()}
    except AgentTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/{conversation_id}/goals",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_goals(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Read the goals a conversation holds for the goal bar above the composer.

    The bar is read-only: a goal is created and advanced by the agent through
    the goal tool, so a client never invents one.  The route returns every
    goal in creation order with its phase, revision, and round counters.
    """
    return goal_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )


@router.get(
    "/{conversation_id}/jobs",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_jobs(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Read the background jobs a conversation queued for the jobs panel.

    Turn jobs and artifact jobs are merged into one list ordered newest
    first, because the panel answers "what is this conversation still working
    on" without caring which table the job lives in.
    """
    return job_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        limit=limit,
    )


@router.get(
    "/{conversation_id}/skills",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_workspace_skills(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Read the skills a project workspace installs for the composer reference.

    The same rule the ``skill_list`` tool applies is applied here, so the
    composer's reference and the tool report the same skills.  One skill is a
    directory under ``.agents/skills`` that holds a ``SKILL.md``, a
    ``skill.md``, or a ``README.md``.
    """
    settings = get_settings()
    from app.deepspace.services.workspace_root import workspace_root_for

    workspace_root_for(settings, auth.tenant_id, auth.user_id, None)
    return workspace_skills(
        settings,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        query=query,
        limit=limit,
    )


@router.get(
    "/{conversation_id}/workflow-runs",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_workflow_runs(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Read the workflow runs a conversation started for the transcript card.

    A workflow run is one ``workflow_run`` call in the typed log.  The run's
    name, its outcome, and how long it took come from the same call and its
    result, so the transcript node and the list show the same numbers the
    model saw.
    """
    return workflow_run_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        limit=limit,
    )


@router.get(
    "/{conversation_id}/trajectory",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_trajectory(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    max_turns: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """Read one conversation's timing ledger.

    Each turn reports when it opened, when it closed, and the steps it ran.
    Each step reports the tools it called and how long each took.  The
    duration a tool reports is the one the runtime measured; the wall time
    between its call event and its result event is reported beside it, so a
    queue wait is visible when it happens.
    """
    return trajectory_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        max_turns=max_turns,
    )


@router.get(
    "/{conversation_id}/deliverables",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_deliverables(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """Read the files a conversation changed and the files it declared.

    The projection counts write tool calls from the typed log, because the
    log records the call and not the bytes it carried.  Declared deliverables
    are read from the worker's store when available and reconstructed from
    durable ``present`` tool events when the worker and API are separate
    processes.
    """
    changed = changed_file_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        limit=limit,
    )
    declared = declared_deliverable_projection(
        conversation_id,
        db=db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
    )
    return {
        "changed_files": changed,
        "declared_deliverables": declared.get("declared", []),
        "turn_changes": declared.get("turn_changes", []),
        "store_available": declared.get("available", False),
    }


@router.get(
    "/{conversation_id}/subagents/catalog",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_subagent_catalog(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read one conversation's delegated children as a routing catalogue.

    The tree answers "who reported to whom".  The catalogue answers the
    question a follow-up message has to answer first: this child accepted one
    message and finished (``one-shot``), it accepted messages and is standing
    by (``continuable`` and idle), or it accepted messages and is working (it
    is queued, not refused).  Each child reports how many messages are still
    queued for it and whether a message from the caller would be accepted
    now.
    """
    return subagent_catalog_projection(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )


@router.get(
    "/{conversation_id}/persona",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_persona(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read the deployment persona's state for this conversation.

    The persona is a deployment-wide statement of who the agent is for this
    customer.  It is injected as one system section before every turn.  The
    route reports whether it is enabled, configured, and active, without
    returning the full statement to a client.
    """
    settings = get_settings()
    return deployment_persona_state(settings)


@router.get(
    "/{conversation_id}/dictation",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_dictation(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the dictation settings for the composer voice input.

    The browser records the user, the server never receives audio, and the
    transcript is inserted as text.  The route reports the enabled flag, the
    provider, the default language, and the maximum duration.
    """
    return {
        "enabled": getattr(settings, "deepspace_dictation_enabled", True),
        "provider": getattr(settings, "deepspace_dictation_provider", "browser"),
        "language": getattr(settings, "deepspace_dictation_language", "en-US"),
        "max_seconds": getattr(settings, "deepspace_dictation_max_seconds", 300.0),
    }


@router.get(
    "/{conversation_id}/open-in-app",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_open_in_app_url(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the desktop app URL scheme for opening this conversation.

    The installed shell registers a custom URL scheme.  The frontend builds
    the link and offers it as a button in the conversation menu.
    """
    if not getattr(settings, "deepspace_open_in_app_enabled", True):
        return {"enabled": False, "url": None}
    return {
        "enabled": True,
        "url": f"averqel://conversation/{conversation_id}",
    }
