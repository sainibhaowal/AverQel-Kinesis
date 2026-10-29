"""Workspace instruction tools for DeepSpace."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    ToolContext,
    define_native_tool,
)
from app.deepspace.services.workspace_instructions import (
    get_workspace_roots,
    load_baseline_instructions,
)


class InstructionsReadArguments(ToolArguments):
    """Read the current workspace instruction set."""

    include_nested: bool = Field(
        default=True, description="Include nested AGENTS.md/CLAUDE.md files"
    )
    max_chars: int = Field(
        default=32000, ge=1000, le=200000, description="Maximum characters to return"
    )


class InstructionsReloadArguments(ToolArguments):
    """Force reload of workspace instructions (admin/owner only)."""

    pass


async def _instructions_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the current workspace instruction set."""
    workspace_root, project_root = get_workspace_roots(context.settings, context.auth)
    loaded = load_baseline_instructions(workspace_root, project_root)

    result = {
        "workspace_root": str(workspace_root),
        "project_root": str(project_root),
        "has_baseline": loaded.baseline is not None,
        "nested_count": len(loaded.nested),
    }

    if loaded.baseline:
        result["baseline"] = {
            "path": loaded.baseline.relative_path,
            "size": loaded.baseline.size,
            "content": loaded.baseline.content,
        }

    if arguments.get("include_nested", True) and loaded.nested:
        nested = []
        for f in loaded.nested:
            nested.append(
                {
                    "path": f.relative_path,
                    "size": f.size,
                    "content": f.content,
                }
            )
        result["nested"] = nested

    # Apply character limit
    max_chars = arguments.get("max_chars", 32000)
    if loaded.baseline and len(result["baseline"]["content"]) > max_chars:
        result["baseline"]["content"] = result["baseline"]["content"][:max_chars] + "\n[truncated]"

    return {"status": "ok", **result}


async def _instructions_reload(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Force reload of workspace instructions.

    This invalidates any cached instruction context for the conversation.
    """
    # The instruction context is computed fresh each turn, so just confirm
    workspace_root, project_root = get_workspace_roots(context.settings, context.auth)
    loaded = load_baseline_instructions(workspace_root, project_root)

    return {
        "status": "ok",
        "message": "Instruction context will be refreshed on next turn",
        "workspace_root": str(workspace_root),
        "has_baseline": loaded.baseline is not None,
        "nested_count": len(loaded.nested),
    }


INSTRUCTIONS_TOOLS = [
    define_native_tool(
        name="instructions_read",
        description="Read the current workspace AGENTS.md/CLAUDE.md instruction set",
        properties={
            "include_nested": {
                "type": "boolean",
                "description": "Include nested instruction files",
                "default": True,
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return",
                "default": 32000,
                "minimum": 1000,
                "maximum": 200000,
            },
        },
        arguments_model=InstructionsReadArguments,
        mode="read",
        handler=_instructions_read,
        required=[],
    ),
    define_native_tool(
        name="instructions_reload",
        description="Force reload of workspace instructions for the next turn",
        properties={},
        arguments_model=InstructionsReloadArguments,
        mode="read",
        handler=_instructions_reload,
        required=[],
    ),
]
