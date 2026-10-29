"""Live MCP resource tests against a real MCP server over HTTP.

The stdio proof reaches a real MCP server over a pipe.  The deployment also
supports the ``streamable_http`` transport, and this module proves the
resource path over it: a real MCP server listens on the loopback interface,
the product's own MCP integration is pointed at it, and the product's own
broker and resource tools answer with that server's own resources.

The server runs in the test's own event loop, so nothing outside the loopback
interface is reached.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

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


def _resource_server() -> Server:
    """Return a real MCP server that publishes resources, a template, and tools.

    The resources are published on two pages, the way a paginating server
    publishes them, and one of them carries binary content.

    Returns:
        The server.
    """

    server: Server = Server("live-http-resource-server")

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
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        target = str(uri)
        if target == NOTES_URI:
            return [ReadResourceContents(content=NOTES_TEXT, mime_type="text/plain")]
        if target == LOGO_URI:
            return [ReadResourceContents(content=LOGO_BYTES, mime_type="application/octet-stream")]
        if target == EMPTY_URI:
            return [ReadResourceContents(content="", mime_type="text/plain")]
        if target.startswith("file:///workspace/report/"):
            return [ReadResourceContents(content=f"report:{target}", mime_type="text/plain")]
        raise ValueError(f"Unknown resource: {uri}")

    return server


def _tools_only_server() -> Server:
    """Return a real MCP server that publishes tools and no resources.

    Returns:
        The server.
    """

    server: Server = Server("live-http-tools-only-server")

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

    return server


def _http_app(server: Server) -> Starlette:
    """Return the ASGI application that serves one MCP server over HTTP.

    The application is the one the MCP SDK ships: a streamable HTTP session
    manager in front of the server.  The host header check is switched off
    because the server listens on the loopback interface of the test.

    Args:
        server: The MCP server to serve.

    Returns:
        The application.
    """
    manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        stateless=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))],
        lifespan=lifespan,
    )


@contextlib.asynccontextmanager
async def _serve(server: Server) -> AsyncIterator[str]:
    """Serve one MCP server on the loopback interface and yield its URL.

    Args:
        server: The MCP server to serve.

    Yields:
        The URL of the server's MCP endpoint.
    """
    config = uvicorn.Config(
        _http_app(server),
        host="127.0.0.1",
        port=0,
        log_level="error",
        lifespan="on",
    )
    running = uvicorn.Server(config)
    task = asyncio.create_task(running.serve())
    try:
        for _ in range(500):
            if running.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - the server always binds a loopback port
            raise RuntimeError("The MCP server did not start.")
        port = running.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.should_exit = True
        await task


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
    the connected server, its connection policy, and the OAuth token an
    anonymous server does not have.
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


def _server_row(url: str) -> SimpleNamespace:
    """Return a connected HTTP MCP server that runs the live server."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        registry_entry_id=None,
        provider_slug=None,
        name="Live HTTP resources",
        transport="streamable_http",
        enabled=True,
        status="connected",
        catalog_revision=1,
        created_at=None,
        config={
            "oauth_mode": "none",
            "server_url": url,
            "mcp_sse_fallback": False,
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


@pytest.fixture
async def resource_server_url() -> AsyncIterator[str]:
    """Serve the MCP server that publishes resources over HTTP."""
    async with _serve(_resource_server()) as url:
        yield url


@pytest.fixture
async def tools_only_server_url() -> AsyncIterator[str]:
    """Serve the MCP server that publishes no resources over HTTP."""
    async with _serve(_tools_only_server()) as url:
        yield url


@pytest.mark.asyncio
async def test_the_http_server_is_a_real_mcp_server(resource_server_url: str) -> None:
    """The live HTTP server answers the product's own runtime with its tools."""
    server = _server_row(resource_server_url)
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
    resource_server_url: str,
) -> None:
    """The bridge follows the pagination cursor the live HTTP server sends."""
    bridge = _bridge(_server_row(resource_server_url))

    resources = await bridge.list_resources(auth=_auth(), conversation_id=CONVERSATION_ID)

    assert [resource.uri for resource in resources] == [NOTES_URI, LOGO_URI, EMPTY_URI]
    assert [resource.name for resource in resources] == ["Notes", "Logo", "Empty"]
    assert [resource.mime_type for resource in resources] == [
        "text/plain",
        "application/octet-stream",
        "text/plain",
    ]


