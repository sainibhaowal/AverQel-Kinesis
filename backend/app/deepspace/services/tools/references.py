"""File and session reference tools for DeepSpace."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.file_references import get_file_reference_service
from app.deepspace.services.session_references import (
    get_session_reference_resolver,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    ToolContext,
    define_native_tool,
    require_db,
)
from app.deepspace.services.workspace_root import workspace_root_for


class FileReferenceArguments(ToolArguments):
    """List workspace file candidates for @path completion."""

    query: str = Field(
        min_length=1, max_length=1024, description="Path prefix or pattern to search"
    )
    max_results: int = Field(default=50, ge=1, le=200, description="Maximum candidates to return")


class SessionReferenceArguments(ToolArguments):
    """Resolve @session mentions into structured snapshots."""

    mentions: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Session mentions to resolve (e.g., 'session:<uuid>', 'session:<uuid>:10')",
    )
    max_bytes: int = Field(
        default=50000, ge=10000, le=500000, description="Maximum total bytes for all references"
    )
    max_turns_per_session: int = Field(
        default=10, ge=1, le=50, description="Maximum turns per session"
    )


async def _file_reference(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List workspace file candidates for @path completion."""
    query = str(arguments["query"])
    max_results = arguments.get("max_results", 50)

    workspace_root = workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    service = get_file_reference_service(context.settings)

    candidates = await service.list(workspace_root, query)

    # Limit results
    candidates = candidates[:max_results]

    return {
        "status": "ok",
        "query": query,
        "candidates": [
            {
                "path": c.path,
                "is_directory": c.is_directory,
                "size": c.size,
                "mime_type": c.mime_type,
            }
            for c in candidates
        ],
        "count": len(candidates),
    }


async def _session_reference(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Resolve @session mentions into structured snapshots."""
    mentions = [str(m).strip() for m in arguments.get("mentions", []) if str(m).strip()]
    max_bytes = arguments.get("max_bytes", 50000)
    max_turns = arguments.get("max_turns_per_session", 10)

    if not mentions:
        return {"status": "error", "message": "No mentions provided"}

    # Get session query service
    from app.deepspace.services.session_query import SessionQueryService

    session_query = SessionQueryService(require_db(context))

    resolver = get_session_reference_resolver(session_query, context.settings)

    # Override limits for this call
    resolver._max_bytes = max_bytes
    resolver._max_refs = len(mentions)

    sessions = await resolver.resolve(mentions, context.auth)

    return {
        "status": "ok",
        "mentions": mentions,
        "sessions": [
            {
                "conversation_id": str(s.conversation_id),
                "title": s.title,
                "updated_at": s.updated_at,
                "turn_count": s.turn_count,
                "message_count": s.message_count,
                "snippet": s.snippet,
                "turns": s.turns,
            }
            for s in sessions
        ],
        "count": len(sessions),
        "total_bytes": sum(len(str(s)) for s in sessions),
    }


REFERENCE_TOOLS = [
    define_native_tool(
        name="file_reference",
        description=(
            "List workspace file candidates for @path completion. "
            "Returns files and directories matching the query prefix."
        ),
        properties={
            "query": {
                "type": "string",
                "description": "Path prefix or pattern to search",
                "minLength": 1,
                "maxLength": 1024,
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum candidates to return",
                "default": 50,
                "minimum": 1,
                "maximum": 200,
            },
        },
        arguments_model=FileReferenceArguments,
        mode="read",
        handler=_file_reference,
        required=["query"],
    ),
    define_native_tool(
        name="session_reference",
        description=(
            "Resolve @session mentions into structured snapshots. "
            "Mentions: 'session:<uuid>' or 'session:<uuid>:N' for N turns."
        ),
        properties={
            "mentions": {
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "minItems": 1,
                "maxItems": 10,
                "description": "Session mentions to resolve",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Maximum total bytes for all references",
                "default": 50000,
                "minimum": 10000,
                "maximum": 500000,
            },
            "max_turns_per_session": {
                "type": "integer",
                "description": "Maximum turns per session",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
        },
        arguments_model=SessionReferenceArguments,
        mode="read",
        handler=_session_reference,
        required=["mentions"],
    ),
]
