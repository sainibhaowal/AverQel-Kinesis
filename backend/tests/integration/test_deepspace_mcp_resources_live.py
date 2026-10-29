"""Live MCP resource tests against a real MCP server.

The unit tests for the DeepSpace MCP resource tools answer with a fake MCP
session, so they prove the bridge's own logic but not the MCP client's real
answers.  These tests run the product's bridge and the product's tools against
a real MCP server process that speaks the protocol over stdio, so the client's
real answers decide what the product does.
"""

from __future__ import annotations

import base64
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import get_settings
from app.deepspace.services.mcp_bridge import DeepSpaceMCPBridge
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.mcp_resource import MCP_RESOURCE_TOOLS
from app.integrations.models.mcp_connection_policy import MCPConnectionPolicy
from app.integrations.models.mcp_server import MCPOAuthToken, MCPServer
from app.integrations.services.mcp_runtime import build_mcp_server_runtime

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()

NOTES_URI = "file:///workspace/notes.md"
LOGO_URI = "file:///workspace/logo.png"
EMPTY_URI = "file:///workspace/empty.txt"
REPORT_URI = "file:///workspace/report/q3.md"

NOTES_TEXT = "# Notes\n"
REPORT_TEXT = f"report:{REPORT_URI}"
LOGO_BYTES = b"\x89PNG\r\n\x1a\n averqel"
LOGO_BLOB = base64.b64encode(LOGO_BYTES).decode()

#: A real MCP server that publishes two resources of different media types on
#: one page, a third on a second page, one resource template, and two tools.
_RESOURCE_SERVER_SOURCE = r'''
"""An MCP server that publishes resources, a resource template, and tools."""

from __future__ import annotations

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

NOTES_URI = "file:///workspace/notes.md"
LOGO_URI = "file:///workspace/logo.png"
EMPTY_URI = "file:///workspace/empty.txt"
REPORT_PREFIX = "file:///workspace/report/"

LOGO_BYTES = b"\x89PNG\r\n\x1a\n averqel"

server = Server("live-resource-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="echo",
            description="Echo a message back.",
            inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
        ),
        types.Tool(
            name="add",
            description="Add two numbers.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"{name}:{sorted(arguments.items())}")]


@server.list_resources()
async def list_resources(request: types.ListResourcesRequest) -> types.ListResourcesResult:
    """Publish the resources on two pages, the way a paginating server does."""
    cursor = request.params.cursor if request.params else None
    if cursor is None:
        return types.ListResourcesResult(
            resources=[
                types.Resource(uri=NOTES_URI, name="Notes", mimeType="text/plain"),
                types.Resource(uri=LOGO_URI, name="Logo", mimeType="application/octet-stream"),
            ],
            nextCursor="page-2",
        )
    return types.ListResourcesResult(
        resources=[types.Resource(uri=EMPTY_URI, name="Empty", mimeType="text/plain")]
    )


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            uriTemplate="file:///workspace/report/{name}",
            name="Report",
            mimeType="text/plain",
        )
    ]


@server.read_resource()
async def read_resource(uri) -> list[ReadResourceContents]:
    target = str(uri)
    if target == NOTES_URI:
        return [ReadResourceContents(content="# Notes\n", mime_type="text/plain")]
    if target == LOGO_URI:
        return [ReadResourceContents(content=LOGO_BYTES, mime_type="application/octet-stream")]
    if target == EMPTY_URI:
        return [ReadResourceContents(content="", mime_type="text/plain")]
    if target.startswith(REPORT_PREFIX):
        return [ReadResourceContents(content=f"report:{target}", mime_type="text/plain")]
    raise ValueError(f"Unknown resource: {uri}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
'''

#: A real MCP server that publishes tools but no resources at all.
_TOOLS_ONLY_SERVER_SOURCE = r'''
"""An MCP server that publishes tools and no resources."""

from __future__ import annotations

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

server = Server("live-tools-only-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="echo",
            description="Echo a message back.",
            inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"{name}:{sorted(arguments.items())}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
'''


class _Result:
    """A stand-in for a SQLAlchemy result."""

    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[object]:
        return self._values

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None


class _Db:
    """A stand-in for the request's database session.

    The live server is reached through the product's own runtime, so the
    session only has to answer the lookups the bridge and that runtime make:
    the connected server, its connection policy, and the OAuth token a stdio
    server does not have.
    """

    def __init__(self, server: object, policy: object) -> None:
        self._server = server
        self._policy = policy

    def execute(self, statement: Any) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        if entity is MCPServer:
            return _Result([self._server])
        if entity is MCPConnectionPolicy:
            return _Result([self._policy])
        if entity is MCPOAuthToken:
            return _Result([])
        return _Result([self._policy])


def _server_row(script: Path) -> SimpleNamespace:
    """Return a connected stdio MCP server that runs the live server script."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        registry_entry_id=None,
        provider_slug=None,
        name="Live resources",
        transport="stdio",
        enabled=True,
        status="connected",
        catalog_revision=1,
        created_at=None,
        config={
            "oauth_mode": "none",
            "command": sys.executable,
            "args": [str(script)],
        },
    )


def _policy_row(server: SimpleNamespace) -> SimpleNamespace:
    """Return the connection policy of the connected server."""
    return SimpleNamespace(
        server_id=server.id,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        default_enabled=True,
        conversation_overrides={},
    )


def _auth() -> SimpleNamespace:
    """Return the authenticated tenant and user that own the call."""
    return SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID)


def _write_server(tmp_path: Path, name: str, source: str) -> Path:
    """Write one live MCP server script the client can spawn."""
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script


@pytest.fixture(scope="module")
def resource_server(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the script of the live server that publishes resources."""
    tmp_path = tmp_path_factory.mktemp("mcp-resource-server")
    return _write_server(tmp_path, "resource_server.py", _RESOURCE_SERVER_SOURCE)


@pytest.fixture(scope="module")
def tools_only_server(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the script of the live server that publishes no resources."""
    tmp_path = tmp_path_factory.mktemp("mcp-tools-only-server")
    return _write_server(tmp_path, "tools_only_server.py", _TOOLS_ONLY_SERVER_SOURCE)


def _context(server: SimpleNamespace) -> ToolContext:
    """Return the tool context of the calling conversation."""
    return ToolContext(
        auth=_auth(),
        conversation_id=CONVERSATION_ID,
        settings=get_settings(),
        db=_Db(server, _policy_row(server)),
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _bridge(server: SimpleNamespace) -> DeepSpaceMCPBridge:
    """Return the bridge of the calling conversation."""
    return DeepSpaceMCPBridge(_Db(server, _policy_row(server)), get_settings())


async def _call_tool(
    name: str,
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """Run one registered resource tool."""
    spec = next(item for item in MCP_RESOURCE_TOOLS if item.name == name)
    return await spec.handler(arguments, context)


@pytest.mark.asyncio
async def test_the_live_server_is_a_real_mcp_server(resource_server: Path) -> None:
    """The live server answers the product's own runtime with its tools."""
    server = _server_row(resource_server)
    runtime = build_mcp_server_runtime(
        db=_Db(server, _policy_row(server)),
        settings=get_settings(),
        server=server,
    )

    assert runtime is not None
    tools = await runtime.list_tools()

    assert [tool["name"] for tool in tools] == ["echo", "add"]


@pytest.mark.asyncio
async def test_the_bridge_lists_the_resources_of_every_page(
    resource_server: Path,
) -> None:
    """The bridge follows the pagination cursor the live server sends."""
    bridge = _bridge(_server_row(resource_server))

    resources = await bridge.list_resources(auth=_auth(), conversation_id=CONVERSATION_ID)

    assert [resource.uri for resource in resources] == [NOTES_URI, LOGO_URI, EMPTY_URI]
    assert [resource.name for resource in resources] == ["Notes", "Logo", "Empty"]
    assert [resource.mime_type for resource in resources] == [
        "text/plain",
        "application/octet-stream",
        "text/plain",
    ]


@pytest.mark.asyncio
async def test_the_bridge_lists_the_resource_templates_of_the_live_server(
    resource_server: Path,
) -> None:
    """The bridge reports the template the live server publishes."""
    bridge = _bridge(_server_row(resource_server))

    templates = await bridge.list_resource_templates(auth=_auth(), conversation_id=CONVERSATION_ID)

    assert len(templates) == 1
    assert templates[0].uri_template == "file:///workspace/report/{name}"
    assert templates[0].name == "Report"
    assert templates[0].mime_type == "text/plain"


@pytest.mark.asyncio
async def test_the_bridge_reads_the_text_of_a_live_resource(resource_server: Path) -> None:
    """The bridge reads a text resource from the live server."""
    bridge = _bridge(_server_row(resource_server))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=NOTES_URI
    )

    assert len(contents) == 1
    assert contents[0].text == NOTES_TEXT
    assert contents[0].blob is None
    assert contents[0].mime_type == "text/plain"


