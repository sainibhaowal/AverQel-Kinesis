"""The ``present`` tool for DeepSpace.

A coding agent writes files in the project workspace, but the user cannot
open a file the agent only mentioned in prose.  The ``present`` tool lets the
agent declare the files it has already written as the deliverables of the
turn, so the conversation can offer them to the user.

The tool declares; it does not copy.  A file stays where it is in the
workspace, and the declaration is recorded in the deliverable store next to
the turn that made it.  A path that leaves the workspace, or a file that does
not exist, refuses the call: a deliverable the user cannot open is worse
than no deliverable at all.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from app.deepspace.services.deliverables import declare_deliverable, deliverable_store
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    WorkspaceUnavailableError,
    resolve_within_root,
    workspace_root_for,
)

#: The largest number of files one call may present.
MAX_PRESENT_PATHS = 4

#: The largest path one call may name, in characters.
MAX_PATH_LENGTH = 1024

#: One workspace-relative path a call may name.
PresentPath = Annotated[str, Field(min_length=1, max_length=MAX_PATH_LENGTH)]


class PresentArguments(ToolArguments):
    """Declare files that already exist as this turn's deliverables."""

    paths: list[PresentPath] = Field(min_length=1, max_length=MAX_PRESENT_PATHS)


async def _present(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Declare files that already exist as this turn's deliverables."""
    raw_paths = [str(item) for item in arguments.get("paths") or []]
    try:
        root = context.workspace_root or workspace_root_for(
            context.settings, context.auth.tenant_id, context.auth.user_id
        )
    except WorkspaceUnavailableError as exc:
        return {"status": "unavailable", "message": str(exc)}
    refused: list[dict[str, str]] = []
    deliverables: list[dict[str, Any]] = []
    for raw in raw_paths:
        try:
            resolved = resolve_within_root(root, raw)
        except (WorkspacePathError, OSError) as exc:
            refused.append({"path": raw[:MAX_PATH_LENGTH], "reason": str(exc)})
            continue
        target = resolved.absolute
        if not target.exists():
            refused.append(
                {
                    "path": resolved.relative,
                    "reason": "No such file in the project workspace.",
                }
            )
            continue
        if target.is_dir():
            refused.append(
                {
                    "path": resolved.relative,
                    "reason": "The path is a directory; present a file.",
                }
            )
            continue
        try:
            size_bytes = target.stat().st_size
        except OSError as exc:
            refused.append(
                {
                    "path": resolved.relative,
                    "reason": f"The file could not be read: {exc}",
                }
            )
            continue
        deliverables.append({"path": resolved.relative, "size_bytes": size_bytes})
    if refused:
        return {
            "status": "error",
            "message": (
                "Every path must name an existing file inside the project workspace. "
                "Nothing was presented."
            ),
            "refused": refused,
        }
    turn = deliverable_store().current_turn(context.conversation_id)
    for item in deliverables:
        declare_deliverable(context.conversation_id, turn, str(item["path"]))
    return {"status": "ok", "deliverables": deliverables}


PRESENT_DESCRIPTION = (
    "Present files you have already written as the deliverables of this turn, so the "
    "user can open them. Name one to four workspace-relative paths of files that "
    "exist. A path outside the workspace, or a file that does not exist, refuses the "
    "whole call. Present the files the user asked for, not every file you touched."
)

PRESENT_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="present",
        description=PRESENT_DESCRIPTION,
        properties={
            "paths": array_property(
                "The workspace-relative paths of the files to present.",
                min_items=1,
                max_items=MAX_PRESENT_PATHS,
            ),
        },
        required=["paths"],
        arguments_model=PresentArguments,
        mode="write",
        handler=_present,
    ),
]

__all__ = ["MAX_PRESENT_PATHS", "PRESENT_TOOLS", "PresentArguments"]
