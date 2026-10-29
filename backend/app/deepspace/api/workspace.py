"""The workspace panel routes for DeepSpace.

The panel shows the conversation's workspace, its live terminal sessions,
and an in-app browser.  Every route here is scoped to the tenant and the
user the request belongs to, and every path it reads or writes is resolved
inside that workspace, so a request can never reach another workspace.

An account owns a default workspace and may own named workspaces beside it.
The routes here list those workspaces, create one, and record which of them
one conversation works in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.deepspace.api.chats import (
    _authenticate_websocket_auth_context,
    _require_websocket_permissions,
)
from app.deepspace.services.terminals import (
    ShellUnavailableError,
    TerminalError,
    TerminalLimitError,
    TerminalNotFoundError,
    available_shells,
    default_shell,
    signal_number,
    terminal_manager,
)
from app.deepspace.services.workspace_root import (
    WorkspaceUnavailableError,
    workspace_root_for,
)
from app.deepspace.services.workspaces import (
    DEFAULT_WORKSPACE_ID,
    DuplicateWorkspaceError,
    InvalidWorkspaceNameError,
    UnknownWorkspaceError,
    WorkspaceSelectionService,
    create_workspace,
    delete_workspace,
    find_workspace,
    list_workspaces,
)
from app.platform.database.session import SessionLocal, get_db

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-workspace"])
logger = logging.getLogger(__name__)

#: The directories the file listing never walks into.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".deepspace-tmp",
    }
)

#: The largest file the panel reads.
MAX_FILE_BYTES = 512_000

#: The largest number of entries one listing returns.
MAX_ENTRIES = 500


def _workspace(settings: Settings, auth: AuthContext) -> Path:
    """Return the caller's default workspace root.

    Args:
        settings: The deployment settings.
        auth: The authenticated caller.

    Returns:
        The workspace root.

    Raises:
        HTTPException: When the deployment has no workspace.
    """
    try:
        return workspace_root_for(settings, auth.tenant_id, auth.user_id)
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _conversation_workspace(
    settings: Settings,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    db: Session,
) -> Path:
    """Return the root of the workspace one conversation works in.

    The workspace a conversation selected is a per-conversation state, so
    the conversation's own choice decides and the account's default
    workspace is only the default.  A selection that cannot be read leaves
    the conversation on the default workspace.

    Args:
        settings: The deployment settings.
        auth: The authenticated caller.
        conversation_id: The conversation the request belongs to.
        db: The database session.

    Returns:
        The workspace root.

    Raises:
        HTTPException: When the deployment has no workspace.
    """
    try:
        workspace_id = WorkspaceSelectionService(db).effective_workspace(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        return workspace_root_for(settings, auth.tenant_id, auth.user_id, workspace_id)
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:  # noqa: BLE001
        # A selection that cannot be read must not move the boundary, so the
        # account's default workspace stands.
        return _workspace(settings, auth)


def _resolve_within(root: Path, relative: str) -> Path:
    """Resolve one workspace-relative path inside the workspace.

    Args:
        root: The workspace root.
        relative: The relative path.

    Returns:
        The resolved path.

    Raises:
        HTTPException: When the path escapes the workspace.
    """
    candidate = (root / relative).resolve() if relative else root.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="That path is outside the workspace.") from exc
    return candidate


def _language_for(path: Path) -> str | None:
    """Return the language a file is written in.

    Args:
        path: The file's path.

    Returns:
        The language name, or None when it is not known.
    """
    names = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".sh": "shell",
        ".sql": "sql",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".html": "html",
        ".css": "css",
    }
    return names.get(path.suffix.lower())


@router.get(
    "/workspaces",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_caller_workspaces(
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List the workspaces the caller owns."""
    try:
        workspaces = list_workspaces(settings, auth.tenant_id, auth.user_id)
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "workspaces": [workspace.as_dict() for workspace in workspaces],
        "default": DEFAULT_WORKSPACE_ID,
    }


@router.post(
    "/workspaces",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def create_caller_workspace(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create one named workspace for the caller."""
    name = str(payload.get("name") or "")
    path = str(payload.get("path") or "").strip() or None
    try:
        workspace = create_workspace(settings, auth.tenant_id, auth.user_id, name, path=path)
    except InvalidWorkspaceNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DuplicateWorkspaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return workspace.as_dict()


@router.delete(
    "/workspaces/{workspace_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def delete_caller_workspace(
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete one named workspace for the caller."""
    if workspace_id == DEFAULT_WORKSPACE_ID:
        raise HTTPException(status_code=400, detail="The default workspace cannot be deleted.")
    deleted = delete_workspace(settings, auth.tenant_id, auth.user_id, workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such workspace exists.")
    return {"deleted": True, "id": workspace_id}


@router.get(
    "/workspaces/browse",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def browse_host_directories(
    path: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Browse host directory tree for the directory picker dialog."""
    del auth
    home = str(Path.home())
    target_str = path.strip() if path and path.strip() else home
    target = Path(target_str).expanduser().resolve()

    if not target.exists() or not target.is_dir():
        target = Path(home).resolve()

    # Ancestry crumbs from filesystem root to target
    crumbs: list[dict[str, Any]] = []
    current = target
    for parent in [current, *current.parents]:
        name = str(parent) if parent.parent == parent else parent.name
        crumbs.insert(0, {"name": name or str(parent), "path": str(parent), "hidden": False})

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(
            [c for c in target.iterdir() if c.is_dir()],
            key=lambda c: c.name.lower(),
        )
        for child in children:
            if len(entries) >= 500:
                truncated = True
                break
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.resolve()),
                    "hidden": child.name.startswith("."),
                }
            )
    except OSError:
        pass

    return {
        "path": str(target),
        "home": home,
        "crumbs": crumbs,
        "entries": entries,
        "truncated": truncated,
    }


@router.post(
    "/workspaces/create-directory",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def create_host_directory(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Create a new folder in a directory on the host."""
    del auth
    parent_str = str(payload.get("path") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not parent_str:
        raise HTTPException(status_code=400, detail="A parent directory path is required.")
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid directory name.")

    parent = Path(parent_str).expanduser().resolve()
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent is not a directory.")

    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail=f"'{target}' already exists.")

    try:
        target.mkdir()
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"Could not create folder: {exc}") from exc

    return {"path": str(target)}


@router.get(
    "/{conversation_id}/workspace/tree",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_workspace_tree(
    conversation_id: uuid.UUID,
    path: str = Query(default=""),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List one directory of the conversation's workspace."""
    root = _conversation_workspace(settings, auth, conversation_id, db)
    target = _resolve_within(root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="That path does not exist in the workspace.")
    if target.is_file():
        return {
            "entries": [
                {
                    "name": target.name,
                    "path": str(target.relative_to(root.resolve())),
                    "kind": "file",
                    "size_bytes": target.stat().st_size,
                }
            ],
            "root": str(root.resolve()),
        }
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
        if child.name in SKIPPED_DIRECTORIES or child.name.startswith("."):
            continue
        try:
            uuid.UUID(child.name)
            continue
        except (ValueError, AttributeError):
            pass
        try:
            size = child.stat().st_size
        except OSError:
            size = 0
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(root.resolve())),
                "kind": "directory" if child.is_dir() else "file",
                "size_bytes": size,
            }
        )
        if len(entries) >= MAX_ENTRIES:
            break
    return {"entries": entries, "root": str(root.resolve())}


@router.get(
    "/{conversation_id}/workspace/file",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def read_workspace_file(
    conversation_id: uuid.UUID,
    path: str = Query(default=""),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read one text file of the conversation's workspace."""
    if not path:
        raise HTTPException(status_code=400, detail="A path is required.")
    root = _conversation_workspace(settings, auth, conversation_id, db)
    target = _resolve_within(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="That file does not exist in the workspace.")
    try:
        payload = target.read_bytes()[:MAX_FILE_BYTES]
    except OSError as exc:
        raise HTTPException(status_code=409, detail="That file could not be read.") from exc
    truncated = target.stat().st_size > MAX_FILE_BYTES
    return {
        "path": str(target.relative_to(root.resolve())),
        "content": payload.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "language": _language_for(target),
    }


@router.get(
    "/{conversation_id}/workspace",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_workspace(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the workspace the conversation works in."""
    service = WorkspaceSelectionService(db)
    recorded = service.recorded_workspace(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    effective = service.effective_workspace(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    try:
        workspace = find_workspace(settings, auth.tenant_id, auth.user_id, effective)
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "workspace": workspace.as_dict() if workspace is not None else None,
        "recorded": recorded,
    }


@router.put(
    "/{conversation_id}/workspace",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_conversation_workspace(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record the workspace the conversation works in."""
    identifier = str(payload.get("workspace") or "").strip()
    try:
        workspace = find_workspace(settings, auth.tenant_id, auth.user_id, identifier)
    except WorkspaceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if workspace is None:
        # A workspace that belongs to another account is not found either, so
        # a caller cannot learn that it exists.
        raise HTTPException(status_code=400, detail="No such workspace.")
    try:
        return WorkspaceSelectionService(db).set_workspace(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            workspace=workspace.id,
        )
    except UnknownWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{conversation_id}/terminals",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_terminal_sessions(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List the conversation's live terminal sessions."""
    del settings
    manager = terminal_manager()
    sessions = manager.list(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return {
        "sessions": [
            {
                "session_id": session.id,
                "shell": session.shell,
                "pty": session.pty,
                "cwd": str(session.cwd),
                "created_at": session.created_at.isoformat(),
                "idle_seconds": round(session.idle_seconds(), 3),
            }
            for session in sessions
        ]
    }


@router.get(
    "/{conversation_id}/terminals/shells",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_terminal_shells(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """List the shells this host can actually start for this caller."""
    del conversation_id, auth
    shells = available_shells()
    try:
        selected = default_shell()
    except ShellUnavailableError:
        selected = None
    return {"shells": list(shells), "default_shell": selected}


@router.post(
    "/{conversation_id}/terminals",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def open_terminal_session(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Open a persistent terminal session for the conversation."""
    requested_shell = str(payload.get("shell") or "").strip()
    try:
        shell = requested_shell or default_shell()
    except ShellUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if shell not in {"bash", "pwsh", "cmd"}:
        raise HTTPException(status_code=400, detail="That shell is not available.")
    manager = terminal_manager()
    try:
        session = await manager.open(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            settings=settings,
            shell=shell,  # type: ignore[arg-type]
            cwd=str(payload.get("cwd") or "") or None,
            command=str(payload.get("command") or "") or None,
            workspace_root=_conversation_workspace(settings, auth, conversation_id, db),
        )
    except ShellUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TerminalLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except TerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "session_id": session.id,
        "shell": session.shell,
        "pty": session.pty,
        "cwd": str(session.cwd),
        "output": session.process.peek(16384),
    }


@router.websocket("/{conversation_id}/terminals/{session_id}/stream")
async def terminal_stream(
    websocket: WebSocket,
    conversation_id: uuid.UUID,
    session_id: str,
) -> None:
    """Stream one owned PTY and accept interactive terminal input.

    The browser cannot attach an Authorization header during a WebSocket
    handshake, so this endpoint accepts the same short-lived access token and
    tenant id in query parameters as the other authenticated application
    sockets.  The terminal session itself remains scoped by the authenticated
    tenant, user, and conversation.
    """
    db = SessionLocal()
    manager = terminal_manager()
    session = None
    queue: asyncio.Queue[bytes | None] | None = None
    receive_task: asyncio.Task[str] | None = None
    output_task: asyncio.Task[bytes | None] | None = None
    try:
        settings = get_settings()
        try:
            auth = await _authenticate_websocket_auth_context(websocket, db=db, settings=settings)
            _require_websocket_permissions(auth)
        except ApiError:
            await websocket.close(code=1008, reason="Authentication required")
            return

        try:
            session, initial_output, queue = manager.subscribe(
                session_id,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
        except TerminalNotFoundError:
            await websocket.close(code=4404, reason="Terminal session not found")
            return

        await websocket.accept()
        if initial_output:
            await websocket.send_json({"type": "output", "data": initial_output})
        await websocket.send_json(
            {
                "type": "ready",
                "shell": session.shell,
                "pty": session.pty,
                "cwd": str(session.cwd),
                "alive": session.process.is_alive,
            }
        )

        receive_task = asyncio.create_task(websocket.receive_text())
        output_task = asyncio.create_task(queue.get())
        while True:
            done, _ = await asyncio.wait(
                (receive_task, output_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if output_task in done:
                chunk = output_task.result()
                if chunk is None:
                    await websocket.send_json({"type": "exit", "alive": False})
                    break
                await websocket.send_bytes(chunk)
                output_task = asyncio.create_task(queue.get())

            if receive_task in done:
                try:
                    message = json.loads(receive_task.result())
                except WebSocketDisconnect:
                    break
                except (TypeError, ValueError):
                    await websocket.send_json(
                        {"type": "error", "message": "Terminal messages must be JSON."}
                    )
                    receive_task = asyncio.create_task(websocket.receive_text())
                    continue

                if isinstance(message, dict):
                    message_type = message.get("type")
                    try:
                        if message_type == "input" and isinstance(message.get("data"), str):
                            await manager.write(
                                session_id,
                                tenant_id=auth.tenant_id,
                                user_id=auth.user_id,
                                conversation_id=conversation_id,
                                data=message["data"],
                            )
                        elif message_type == "resize":
                            columns = message.get("columns")
                            rows = message.get("rows")
                            if not isinstance(columns, int) or not isinstance(rows, int):
                                raise TerminalError("Terminal dimensions must be integers.")
                            await manager.resize(
                                session_id,
                                tenant_id=auth.tenant_id,
                                user_id=auth.user_id,
                                conversation_id=conversation_id,
                                columns=columns,
                                rows=rows,
                            )
                        elif message_type == "signal":
                            sig = signal_number(str(message.get("signal") or ""))
                            if sig is None:
                                raise TerminalError("That terminal signal is not supported.")
                            await manager.signal(
                                session_id,
                                tenant_id=auth.tenant_id,
                                user_id=auth.user_id,
                                conversation_id=conversation_id,
                                sig=sig,
                            )
                        elif message_type == "ping":
                            await websocket.send_json({"type": "pong"})
                    except TerminalNotFoundError:
                        await websocket.send_json(
                            {"type": "exit", "alive": False, "message": "Terminal session closed."}
                        )
                        break
                    except TerminalError as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                receive_task = asyncio.create_task(websocket.receive_text())
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - a socket failure must not kill the API process.
        logger.exception("DeepSpace terminal WebSocket failed")
        try:
            await websocket.close(code=1011, reason="Terminal stream failed")
        except Exception:  # noqa: BLE001 - cleanup must not mask the socket failure.
            logger.debug("Terminal WebSocket close failed", exc_info=True)
    finally:
        for task in (receive_task, output_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (receive_task, output_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
        if session is not None and queue is not None:
            manager.unsubscribe(session, queue)
        db.close()


@router.get(
    "/{conversation_id}/terminals/{session_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def read_terminal_session(
    conversation_id: uuid.UUID,
    session_id: str,
    max_bytes: int = Query(default=16384, ge=1, le=262_144),
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Read the buffered output of one terminal session."""
    manager = terminal_manager()
    try:
        result = await manager.read(
            session_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            max_bytes=max_bytes,
        )
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such terminal session.") from exc
    except TerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "output": result.output,
        "truncated": result.truncated,
        "dropped_bytes": result.dropped_bytes,
        "alive": result.alive,
    }


@router.post(
    "/{conversation_id}/terminals/{session_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def write_terminal_session(
    conversation_id: uuid.UUID,
    session_id: str,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Send input to one terminal session."""
    data = str(payload.get("data") or "")
    if not data:
        raise HTTPException(status_code=400, detail="There is nothing to send.")
    manager = terminal_manager()
    try:
        written = await manager.write(
            session_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            data=data,
        )
        # The reason tells the caller whether the session finished, is waiting
        # for input, or went quiet.
        wait_reason = await manager.wait_for_reason(
            session_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such terminal session.") from exc
    except TerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"written_bytes": written, "wait_reason": wait_reason}


@router.delete(
    "/{conversation_id}/terminals/{session_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def close_terminal_session(
    conversation_id: uuid.UUID,
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Close one terminal session."""
    manager = terminal_manager()
    try:
        closed = await manager.close(
            session_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such terminal session.") from exc
    except TerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"closed": closed}


__all__ = ["router"]