@pytest.mark.asyncio
async def test_the_bridge_reads_the_base64_payload_of_a_live_binary_resource(
    resource_server: Path,
) -> None:
    """The bridge reads a binary resource from the live server."""
    bridge = _bridge(_server_row(resource_server))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=LOGO_URI
    )

    assert len(contents) == 1
    assert contents[0].text is None
    assert contents[0].blob == LOGO_BLOB
    assert base64.b64decode(contents[0].blob) == LOGO_BYTES
    assert contents[0].mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_the_bridge_reads_a_live_resource_whose_text_is_empty(
    resource_server: Path,
) -> None:
    """An empty resource keeps its empty text instead of becoming a blob."""
    bridge = _bridge(_server_row(resource_server))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=EMPTY_URI
    )

    assert len(contents) == 1
    assert contents[0].text == ""
    assert contents[0].blob is None


@pytest.mark.asyncio
async def test_the_bridge_reads_a_live_resource_behind_a_template(
    resource_server: Path,
) -> None:
    """A URI expanded from the live server's template is readable."""
    bridge = _bridge(_server_row(resource_server))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=REPORT_URI
    )

    assert len(contents) == 1
    assert contents[0].text == REPORT_TEXT


@pytest.mark.asyncio
async def test_reading_a_uri_the_live_server_does_not_publish(
    resource_server: Path,
) -> None:
    """The live server's own "unknown resource" answer is not a failure.

    The MCP client reports the answer as an exception group that carries the
    server's message one level down, so the bridge has to unwrap it to learn
    that the server does not publish the URI.
    """
    bridge = _bridge(_server_row(resource_server))

    with pytest.raises(Exception) as raised:
        await bridge.read_resource(
            auth=_auth(), conversation_id=CONVERSATION_ID, uri="file:///workspace/missing.md"
        )

    assert str(raised.value) == "No connected MCP server publishes that resource URI."


@pytest.mark.asyncio
async def test_the_tools_read_the_live_resources(resource_server: Path) -> None:
    """The model-facing tools answer with the live server's own content."""
    server = _server_row(resource_server)
    context = _context(server)

    listing = await _call_tool("list_mcp_resources", {}, context)
    templates = await _call_tool("list_mcp_resource_templates", {}, context)
    text = await _call_tool("read_mcp_resource", {"uri": NOTES_URI}, context)
    binary = await _call_tool("read_mcp_resource", {"uri": LOGO_URI}, context)
    empty = await _call_tool("read_mcp_resource", {"uri": EMPTY_URI}, context)

    assert listing == {
        "status": "ok",
        "resources": [
            {"uri": NOTES_URI, "name": "Notes", "mime_type": "text/plain"},
            {"uri": LOGO_URI, "name": "Logo", "mime_type": "application/octet-stream"},
            {"uri": EMPTY_URI, "name": "Empty", "mime_type": "text/plain"},
        ],
    }
    assert templates == {
        "status": "ok",
        "templates": [
            {
                "uriTemplate": "file:///workspace/report/{name}",
                "name": "Report",
                "mimeType": "text/plain",
            }
        ],
    }
    assert text == {
        "status": "ok",
        "uri": NOTES_URI,
        "contents": [{"text": NOTES_TEXT, "mimeType": "text/plain"}],
    }
    assert binary == {
        "status": "ok",
        "uri": LOGO_URI,
        "contents": [{"blob": LOGO_BLOB, "mimeType": "application/octet-stream"}],
    }
    assert empty == {
        "status": "ok",
        "uri": EMPTY_URI,
        "contents": [{"text": "", "mimeType": "text/plain"}],
    }


@pytest.mark.asyncio
async def test_the_tools_report_a_uri_no_live_server_publishes(
    resource_server: Path,
) -> None:
    """The model-facing tools report an unpublished URI as a plain error."""
    context = _context(_server_row(resource_server))

    result = await _call_tool("read_mcp_resource", {"uri": "file:///workspace/missing.md"}, context)

    assert result == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }


@pytest.mark.asyncio
async def test_a_live_server_that_publishes_no_resources_lists_nothing(
    tools_only_server: Path,
) -> None:
    """A server with tools but no resources publishes no resources.

    The live server answers the resource listings with the JSON-RPC "method
    not found" code, which the client reports as an exception group.
    """
    context = _context(_server_row(tools_only_server))

    listing = await _call_tool("list_mcp_resources", {}, context)
    templates = await _call_tool("list_mcp_resource_templates", {}, context)
    read = await _call_tool("read_mcp_resource", {"uri": NOTES_URI}, context)

    assert listing == {"status": "ok", "resources": []}
    assert templates == {"status": "ok", "templates": []}
    assert read == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }
