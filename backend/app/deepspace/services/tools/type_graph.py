"""The type-graph tool for DeepSpace.

One tool answers the questions a coding agent asks about a codebase: what
is defined where, and what refers to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.type_graph import build_type_graph, graph_fingerprint


class TypeGraphArguments(ToolArguments):
    """Read the type graph of the workspace."""

    name: str | None = Field(default=None, max_length=200)
    path: str | None = Field(default=None, max_length=400)


def _workspace(context: ToolContext) -> Path:
    """Return the workspace the call runs in.

    Args:
        context: The calling request's tool context.

    Returns:
        The workspace root.
    """
    chosen = getattr(context, "workspace_root", None)
    if chosen is not None:
        return Path(chosen)
    settings = getattr(context, "settings", None)
    auth = getattr(context, "auth", None)
    if settings is not None and auth is not None:
        from app.deepspace.services.workspace_root import workspace_root_for

        return Path(workspace_root_for(settings, auth.tenant_id, auth.user_id))
    return Path.cwd()


async def _type_graph(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the type graph of the workspace."""
    root = _workspace(context)
    graph = build_type_graph(root)
    name = str(arguments.get("name") or "").strip()
    path = str(arguments.get("path") or "").strip()
    if name:
        definitions = graph.definitions_of(name)
        references = graph.references_to(name)
        return {
            "status": "ok",
            "name": name,
            "definitions": [
                {
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "path": symbol.path,
                    "line": symbol.line,
                    "signature": symbol.signature,
                }
                for symbol in definitions
            ],
            "references": [
                {"source": edge.source, "path": edge.path, "line": edge.line} for edge in references
            ],
        }
    symbols = [
        {
            "name": symbol.name,
            "qualified_name": symbol.qualified_name,
            "kind": symbol.kind,
            "path": symbol.path,
            "line": symbol.line,
        }
        for symbol in sorted(graph.symbols.values(), key=lambda item: (item.path, item.line))
        if not path or symbol.path.startswith(path)
    ]
    return {
        "status": "ok",
        "root": graph.root,
        "files": len(graph.files),
        "fingerprint": graph_fingerprint(graph),
        "symbols": symbols,
    }


TYPE_GRAPH_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="type_graph",
        description=(
            "Reads the type graph of the workspace: the classes, functions, methods, "
            "interfaces, and types it declares, and the references between them. "
            "Give a name to see where it is defined and what refers to it."
        ),
        properties={
            "name": string_property(
                "The name to look for. The whole graph is returned when it is absent.",
                max_length=200,
            ),
            "path": string_property(
                "A workspace-relative path to limit the graph to.",
                max_length=400,
            ),
        },
        arguments_model=TypeGraphArguments,
        mode="read",
        handler=_type_graph,
    ),
]

__all__ = ["TYPE_GRAPH_TOOLS", "TypeGraphArguments"]
