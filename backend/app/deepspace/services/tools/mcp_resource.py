"""MCP resource tools for DeepSpace.

A connected MCP server publishes resources as well as tools.  These three
tools let the agent discover what the connected servers publish and read it.
The server is chosen by the bridge from the connections this conversation has,
so a resource published by a server the conversation is not connected to stays
out of reach.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from app.deepspace.services.mcp_bridge import (
    DeepSpaceMCPBridge,
    DeepSpaceMCPResourceContent,
    MCPResourceError,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    require_db,
    string_property,
)

logger = logging.getLogger(__name__)

#: The largest number of characters one resource URI may hold.
MAX_RESOURCE_URI_CHARS = 2_048


class ListMCPResourcesArguments(ToolArguments):
    """List the resources the connected MCP servers publish."""


class ListMCPResourceTemplatesArguments(ToolArguments):
    """List the resource URI templates the connected MCP servers publish."""


class ReadMCPResourceArguments(ToolArguments):
    """Read one resource a connected MCP server publishes."""

    uri: str = Field(min_length=1, max_length=MAX_RESOURCE_URI_CHARS)


def _bridge(context: ToolContext) -> DeepSpaceMCPBridge:
    """Return the MCP bridge for the calling request.

    Args:
        context: The calling request's tool context.

    Returns:
        The bridge, scoped to the request's database session.

    Raises:
        MCPResourceError: When the request has no database session.
    """
    try:
        db = require_db(context)
    except RuntimeError as exc:
        raise MCPResourceError(str(exc)) from exc
    return DeepSpaceMCPBridge(db, context.settings)


def _content_payload(content: DeepSpaceMCPResourceContent) -> dict[str, Any]:
    """Return one content item in the form the model receives.

    Args:
        content: The content item the server returned.

    Returns:
        The text or the base64 payload, with the media type.
    """
    if content.text is not None:
        return {"text": content.text, "mimeType": content.mime_type}
    return {"blob": content.blob or "", "mimeType": content.mime_type}


async def _list_mcp_resources(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """List the resources the connected MCP servers publish."""
    del arguments
    try:
        resources = await _bridge(context).list_resources(
            auth=context.auth,
            conversation_id=context.conversation_id,
        )
    except MCPResourceError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception:  # noqa: BLE001
        logger.warning("Listing MCP resources failed", exc_info=True)
        return {"status": "error", "message": "The MCP resource listing failed."}
    return {
        "status": "ok",
        "resources": [
            {"uri": item.uri, "name": item.name, "mime_type": item.mime_type} for item in resources
        ],
    }


async def _list_mcp_resource_templates(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """List the resource URI templates the connected MCP servers publish."""
    del arguments
    try:
        templates = await _bridge(context).list_resource_templates(
            auth=context.auth,
            conversation_id=context.conversation_id,
        )
    except MCPResourceError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception:  # noqa: BLE001
        logger.warning("Listing MCP resource templates failed", exc_info=True)
        return {"status": "error", "message": "The MCP resource template listing failed."}
    return {
        "status": "ok",
        "templates": [
            {
                "uriTemplate": item.uri_template,
                "name": item.name,
                "mimeType": item.mime_type,
            }
            for item in templates
        ],
    }


async def _read_mcp_resource(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """Read one resource a connected MCP server publishes."""
    uri = str(arguments["uri"]).strip()
    try:
        contents = await _bridge(context).read_resource(
            auth=context.auth,
            conversation_id=context.conversation_id,
            uri=uri,
        )
    except MCPResourceError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception:  # noqa: BLE001
        logger.warning("Reading the MCP resource %s failed", uri, exc_info=True)
        return {"status": "error", "message": "The MCP resource read failed."}
    return {
        "status": "ok",
        "uri": uri,
        "contents": [_content_payload(item) for item in contents],
    }


LIST_MCP_RESOURCES_DESCRIPTION = (
    "List the resources the MCP servers this conversation is connected to publish. "
    "Each entry carries the resource URI to read with read_mcp_resource, the "
    "resource name, and the media type when the server publishes one."
)

LIST_MCP_RESOURCE_TEMPLATES_DESCRIPTION = (
    "List the resource URI templates the MCP servers this conversation is connected "
    "to publish. A template such as file://{path} describes a family of resource "
    "URIs; expand it and read the result with read_mcp_resource."
)

READ_MCP_RESOURCE_DESCRIPTION = (
    "Read one resource published by an MCP server this conversation is connected to. "
    "Use a URI from list_mcp_resources, or a URI you expanded from a template in "
    "list_mcp_resource_templates."
)

MCP_RESOURCE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="list_mcp_resources",
        description=LIST_MCP_RESOURCES_DESCRIPTION,
        properties={},
        arguments_model=ListMCPResourcesArguments,
        mode="read",
        handler=_list_mcp_resources,
    ),
    define_native_tool(
        name="list_mcp_resource_templates",
        description=LIST_MCP_RESOURCE_TEMPLATES_DESCRIPTION,
        properties={},
        arguments_model=ListMCPResourceTemplatesArguments,
        mode="read",
        handler=_list_mcp_resource_templates,
    ),
    define_native_tool(
        name="read_mcp_resource",
        description=READ_MCP_RESOURCE_DESCRIPTION,
        properties={
            "uri": string_property(
                "The resource URI to read.",
                min_length=1,
                max_length=MAX_RESOURCE_URI_CHARS,
            ),
        },
        required=["uri"],
        arguments_model=ReadMCPResourceArguments,
        mode="read",
        handler=_read_mcp_resource,
    ),
]

__all__ = [
    "MCP_RESOURCE_TOOLS",
    "ListMCPResourceTemplatesArguments",
    "ListMCPResourcesArguments",
    "ReadMCPResourceArguments",
]
