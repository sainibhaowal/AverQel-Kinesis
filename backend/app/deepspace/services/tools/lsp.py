"""LSP navigation tool for DeepSpace."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.lsp import (
    LspPosition,
    LspQueryResult,
    get_lsp_service,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    ToolContext,
    define_native_tool,
)
from app.deepspace.services.workspace_root import (
    workspace_root_for,
)


class LspArguments(ToolArguments):
    """LSP navigation query."""

    operation: Literal["goToDefinition", "findReferences", "goToImplementation", "hover"] = Field(
        description="The LSP operation to perform"
    )
    path: str = Field(
        min_length=1, max_length=1024, description="File path relative to workspace root"
    )
    line: int = Field(ge=1, description="One-based line number")
    character: int = Field(ge=1, description="One-based character (UTF-16) position")


LSP_PROMPT_TEXT = (
    "Use search/read for ordinary navigation. Use lsp when textual matches are ambiguous "
    "or before a change requires precise definitions, implementations, or references. "
    "Positions are one-based line and character (UTF-16) at the cursor; "
    "an off-symbol position may return no results. findReferences always includes the declaration."
)


async def _lsp_handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Handle LSP navigation query."""
    operation = arguments["operation"]
    path = arguments["path"]
    line = arguments["line"]
    character = arguments["character"]

    # Get workspace root
    workspace_root = workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )

    # Get LSP service
    lsp_service = get_lsp_service(context.settings)

    # Execute query
    position = LspPosition(line=line, character=character)
    result = await lsp_service.query(workspace_root, operation, path, position)

    return _format_result(result)


def _format_result(result: LspQueryResult) -> dict[str, Any]:
    """Format LSP query result for model consumption."""
    if result.error:
        return {
            "status": "error",
            "operation": result.operation,
            "error": result.error,
        }

    if result.hover:
        return {
            "status": "ok",
            "operation": result.operation,
            "hover": {
                "contents": result.hover.contents,
                "range": result.hover.range,
            },
        }

    # Format locations
    locations = []
    for loc in result.locations:
        locations.append(
            {
                "uri": loc.uri,
                "range": loc.range,
            }
        )

    return {
        "status": "ok",
        "operation": result.operation,
        "locations": locations,
        "count": len(locations),
    }


LSP_TOOLS = [
    define_native_tool(
        name="lsp",
        description=(
            "Navigate code with LSP: goToDefinition, findReferences, goToImplementation, hover. "
            "Requires a language server configured for the file's extension."
        ),
        properties={
            "operation": {
                "type": "string",
                "enum": ["goToDefinition", "findReferences", "goToImplementation", "hover"],
                "description": "The LSP operation to perform",
            },
            "path": {
                "type": "string",
                "description": "File path relative to workspace root",
                "minLength": 1,
                "maxLength": 1024,
            },
            "line": {
                "type": "integer",
                "description": "One-based line number",
                "minimum": 1,
            },
            "character": {
                "type": "integer",
                "description": "One-based character (UTF-16) position",
                "minimum": 1,
            },
        },
        arguments_model=LspArguments,
        mode="read",
        handler=_lsp_handler,
        required=["operation", "path", "line", "character"],
    ),
]
