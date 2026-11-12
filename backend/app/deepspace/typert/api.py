"""Authenticated Typert introspection, capability calls, and change streaming.

The API answers what capabilities exist, what one schema looks like, and how
one package is reflected. Explicitly scoped, global-resource capabilities can
also be invoked through authenticated HTTP or WebSocket transports. Global
resources are accepted directly; identity-bound resources require a registered
ownership adapter and are denied when no authoritative owner record exists.

The change stream is SSE. It is authenticated exactly like the read endpoints
and carries only the changed store and key, never a payload. Capability scopes
are checked against the authenticated caller's permissions; an endpoint with
no scope is never remotely invocable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions, resolve_permissions
from app.deepspace.typert.descriptors import RemoteDescriptor
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.principal import Principal
from app.deepspace.typert.resources import GlobalResource, ResourceRef
from app.deepspace.typert.service import get_typert_service
from app.deepspace.typert.streams import RemoteStream
from app.platform.database.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/deepspace/typert",
    tags=["deepspace"],
    dependencies=[Depends(require_permissions("queries:run"))],
)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_READ_TIMEOUT_SECONDS = 15.0
_MAX_INVOCATION_ARGUMENTS = 1_000_000

stream_router = APIRouter(prefix="/deepspace/typert", tags=["deepspace"])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


def _authorize_invocation(
    auth: AuthContext,
    endpoint: str,
    db: Session | None = None,
) -> RemoteDescriptor:
    """Resolve an endpoint after permission and resource-owner checks."""
    descriptor = get_typert_service().registry.local.get(endpoint)
    if descriptor is None or not descriptor.invocable:
        raise RemoteError("gateway/not-found", f"Capability {endpoint!r} is not invocable.")
    if not descriptor.scope:
        raise RemoteError(
            "gateway/forbidden",
            "This capability has no remote permission scope and cannot be called over the API.",
        )
    permissions = resolve_permissions(
        roles=frozenset(auth.roles),
        direct_permissions=frozenset(getattr(auth, "permissions", ())),
    )
    if "*" not in permissions and descriptor.scope not in permissions:
        raise RemoteError(
            "gateway/forbidden",
            "The caller does not have the permission required by this capability.",
            details={"required_scope": descriptor.scope},
        )
    if not descriptor.resources:
        raise RemoteError(
            "gateway/forbidden",
            "This capability does not declare the resources it accesses.",
        )
    for access in descriptor.resources:
        if isinstance(access, GlobalResource):
            continue
        if not isinstance(access, ResourceRef) or db is None:
            raise RemoteError(
                "resource/denied",
                "This capability's identity-bound resource could not be authorized.",
            )
        kind = get_typert_service().registry.resource_kinds.get(access.kind)
        if kind is None or kind.authorize is None:
            raise RemoteError(
                "resource/denied",
                f"No ownership adapter is configured for resource kind {access.kind!r}.",
            )
        try:
            allowed = kind.authorize(db, auth, access.id)
        except Exception as exc:  # noqa: BLE001 - ownership failures must deny
            logger.warning(
                "Typert resource authorization failed for kind %s (%s)",
                access.kind,
                type(exc).__name__,
            )
            allowed = False
        if not allowed:
            raise RemoteError(
                "resource/denied",
                "The caller does not own or cannot access a declared resource.",
                details={"kind": access.kind},
            )
    return descriptor


def _principal_for(auth: AuthContext) -> Principal:
    return Principal(
        kind="user",
        id=str(auth.user_id),
        tenant_id=auth.tenant_id,
        display_name=None,
    )


def _raise_http_remote_error(error: RemoteError) -> None:
    status = {
        "gateway/unauthorized": 401,
        "gateway/forbidden": 403,
        "gateway/not-found": 404,
        "gateway/bad-request": 422,
        "validation/failed": 422,
        "gateway/conflict": 409,
        "gateway/unavailable": 503,
        "gateway/cancelled": 409,
        "resource/denied": 403,
    }.get(error.code, 500)
    raise HTTPException(status_code=status, detail=error.as_dict()) from error


@router.get("")
def get_typert_state(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Return the whole live capability state."""
    del auth
    return get_typert_service().snapshot()


@router.get("/schemas")
def list_typert_schemas(
    package: str | None = Query(default=None),
    face: str | None = Query(default=None),
    include_schema: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List live schemas, optionally with their projected JSON Schema."""
    del auth
    service = get_typert_service()
    records = service.registry.list(package=package, face=face)
    items: list[dict[str, Any]] = []
    for record in records:
        item = record.as_dict()
        if include_schema:
            item["json_schema"] = service.registry.to_json_schema(record.key)
        items.append(item)
    return {"schemas": items, "count": len(items)}


@router.get("/schemas/{key:path}")
def get_typert_schema(
    key: str,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Return one schema and its projected JSON Schema."""
    del auth
    service = get_typert_service()
    try:
        record = service.registry.resolve(key)
    except Exception as exc:  # noqa: BLE001 - one read must not 500 on a miss
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **record.as_dict(),
        "json_schema": service.registry.to_json_schema(key),
    }


@router.get("/packages")
def list_typert_packages(
    package: str | None = Query(default=None),
    face: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List the reflected package faces."""
    del auth
    service = get_typert_service()
    records = service.registry.list_packages(package=package, face=face)
    return {
        "packages": [
            {"package": record.package, "face": record.face, "key": record.key}
            for record in records
        ],
        "count": len(records),
    }


@router.get("/packages/{package:path}")
def get_typert_package(
    package: str,
    face: str = Query(default="host"),
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Return one package face's reflection and its declared endpoints."""
    del auth
    service = get_typert_service()
    record = service.registry.get_package(package, face)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Package {package!r} face {face!r} is not mounted.",
        )
    descriptors = service.registry.descriptors_for_package(package)
    return {
        **record.as_dict(),
        "descriptors": [descriptor.as_dict() for descriptor in descriptors],
    }


@router.get("/descriptors")
def list_typert_descriptors(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List local and consumer-selected remote descriptors."""
    del auth
    service = get_typert_service()
    return {
        "local": service.registry.local.as_dict(),
        "remote": service.registry.remotes.as_dict(),
        "remote_packages": service.registry.remotes.packages(),
        "descriptor_packages": service.registry.descriptor_packages(),
    }


@router.get("/lookups")
def list_typert_lookups(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List lookup declarations and whether a resolver is configured."""
    del auth
    service = get_typert_service()
    return {
        "lookups": service.registry.lookups.as_dict(),
        "keys": service.registry.lookups.keys(),
    }


@router.get("/contexts")
def list_typert_contexts(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List host and client Context adapters."""
    del auth
    service = get_typert_service()
    return {
        **service.registry.contexts.as_dict(),
        "keys": service.registry.contexts.keys(),
    }


@router.get("/resources")
def list_typert_resources(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List the resource kinds and their default wire rules."""
    del auth
    service = get_typert_service()
    return {
        "kinds": service.registry.resource_kinds.as_dict(),
        "declarations": service.registry.resources.entries(),
    }


@router.get("/loader")
def get_typert_loader(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Return the loader state and discovery errors."""
    del auth
    return get_typert_service().loader.status()


@router.post("/invoke")
async def invoke_typert_capability(
    payload: Annotated[dict[str, Any], Body()],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Invoke one scoped unary capability after authorizing its resources."""
    endpoint = payload.get("endpoint")
    arguments = payload.get("arguments", {})
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise HTTPException(status_code=422, detail="endpoint must be a non-empty string")
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=422, detail="arguments must be a JSON object")
    if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > _MAX_INVOCATION_ARGUMENTS:
        raise HTTPException(status_code=413, detail="arguments exceed the 1 MB limit")
    try:
        descriptor = _authorize_invocation(auth, endpoint, db)
        if descriptor.mode != "unary":
            raise RemoteError(
                "gateway/bad-request", "Use the stream transport for this capability."
            )
        result = await get_typert_service().registry.ainvoke(
            endpoint,
            arguments,
            principal=_principal_for(auth),
        )
    except RemoteError as exc:
        _raise_http_remote_error(exc)
    return {"result": result}


@stream_router.websocket("/invoke/stream")
async def invoke_typert_stream(websocket: WebSocket) -> None:
    """Run one authenticated Typert stream over a full-duplex WebSocket."""
    from app.core.config import get_settings
    from app.core.errors import ApiError
    from app.deepspace.api.chats import (
        _authenticate_websocket_auth_context,
        _require_websocket_permissions,
    )
    from app.platform.database.session import SessionLocal

    await websocket.accept()
    db = SessionLocal()
    stream: RemoteStream[Any, Any] | None = None
    downlink_task: asyncio.Task[None] | None = None
    try:
        auth = await _authenticate_websocket_auth_context(
            websocket,
            db=db,
            settings=get_settings(),
        )
        _require_websocket_permissions(auth)
        first = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        if not isinstance(first, dict):
            raise RemoteError("gateway/bad-request", "The stream request must be an object.")
        endpoint = first.get("endpoint")
        arguments = first.get("arguments", {})
        if not isinstance(endpoint, str) or not endpoint.strip() or not isinstance(arguments, dict):
            raise RemoteError(
                "gateway/bad-request", "The stream endpoint or arguments are invalid."
            )
        if (
            len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
            > _MAX_INVOCATION_ARGUMENTS
        ):
            raise RemoteError("gateway/bad-request", "Stream arguments exceed the 1 MB limit.")
        descriptor = _authorize_invocation(auth, endpoint, db)
        if descriptor.mode != "stream":
            raise RemoteError("gateway/bad-request", "The requested capability is not a stream.")
        value = await get_typert_service().registry.ainvoke(
            endpoint,
            arguments,
            principal=_principal_for(auth),
        )
        if not isinstance(value, RemoteStream):
            raise RemoteError("gateway/internal", "The capability did not create a stream.")
        stream = value
        await websocket.send_json({"event": "ready", "endpoint": endpoint})

        async def send_downlink() -> None:
            assert stream is not None
            async for item in stream:
                await websocket.send_json({"event": "downlink", "value": item})
            await websocket.send_json({"event": "complete"})

        downlink_task = asyncio.create_task(send_downlink())
        uplink = stream.handle()
        while not uplink.ended:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict):
                raise RemoteError("gateway/bad-request", "A stream frame must be an object.")
            event = frame.get("event")
            if event == "uplink":
                if (
                    len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
                    > _MAX_INVOCATION_ARGUMENTS
                ):
                    raise RemoteError("gateway/bad-request", "Stream frames exceed the 1 MB limit.")
                await uplink.send(frame.get("value"))
            elif event == "end":
                await uplink.end()
            elif event == "cancel":
                await uplink.dispose()
            else:
                raise RemoteError("gateway/bad-request", "Unknown stream frame event.")
        if downlink_task is not None:
            await downlink_task
    except WebSocketDisconnect:
        pass
    except RemoteError as exc:
        try:
            await websocket.send_json({"event": "error", **exc.as_dict()})
            await websocket.close(code=4403 if exc.code == "gateway/forbidden" else 4400)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass
    except ApiError as exc:
        try:
            await websocket.close(code=4401 if exc.status_code == 401 else 4403)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass
    except TimeoutError:
        try:
            await websocket.close(code=4408, reason="Timed out waiting for a stream request")
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass
    except Exception:  # noqa: BLE001 - never expose implementation details on the wire
        logger.exception("Typert stream invocation failed")
        try:
            await websocket.send_json(
                {
                    "event": "error",
                    "code": "gateway/internal",
                    "message": "The capability stream failed.",
                    "details": {},
                }
            )
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if downlink_task is not None and not downlink_task.done():
            downlink_task.cancel()
            await asyncio.gather(downlink_task, return_exceptions=True)
        if stream is not None and not stream.closed:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001 - transport cleanup must not mask the call result
                logger.warning("Typert stream cleanup failed", exc_info=True)
        db.close()


@router.get("/changes")
async def stream_typert_changes(
    request: Request,
    _auth: AuthContext = Depends(get_auth_context),
) -> Response:
    """Stream registry changes as SSE until the client disconnects."""
    service = get_typert_service()
    subscription = service.subscribe_changes()

    async def events() -> AsyncIterator[str]:
        try:
            yield _sse("typert/ready", {"ready": True})
            while True:
                if await request.is_disconnected():
                    break
                change = await asyncio.to_thread(
                    subscription.get,
                    _READ_TIMEOUT_SECONDS,
                )
                if change is None:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse("typert/change", change.as_dict())
        finally:
            service.unsubscribe_changes(subscription)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


__all__ = ["SSE_HEADERS", "router", "stream_router"]
