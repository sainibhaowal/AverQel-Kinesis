"""Tool discovery tool for DeepSpace.

Provides a catalogue lookup for available native tools.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    string_property,
)

# ============================================================
# Argument Model
# ============================================================


class DiscoverToolsArguments(ToolArguments):
    """Discover available native tools."""

    query: str = Field(min_length=2, max_length=1_000)
    tool_names: list[str] | None = Field(default=None, min_length=1, max_length=8)
    offset: int | None = Field(default=None, ge=0, le=100_000)


# ============================================================
# Handler
# ============================================================


async def _discover_tools(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Return the native tool catalogue matching the query."""
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY

    # Use the full registry from chat_service which includes all tools
    registry = NATIVE_TOOL_REGISTRY

    query = arguments["query"].lower()
    tool_names = arguments.get("tool_names")
    offset = arguments.get("offset", 0)
    limit = 50

    # Filter tools by query
    matching = []
    for name in registry.names():
        if tool_names and name not in tool_names:
            continue
        spec = registry.spec(name)
        desc = spec.provider_definition.get("function", {}).get("description", "").lower()
        if query in name.lower() or query in desc:
            matching.append(spec.emit_openai_compatible())

    # Apply pagination
    total = len(matching)
    page = matching[offset : offset + limit]

    return {
        "tools": page,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# ============================================================
# Tool Spec
# ============================================================

DISCOVER_TOOLS_TOOL = define_native_tool(
    name="discover_tools",
    description=(
        "Legacy compatibility catalogue lookup. Current DeepSpace turns should call the "
        "specific tools they need directly. This tool returns the native tool definitions "
        "matching a query string, with optional pagination."
    ),
    properties={
        "query": string_property(
            "Search query for tool names or descriptions.", min_length=2, max_length=1_000
        ),
        "tool_names": array_property(
            "Optional list of specific tool names to look up.", max_items=8
        ),
        "offset": {"type": "integer", "minimum": 0, "maximum": 100_000},
    },
    required=["query"],
    arguments_model=DiscoverToolsArguments,
    mode="read",
    handler=_discover_tools,
)

DISCOVER_TOOLS: list[NativeToolSpec] = [DISCOVER_TOOLS_TOOL]

__all__ = ["DISCOVER_TOOLS", "DISCOVER_TOOLS_TOOL", "DiscoverToolsArguments", "_discover_tools"]
