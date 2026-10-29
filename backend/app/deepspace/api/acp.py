"""The authenticated Agent Client Protocol endpoint for DeepSpace.

A trusted program drives DeepSpace through this endpoint.  It speaks
JSON-RPC 2.0 over HTTP and implements the protocol's session methods:
initialize, authenticate, session creation, prompting, cancellation, and
session listing.

The endpoint carries the same permission checks as the rest of the API, so
a client can only reach conversations the authenticated user owns.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.deepspace.services.acp_bridge import (
    AGENT_NAME,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    AcpBridge,
    AcpError,
    prompt_text,
    run_prompt,
)
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.platform.database.session import managed_db_session, set_db_tenant_context
from app.providers.services.reasoning_capabilities import normalize_reasoning_effort_value

router = APIRouter(prefix="/deepspace/acp", tags=["deepspace-acp"])


def _error_payload(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build one JSON-RPC error response.

    Args:
        request_id: The request's identifier.
        code: The JSON-RPC error code.
        message: The human-readable detail.

    Returns:
        The error response body.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result_payload(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build one JSON-RPC success response.

    Args:
        request_id: The request's identifier.
        result: The method's result.

    Returns:
        The success response body.
    """
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@router.post(
    "",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def acp_rpc(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Handle one Agent Client Protocol request."""
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=400,
            content=_error_payload(None, JSONRPC_INVALID_REQUEST, "The request body is not JSON."),
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                None, JSONRPC_INVALID_REQUEST, "A JSON-RPC request must be an object."
            ),
        )
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    if not method:
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                request_id, JSONRPC_INVALID_REQUEST, "A JSON-RPC request needs a method."
            ),
        )
    if not bool(getattr(settings, "deepspace_acp_enabled", False)):
        return JSONResponse(
            status_code=403,
            content=_error_payload(
                request_id,
                JSONRPC_INVALID_REQUEST,
                "The Agent Client Protocol bridge is disabled on this deployment.",
            ),
        )
    bridge = AcpBridge(auth=auth)
    try:
        result = await _dispatch(
            bridge=bridge,
            method=method,
            params=params,
            auth=auth,
            settings=settings,
        )
    except AcpError as exc:
        return JSONResponse(
            status_code=200, content=_error_payload(request_id, exc.code, exc.message)
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=200,
            content=_error_payload(request_id, JSONRPC_INTERNAL_ERROR, str(exc)[:1_000]),
        )
    return JSONResponse(status_code=200, content=_result_payload(request_id, result))


async def _dispatch(
    *,
    bridge: AcpBridge,
    method: str,
    params: dict[str, Any],
    auth: AuthContext,
    settings: Settings,
) -> dict[str, Any]:
    """Run one protocol method.

    Args:
        bridge: The bridge state.
        method: The method the client called.
        params: The method's parameters.
        auth: The authenticated tenant and user.
        settings: The deployment settings.

    Returns:
        The method's result.

    Raises:
        AcpError: When the method is unknown or its parameters are invalid.
    """
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentInfo": {"name": AGENT_NAME, "version": "1"},
            "agentCapabilities": {
                "promptCapabilities": {"image": False, "audio": False},
                "sessionCapabilities": {"load": True},
            },
            "authMethods": [],
        }
    if method == "authenticate":
        # The API already authenticated the caller before this method runs,
        # so there is nothing left to negotiate.
        return {}
    if method == "session/new":
        conversation_id = await _create_conversation(auth=auth)
        session = bridge.new_session(conversation_id)
        return {"sessionId": session.session_id}
    if method == "session/load":
        session_id = str(params.get("sessionId") or "")
        conversation_id = await _resolve_conversation(
            auth=auth,
            conversation_id=session_id,
        )
        session = bridge.new_session(conversation_id)
        return {"sessionId": session.session_id}
    if method == "session/prompt":
        session = bridge.session(str(params.get("sessionId") or ""))
        prompt = prompt_text(params.get("prompt"))
        effort = normalize_reasoning_effort_value(
            str(params.get("reasoningEffort") or "").strip() or ""
        )
        with managed_db_session() as db:
            set_db_tenant_context(db, auth.tenant_id)
            service = DeepSpaceChatService(db=db, settings=settings)
            updates, stop_reason = await run_prompt(
                bridge=bridge,
                session=session,
                prompt=prompt,
                service=service,
                thinking_enabled=bool(params.get("thinkingEnabled")),
                reasoning_effort=effort,
            )
        return {"stopReason": stop_reason, "updates": updates}
    if method == "session/cancel":
        session = bridge.session(str(params.get("sessionId") or ""))
        await _cancel_conversation(auth=auth, conversation_id=session.conversation_id)
        return {}
    if method == "session/close":
        bridge.close_session(str(params.get("sessionId") or ""))
        return {}
    if method == "session/list":
        return {"sessions": await _list_conversations(auth=auth)}
    raise AcpError(JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")


async def _create_conversation(*, auth: AuthContext) -> uuid.UUID:
    """Create the conversation one bridge session drives.

    Args:
        auth: The authenticated tenant and user.

    Returns:
        The new conversation's identity.
    """
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    with managed_db_session() as db:
        set_db_tenant_context(db, auth.tenant_id)
        conversation = DeepSpaceChatRepository(db).create_conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            kind="deepspace",
            title="ACP session",
        )
        db.commit()
        return uuid.UUID(str(conversation.id))


async def _resolve_conversation(*, auth: AuthContext, conversation_id: str) -> uuid.UUID:
    """Resolve the conversation a client named, after checking ownership.

    Args:
        auth: The authenticated tenant and user.
        conversation_id: The conversation the client named.

    Returns:
        The conversation's identity.

    Raises:
        AcpError: When the conversation is unknown to this caller.
    """
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    try:
        parsed = uuid.UUID(str(conversation_id))
    except ValueError as exc:
        raise AcpError(
            JSONRPC_INVALID_PARAMS, "the session id is not a valid conversation id"
        ) from exc
    with managed_db_session() as db:
        set_db_tenant_context(db, auth.tenant_id)
        conversation = DeepSpaceChatRepository(db).get_conversation(
            tenant_id=auth.tenant_id,
            conversation_id=parsed,
            user_id=auth.user_id,
        )
    if conversation is None:
        raise AcpError(JSONRPC_INVALID_PARAMS, "the conversation does not belong to this caller")
    return parsed


async def _cancel_conversation(*, auth: AuthContext, conversation_id: uuid.UUID) -> None:
    """Cancel the work one conversation is running.

    Args:
        auth: The authenticated tenant and user.
        conversation_id: The conversation to cancel.
    """
    from app.deepspace.services.runtime_store import DeepSpaceRuntimeStore

    with managed_db_session() as db:
        set_db_tenant_context(db, auth.tenant_id)
        DeepSpaceRuntimeStore(db).request_cancel(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )


async def _list_conversations(*, auth: AuthContext) -> list[dict[str, Any]]:
    """List the conversations this caller owns.

    Args:
        auth: The authenticated tenant and user.

    Returns:
        One entry per conversation.
    """
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    with managed_db_session() as db:
        set_db_tenant_context(db, auth.tenant_id)
        conversations = DeepSpaceChatRepository(db).list_conversations(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
        )
    return [
        {
            "sessionId": str(conversation.id),
            "title": str(conversation.title or ""),
            "createdAt": conversation.created_at.isoformat() if conversation.created_at else None,
        }
        for conversation in conversations
    ]


__all__ = ["router"]
