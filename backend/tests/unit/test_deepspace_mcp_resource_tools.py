"""Tests for the DeepSpace MCP resource tools."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import (
    BlobResourceContents,
    ErrorData,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)

from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.mcp_resource import (
    MCP_RESOURCE_TOOLS,
    ListMCPResourcesArguments,
    ListMCPResourceTemplatesArguments,
    ReadMCPResourceArguments,
)
from app.integrations.models.mcp_server import MCPServer

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()

NOTES_URI = "file:///workspace/notes.md"
LOGO_URI = "file:///workspace/logo.png"


class _FakeSession:
    """A stand-in for the MCP client session's resource methods."""

    def __init__(
        self,
        *,
        resources: list[Resource] | None = None,
        templates: list[ResourceTemplate] | None = None,
        contents: list[TextResourceContents | BlobResourceContents] | None = None,
        failure: Exception | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.resources = resources or []
        self.templates = templates or []
        self.contents = contents or []
        self.failure = failure
        self.missing = missing or set()
        self.read_uris: list[str] = []

    def _raise(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def list_resources(self, *, params: object = None) -> ListResourcesResult:
        self._raise()
        return ListResourcesResult(resources=self.resources)

    async def list_resource_templates(
        self, *, params: object = None
    ) -> ListResourceTemplatesResult:
        self._raise()
        return ListResourceTemplatesResult(resourceTemplates=self.templates)

    async def read_resource(self, uri: object) -> ReadResourceResult:
        self.read_uris.append(str(uri))
        self._raise()
        if str(uri) in self.missing:
            raise McpError(ErrorData(code=-32002, message="Resource not found"))
        return ReadResourceResult(contents=self.contents)


class _FakeRuntime:
    """A stand-in for the MCP connector runtime."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class _Result:
    """A stand-in for a SQLAlchemy result."""

    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[object]:
        return self.values

    def scalar_one_or_none(self) -> object | None:
        return self.values[0] if self.values else None


class _Db:
    """A stand-in for the request's database session."""

    def __init__(self, server: object, policy: object) -> None:
        self.server = server
        self.policy = policy

    def execute(self, statement: Any) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        if entity is MCPServer:
            return _Result([self.server])
        return _Result([self.policy])


def _server() -> SimpleNamespace:
    """Return a connected MCP server owned by the calling user."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        registry_entry_id=None,
        name="Files",
        enabled=True,
        status="connected",
        catalog_revision=1,
        created_at=None,
        config={},
    )


def _policy(**overrides: Any) -> SimpleNamespace:
    """Return the connection policy of the connected server."""
    policy = {
        "server_id": None,
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "default_enabled": True,
        "conversation_overrides": {},
    }
    policy.update(overrides)
    return SimpleNamespace(**policy)


def _context(db: Any) -> ToolContext:
    """Build a tool context for the calling conversation."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=SimpleNamespace(),
        db=db,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _install(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> _Db:
    """Connect one fake MCP session to the bridge the tools use."""
    server = _server()
    db = _Db(server, _policy(server_id=server.id))
    monkeypatch.setattr(
        "app.deepspace.services.mcp_bridge.build_mcp_server_runtime",
        lambda **_kwargs: _FakeRuntime(session),
    )
    return db


async def _call(name: str, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one registered resource tool."""
    spec = next(item for item in MCP_RESOURCE_TOOLS if item.name == name)
    return await spec.handler(arguments, context)


def test_the_resource_tools_declare_their_names_and_modes() -> None:
    assert [spec.name for spec in MCP_RESOURCE_TOOLS] == [
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    ]
    assert {spec.mode for spec in MCP_RESOURCE_TOOLS} == {"read"}
    assert MCP_RESOURCE_TOOLS[0].arguments_model is ListMCPResourcesArguments
    assert MCP_RESOURCE_TOOLS[1].arguments_model is ListMCPResourceTemplatesArguments
    assert MCP_RESOURCE_TOOLS[2].arguments_model is ReadMCPResourceArguments


def test_only_the_read_tool_takes_a_uri() -> None:
    listing = MCP_RESOURCE_TOOLS[0].definition["function"]["parameters"]
    templates = MCP_RESOURCE_TOOLS[1].definition["function"]["parameters"]
    read = MCP_RESOURCE_TOOLS[2].definition["function"]["parameters"]

    assert listing["properties"] == {}
    assert listing["required"] == []
    assert templates["properties"] == {}
    assert templates["required"] == []
    assert read["required"] == ["uri"]
    assert set(read["properties"]) == {"uri"}
    assert read["properties"]["uri"]["type"] == "string"


@pytest.mark.asyncio
async def test_listing_resources_returns_every_published_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        resources=[
            Resource(uri=NOTES_URI, name="Notes", mimeType="text/markdown"),
            Resource(uri=LOGO_URI, name="Logo"),
        ]
    )
    db = _install(monkeypatch, session)

    result = await _call("list_mcp_resources", {}, _context(db))

    assert result == {
        "status": "ok",
        "resources": [
            {"uri": NOTES_URI, "name": "Notes", "mime_type": "text/markdown"},
            {"uri": LOGO_URI, "name": "Logo", "mime_type": None},
        ],
    }


@pytest.mark.asyncio
async def test_listing_resource_templates_returns_every_published_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        templates=[
            ResourceTemplate(uriTemplate="file:///{path}", name="Files", mimeType="text/plain"),
        ]
    )
    db = _install(monkeypatch, session)

    result = await _call("list_mcp_resource_templates", {}, _context(db))

    assert result == {
        "status": "ok",
        "templates": [
            {"uriTemplate": "file:///{path}", "name": "Files", "mimeType": "text/plain"},
        ],
    }


@pytest.mark.asyncio
async def test_reading_a_resource_returns_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(
        contents=[
            TextResourceContents(uri=NOTES_URI, mimeType="text/markdown", text="# Notes"),
        ]
    )
    db = _install(monkeypatch, session)

    result = await _call("read_mcp_resource", {"uri": NOTES_URI}, _context(db))

    assert result == {
        "status": "ok",
        "uri": NOTES_URI,
        "contents": [{"text": "# Notes", "mimeType": "text/markdown"}],
    }
    assert session.read_uris == [NOTES_URI]


@pytest.mark.asyncio
async def test_reading_a_binary_resource_returns_its_base64_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        contents=[BlobResourceContents(uri=LOGO_URI, mimeType="image/png", blob="aGVsbG8=")]
    )
    db = _install(monkeypatch, session)

    result = await _call("read_mcp_resource", {"uri": LOGO_URI}, _context(db))

    assert result == {
        "status": "ok",
        "uri": LOGO_URI,
        "contents": [{"blob": "aGVsbG8=", "mimeType": "image/png"}],
    }


@pytest.mark.asyncio
async def test_an_unknown_resource_uri_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(missing={NOTES_URI})
    db = _install(monkeypatch, session)

    result = await _call("read_mcp_resource", {"uri": NOTES_URI}, _context(db))

    assert result["status"] == "error"
    assert result["message"] == "No connected MCP server publishes that resource URI."


@pytest.mark.asyncio
async def test_a_uri_that_is_not_a_uri_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    db = _install(monkeypatch, session)

    result = await _call("read_mcp_resource", {"uri": "notes.md"}, _context(db))

    assert result == {
        "status": "error",
        "message": "The resource URI is not a valid URI.",
    }
    assert session.read_uris == []


@pytest.mark.asyncio
async def test_a_failing_mcp_session_reports_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(failure=RuntimeError("the MCP service refused the connection"))
    db = _install(monkeypatch, session)
    context = _context(db)

    listing = await _call("list_mcp_resources", {}, context)
    templates = await _call("list_mcp_resource_templates", {}, context)
    read = await _call("read_mcp_resource", {"uri": NOTES_URI}, context)

    assert listing == {
        "status": "error",
        "message": "The connected MCP service returned an error. Please retry.",
    }
    assert templates == {
        "status": "error",
        "message": "The connected MCP service returned an error. Please retry.",
    }
    assert read == {
        "status": "error",
        "message": "The connected MCP service returned an error. Please retry.",
    }


@pytest.mark.asyncio
async def test_a_server_the_conversation_is_not_connected_to_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(resources=[Resource(uri=NOTES_URI, name="Notes")])
    server = _server()
    db = _Db(
        server,
        _policy(server_id=server.id, conversation_overrides={str(CONVERSATION_ID): False}),
    )
    monkeypatch.setattr(
        "app.deepspace.services.mcp_bridge.build_mcp_server_runtime",
        lambda **_kwargs: _FakeRuntime(session),
    )
    context = _context(db)

    listing = await _call("list_mcp_resources", {}, context)
    read = await _call("read_mcp_resource", {"uri": NOTES_URI}, context)

    assert listing == {"status": "ok", "resources": []}
    assert read == {
        "status": "error",
        "message": "No connected MCP server is available in this conversation.",
    }
    assert session.read_uris == []


@pytest.mark.asyncio
async def test_a_bridge_without_a_database_session_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.deepspace.services.mcp_bridge.build_mcp_server_runtime",
        lambda **_kwargs: _FakeRuntime(_FakeSession()),
    )

    result = await _call("list_mcp_resources", {}, _context(None))

    assert result == {
        "status": "error",
        "message": "This tool needs a database session.",
    }


def _grouped(*errors: Exception) -> BaseException:
    """Return failures the way the MCP client reports them.

    The client keeps its session in an anyio task group, and anyio re-raises
    an exception that leaves the group as an exception group.  The JSON-RPC
    error the server sent is one level down.
    """
    return ExceptionGroup("unhandled errors in a TaskGroup", list(errors))


@pytest.mark.asyncio
async def test_a_grouped_unknown_resource_answer_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's own "unknown resource" answer means the URI is not published.

    The MCP SDK answers a URI it does not publish with the error code 0 and a
    message that names the resource, and the client reports that answer as an
    exception group.
    """
    session = _FakeSession(
        failure=_grouped(
            RuntimeError("the transport closed"),
            McpError(ErrorData(code=0, message=f"Unknown resource: {NOTES_URI}")),
        )
    )
    db = _install(monkeypatch, session)

    result = await _call("read_mcp_resource", {"uri": NOTES_URI}, _context(db))

    assert result == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }


@pytest.mark.asyncio
async def test_a_server_without_resource_support_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that answers "method not found" publishes no resources.

    A server with tools but no resources answers the resource methods with
    the JSON-RPC "method not found" code, which is not a failure of the
    listing.
    """
    session = _FakeSession(
        failure=_grouped(McpError(ErrorData(code=-32601, message="Method not found")))
    )
    db = _install(monkeypatch, session)
    context = _context(db)

    listing = await _call("list_mcp_resources", {}, context)
    templates = await _call("list_mcp_resource_templates", {}, context)
    read = await _call("read_mcp_resource", {"uri": NOTES_URI}, context)

    assert listing == {"status": "ok", "resources": []}
    assert templates == {"status": "ok", "templates": []}
    assert read == {
        "status": "error",
        "message": "No connected MCP server publishes that resource URI.",
    }


@pytest.mark.asyncio
async def test_a_grouped_remote_failure_still_names_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grouped failure is classified by the message the server sent."""
    session = _FakeSession(
        failure=_grouped(McpError(ErrorData(code=-32000, message="Unauthorized: token expired")))
    )
    db = _install(monkeypatch, session)

    result = await _call("list_mcp_resources", {}, _context(db))

    assert result == {
        "status": "error",
        "message": "The connected MCP account rejected its access token. Reconnect it if this persists.",
    }
