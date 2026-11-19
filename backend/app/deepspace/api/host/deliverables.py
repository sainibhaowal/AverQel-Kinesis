"""Deliverables API routes for DeepSpace."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.deliverables import DeliverablesService
from app.platform.database.session import get_db

router = APIRouter(prefix="/host", tags=["deliverables"])


class PresentFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    description: str | None = None


class PresentRequest(BaseModel):
    files: list[dict[str, str]] = Field(..., min_length=1, max_length=8)


class PresentResponse(BaseModel):
    presented: int
    files: list[dict[str, str]]


@router.post(
    "/chats/{conversation_id}/present",
    dependencies=[Depends(get_auth_context)],
)
async def present_files(
    conversation_id: str,
    request: PresentRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> PresentResponse:
    """Present files as deliverables for the user."""
    _ = DeliverablesService(db)

    # Validate conversation ownership
    # TODO: Add conversation ownership check

    files = []
    for f in request.files:
        if not f.get("path"):
            raise HTTPException(status_code=400, detail="File path is required")
        files.append(
            {
                "path": f["path"],
                "description": f.get("description"),
            }
        )

    # In a real implementation, this would be called from the chat service
    # during tool execution. For now, we just return success.
    return PresentResponse(
        presented=len(files),
        files=[{"path": f["path"], "description": f.get("description", "")} for f in request.files],
    )


@router.get(
    "/chats/{conversation_id}/workspace-changes",
    dependencies=[Depends(get_auth_context)],
)
async def list_workspace_changes(
    conversation_id: str,
    turn: int | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List workspace changes for a conversation."""
    conv_id = uuid.UUID(conversation_id)

    # Query session events for workspace/changes
    query = select(DeepSpaceSessionEvent).where(
        DeepSpaceSessionEvent.conversation_id == conv_id,
        DeepSpaceSessionEvent.event_type == "workspace/changes",
    )
    if turn is not None:
        query = query.where(DeepSpaceSessionEvent.turn == turn)
    query = query.order_by(DeepSpaceSessionEvent.turn.desc())

    events = db.execute(query).scalars().all()

    entries = []
    for event in events:
        payload = event.payload or {}
        entries.extend(payload.get("entries", []))

    total_files = sum(1 for _ in entries)
    total_lines_added = sum(e.get("lines_added", 0) for e in entries)
    total_lines_removed = sum(e.get("lines_removed", 0) for e in entries)

    return {
        "entries": entries,
        "total_files": total_files,
        "total_lines_added": total_lines_added,
        "total_lines_removed": total_lines_removed,
    }


@router.get(
    "/chats/{conversation_id}/workspace-changes/{turn}",
    dependencies=[Depends(get_auth_context)],
)
async def get_workspace_changes(
    conversation_id: str,
    turn: int,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get workspace changes for a specific turn."""
    conv_id = uuid.UUID(conversation_id)

    query = select(DeepSpaceSessionEvent).where(
        DeepSpaceSessionEvent.conversation_id == conv_id,
        DeepSpaceSessionEvent.event_type == "workspace/changes",
        DeepSpaceSessionEvent.turn == turn,
    )
    event = db.execute(query).scalar_one_or_none()

    if event is None:
        return {
            "turn": turn,
            "entries": [],
            "total_files": 0,
            "total_lines_added": 0,
            "total_lines_removed": 0,
        }

    payload = event.payload or {}
    entries = payload.get("entries", [])
    total_files = len(entries)
    total_lines_added = sum(e.get("lines_added", 0) for e in entries)
    total_lines_removed = sum(e.get("lines_removed", 0) for e in entries)

    return {
        "turn": turn,
        "entries": entries,
        "total_files": total_files,
        "total_lines_added": total_lines_added,
        "total_lines_removed": total_lines_removed,
    }


@router.get(
    "/chats/{conversation_id}/workspace-changes/{turn}/diff/{file_index}",
    dependencies=[Depends(get_auth_context)],
)
async def get_workspace_change_diff(
    conversation_id: str,
    turn: int,
    file_index: int,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get the diff for a specific file in a turn."""
    conv_id = uuid.UUID(conversation_id)

    query = select(DeepSpaceSessionEvent).where(
        DeepSpaceSessionEvent.conversation_id == conv_id,
        DeepSpaceSessionEvent.event_type == "workspace/changes",
        DeepSpaceSessionEvent.turn == turn,
    )
    event = db.execute(query).scalar_one_or_none()

    if event is None:
        return {
            "diff": "",
            "file_path": "",
            "lines_added": 0,
            "lines_removed": 0,
        }

    payload = event.payload or {}
    entries = payload.get("entries", [])

    if file_index < 0 or file_index >= len(entries):
        raise HTTPException(status_code=404, detail="File index out of range")

    entry = entries[file_index]
    file_path = entry.get("path", "")

    # Generate diff using git
    import subprocess
    from pathlib import Path

    # Get workspace root
    try:
        workspace_root = Path.cwd()  # This would need to be the actual workspace root
        result = subprocess.run(
            ["git", "diff", "--", file_path],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff = result.stdout if result.returncode == 0 else ""
    except Exception:
        diff = ""

    return {
        "diff": diff,
        "file_path": file_path,
        "lines_added": entry.get("lines_added", 0),
        "lines_removed": entry.get("lines_removed", 0),
    }


__all__ = ["router"]