@pytest.mark.asyncio
async def test_the_bridge_lists_the_resource_templates_of_the_live_http_server(
    resource_server_url: str,
) -> None:
    """The bridge reports the template the live HTTP server publishes."""
    bridge = _bridge(_server_row(resource_server_url))

    templates = await bridge.list_resource_templates(auth=_auth(), conversation_id=CONVERSATION_ID)

    assert len(templates) == 1
    assert templates[0].uri_template == "file:///workspace/report/{name}"
    assert templates[0].name == "Report"
    assert templates[0].mime_type == "text/plain"


@pytest.mark.asyncio
async def test_the_bridge_reads_the_text_of_a_live_http_resource(
    resource_server_url: str,
) -> None:
    """The bridge reads a text resource from the live HTTP server."""
    bridge = _bridge(_server_row(resource_server_url))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=NOTES_URI
    )

    assert len(contents) == 1
    assert contents[0].text == NOTES_TEXT
    assert contents[0].blob is None
    assert contents[0].mime_type == "text/plain"


@pytest.mark.asyncio
async def test_the_bridge_reads_the_base64_payload_of_a_live_http_binary_resource(
    resource_server_url: str,
) -> None:
    """The bridge reads a binary resource from the live HTTP server."""
    bridge = _bridge(_server_row(resource_server_url))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=LOGO_URI
    )

    assert len(contents) == 1
    assert contents[0].text is None
    assert contents[0].blob == LOGO_BLOB
    assert base64.b64decode(contents[0].blob) == LOGO_BYTES
    assert contents[0].mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_the_bridge_reads_a_live_http_resource_behind_a_template(
    resource_server_url: str,
) -> None:
    """A URI expanded from the live HTTP server's template is readable."""
    bridge = _bridge(_server_row(resource_server_url))

    contents = await bridge.read_resource(
        auth=_auth(), conversation_id=CONVERSATION_ID, uri=REPORT_URI
    )

    assert len(contents) == 1
    assert contents[0].text == REPORT_TEXT


@pytest.mark.asyncio
async def test_reading_a_uri_the_live_http_server_does_not_publish(
    resource_server_url: str,
) -> None:
    """The live HTTP server's own "unknown resource" answer is not a failure."""
    bridge = _bridge(_server_row(resource_server_url))

    with pytest.raises(Exception) as raised:
        await bridge.read_resource(
            auth=_auth(), conversation_id=CONVERSATION_ID, uri="file:///workspace/missing.md"
        )

    assert str(raised.value) == "No connected MCP server publishes that resource URI."


@pytest.mark.asyncio
async def test_the_tools_read_the_live_http_resources(resource_server_url: str) -> None:
    """The model-facing tools answer with the live HTTP server's own content."""
    context = _context(_server_row(resource_server_url))

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
async def test_the_tools_report_a_uri_no_live_http_server_publishes(
    resource_server_url: str,
) -> None:
    """The model-facing tools report an unpublished URI as a plain error."""
    context = _context(_server_row(resource_server_url))

    result = await _call_tool("read_mcp_resource", {"uri": "file:///workspace/missing.md"}, context)

    assert result == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }


@pytest.mark.asyncio
async def test_a_live_http_server_that_publishes_no_resources_lists_nothing(
    tools_only_server_url: str,
) -> None:
    """A server with tools but no resources publishes no resources.

    The live server answers the resource listings with the JSON-RPC "method
    not found" code, which the client reports as an exception group.
    """
    context = _context(_server_row(tools_only_server_url))

    listing = await _call_tool("list_mcp_resources", {}, context)
    templates = await _call_tool("list_mcp_resource_templates", {}, context)
    read = await _call_tool("read_mcp_resource", {"uri": NOTES_URI}, context)

    assert listing == {"status": "ok", "resources": []}
    assert templates == {"status": "ok", "templates": []}
    assert read == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }
