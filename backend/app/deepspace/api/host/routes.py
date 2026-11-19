"""Host API routes for DeepSpace."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.deepspace.api.host.deliverables import router as deliverables_router
from app.deepspace.services.file_references import get_file_reference_service
from app.deepspace.services.host import (
    AnonymousUserIdService,
    DeliverablesService,
    DirectoryPickerService,
    OpenInAppService,
    PluginInventoryService,
    ProductTelemetryService,
    SPADistServer,
    WebServerService,
    WorkspaceChangesService,
)
from app.deepspace.services.slash_commands import get_slash_command_registry
from app.deepspace.services.workspace_root import workspace_root_for
from app.platform.database.session import get_db

router = APIRouter(prefix="/host", tags=["host"])


# Dependency injection - these would be properly wired in the app
def get_web_server() -> WebServerService:
    # In real implementation, this would come from app state
    raise NotImplementedError("WebServerService not wired")


def get_spa_dist_server() -> SPADistServer:
    raise NotImplementedError("SPADistServer not wired")


def get_directory_picker() -> DirectoryPickerService:
    raise NotImplementedError("DirectoryPickerService not wired")


def get_open_in_app() -> OpenInAppService:
    raise NotImplementedError("OpenInAppService not wired")


def get_plugin_inventory() -> PluginInventoryService:
    raise NotImplementedError("PluginInventoryService not wired")


def get_telemetry() -> ProductTelemetryService:
    raise NotImplementedError("ProductTelemetryService not wired")


def get_anonymous_id() -> AnonymousUserIdService:
    raise NotImplementedError("AnonymousUserIdService not wired")


def get_deliverables_service() -> DeliverablesService:
    raise NotImplementedError("DeliverablesService not wired")


def get_workspace_changes_service() -> WorkspaceChangesService:
    raise NotImplementedError("WorkspaceChangesService not wired")


# Web Server Routes
class RouteRegisterRequest(BaseModel):
    path: str
    methods: list[str] = ["GET", "HEAD"]
    is_prefix: bool = False


@router.post("/routes/exact")
async def register_exact_route(
    request: RouteRegisterRequest,
    web_server: WebServerService = Depends(get_web_server),
):
    """Register an exact-match route."""
    # In real implementation, this would register a handler
    return {"disposer": "function"}


@router.post("/routes/prefix")
async def register_prefix_route(
    request: RouteRegisterRequest,
    web_server: WebServerService = Depends(get_web_server),
):
    """Register a prefix-match route."""
    return {"disposer": "function"}


@router.post("/routes/upgrade")
async def register_upgrade_route(
    path: str,
    web_server: WebServerService = Depends(get_web_server),
):
    """Register a WebSocket upgrade route."""
    return {"disposer": "function"}


@router.post("/routes/fallback")
async def register_fallback(
    web_server: WebServerService = Depends(get_web_server),
):
    """Register fallback handler."""
    return {"disposer": "function"}


@router.get("/status")
async def get_web_server_status(
    web_server: WebServerService = Depends(get_web_server),
) -> dict[str, Any]:
    """Get web server runtime status."""
    return {
        "protocol": web_server.protocol,
        "origin": web_server.get_origin(),
        "host": "127.0.0.1",
        "port": 3000,
    }


# Directory Picker Routes
class DirectoryPickRequest(BaseModel):
    signal: str | None = None  # Would be AbortSignal in real impl


@router.post("/directory-picker/pick")
async def pick_directory(
    request: DirectoryPickRequest,
    picker: DirectoryPickerService = Depends(get_directory_picker),
) -> dict[str, Any]:
    """Open native directory picker."""
    if not picker.capability or picker.capability.kind != "native":
        raise HTTPException(status_code=400, detail="Native picker not available")

    # In real implementation, this would call picker.capability.pick()
    return {"path": "/selected/path"}


@router.get("/directory-picker/list")
async def list_directory(
    path: str | None = None,
    picker: DirectoryPickerService = Depends(get_directory_picker),
) -> dict[str, Any]:
    """List directory contents (browse backend)."""
    if not picker.capability or picker.capability.kind != "browse":
        raise HTTPException(status_code=400, detail="Browse picker not available")

    return {"entries": [], "crumbs": [], "truncated": False}


class CreateDirectoryRequest(BaseModel):
    path: str
    name: str


@router.post("/directory-picker/create")
async def create_directory(
    request: CreateDirectoryRequest,
    picker: DirectoryPickerService = Depends(get_directory_picker),
) -> dict[str, Any]:
    """Create a directory (browse backend)."""
    if not picker.capability or picker.capability.kind != "browse":
        raise HTTPException(status_code=400, detail="Browse picker not available")

    return {"path": "/new/path", "hidden": False}


# Open In App Routes
@router.get("/open-in-app/apps")
async def get_open_in_app_apps(
    open_in_app: OpenInAppService = Depends(get_open_in_app),
) -> list[dict[str, Any]]:
    """Get the resolved application catalog."""
    await open_in_app.resolve()
    return open_in_app.get_catalog()


@router.get("/open-in-app/icon/{app_id}")
async def get_open_in_app_icon(
    app_id: str,
    open_in_app: OpenInAppService = Depends(get_open_in_app),
) -> Response:
    """Get application icon."""
    icon_bytes = await open_in_app.get_icon(app_id)
    if not icon_bytes:
        raise HTTPException(status_code=404, detail="Icon not found")
    return Response(content=icon_bytes, media_type="image/png")


class OpenInAppLaunchRequest(BaseModel):
    app_id: str
    path: str


@router.post("/open-in-app/open")
async def launch_open_in_app(
    request: OpenInAppLaunchRequest,
    open_in_app: OpenInAppService = Depends(get_open_in_app),
) -> dict[str, bool]:
    """Launch an application with a workspace path."""
    success = await open_in_app.launch(request.app_id, request.path)
    return {"success": success}


# Plugin Inventory Routes
@router.get("/plugin-inventory/list")
async def list_plugin_inventory(
    inventory: PluginInventoryService = Depends(get_plugin_inventory),
) -> dict[str, Any]:
    """Get plugin inventory snapshot."""
    _ = await inventory.list()
    return {
        "entries": [],
        "agent_presets": [],
    }


# Telemetry Routes
class TelemetryEventRequest(BaseModel):
    name: str
    attributes: dict[str, Any] = {}
    severity: str = "INFO"


@router.post("/telemetry/emit")
async def emit_telemetry(
    request: TelemetryEventRequest,
    telemetry: ProductTelemetryService = Depends(get_telemetry),
) -> dict[str, str]:
    """Emit a telemetry event."""
    telemetry.emit(request.name, request.attributes, severity=request.severity)
    return {"status": "accepted"}


# Identity Routes
@router.get("/identity/anonymous-id")
async def get_anonymous_id(
    identity: AnonymousUserIdService = Depends(get_anonymous_id),
) -> dict[str, str]:
    """Get the anonymous user ID."""
    return {"user_id": identity.get_id_for_telemetry()}


@router.post("/identity/anonymous-id/reset")
async def reset_anonymous_id(
    identity: AnonymousUserIdService = Depends(get_anonymous_id),
) -> dict[str, str]:
    """Reset the anonymous user ID."""
    new_id = identity.reset_id()
    return {"user_id": new_id}


# Slash Command Routes
class SlashCommandExecuteRequest(BaseModel):
    input: str = Field(
        ..., min_length=1, description="Raw slash command input (e.g., '/compact --llm')"
    )


class SlashCommandExecuteResponse(BaseModel):
    success: bool
    message: str
    data: dict[str, Any] | None = None
    continue_conversation: bool = False


class SlashCommandInfo(BaseModel):
    name: str
    description: str
    usage: str | None = None
    aliases: list[str] = []


@router.get(
    "/slash-commands",
    dependencies=[Depends(get_auth_context)],
)
async def list_slash_commands(
    auth: AuthContext = Depends(get_auth_context),
) -> list[SlashCommandInfo]:
    """List all available slash commands."""
    registry = get_slash_command_registry()
    commands = []
    for cmd in registry.list_commands():
        commands.append(
            SlashCommandInfo(
                name=cmd.name,
                description=cmd.description,
                usage=cmd.usage,
                aliases=list(cmd.aliases) if cmd.aliases else [],
            )
        )
    return commands


@router.post(
    "/chats/{conversation_id}/slash-command",
    dependencies=[Depends(get_auth_context)],
)
async def execute_slash_command(
    conversation_id: str,
    request: SlashCommandExecuteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> SlashCommandExecuteResponse:
    """Execute a slash command for a conversation."""
    import uuid

    from app.core.config import get_settings
    from app.deepspace.services.slash_commands import SlashCommandContext

    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid conversation ID") from exc

    registry = get_slash_command_registry(get_settings())

    # Build minimal context; registry.execute() parses input and fills in
    # command, args, kwargs, raw_input automatically.
    context = SlashCommandContext(
        conversation_id=conv_uuid,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        raw_input=request.input,
        command="",
        args=[],
        kwargs={},
        db=db,
    )

    result = await registry.execute(request.input, context)

    return SlashCommandExecuteResponse(
        success=result.success,
        message=result.message,
        data=dict(result.data),
        continue_conversation=result.continue_conversation,
    )


# File Reference Routes (for @file mentions)
class FileReferenceCandidate(BaseModel):
    path: str
    is_directory: bool
    size: int | None = None
    mime_type: str | None = None


class FileReferenceListResponse(BaseModel):
    query: str
    candidates: list[FileReferenceCandidate]
    count: int


@router.get(
    "/file-references",
    dependencies=[Depends(get_auth_context)],
)
async def list_file_references(
    query: str = "",
    conversation_id: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> FileReferenceListResponse:
    """List workspace file candidates for @file completion autocomplete."""
    from app.core.config import get_settings

    settings = get_settings()
    workspace_root = workspace_root_for(settings, auth.tenant_id, auth.user_id)
    service = get_file_reference_service(settings)

    candidates = await service.list(workspace_root, query)
    return FileReferenceListResponse(
        query=query,
        candidates=[
            FileReferenceCandidate(
                path=c.path,
                is_directory=c.is_directory,
                size=c.size,
                mime_type=c.mime_type,
            )
            for c in candidates
        ],
        count=len(candidates),
    )


router.include_router(deliverables_router)

__all__ = ["router"]
