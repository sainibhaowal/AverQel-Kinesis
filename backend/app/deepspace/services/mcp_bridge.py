"""Narrow bridge from DeepSpace tool calling to the existing MCP runtime.

This module deliberately owns no OAuth or transport logic. MCP connection
ownership, catalog freshness, policy, and remote execution remain in the MCP
integration service; DeepSpace discovers tools from the user's connected MCP
accounts and forwards approved calls through that service.

The same connection scope serves the resource surface. A resource is reachable
only through a server the authenticated account has connected and its
connection policy enables, so a resource published by any other server stays
out of reach.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.types import PaginatedRequestParams
from pydantic import AnyUrl, TypeAdapter, ValidationError
from pydantic.networks import UrlConstraints
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.integrations.models.mcp_connection_policy import MCPConnectionPolicy
from app.integrations.models.mcp_server import MCPServer
from app.integrations.services.mcp_runtime import (
    MCPToolPolicyDecision,
    build_mcp_server_runtime,
    classify_mcp_error,
    evaluate_mcp_tool_policy,
    execute_mcp_server_tool,
    mcp_catalog_is_fresh,
    mcp_server_provider_available,
)

logger = logging.getLogger(__name__)

#: The MCP error code a server returns for a URI it does not publish.
MCP_RESOURCE_NOT_FOUND_CODE = -32002

#: The JSON-RPC error code a server returns for a method it does not implement.
MCP_METHOD_NOT_FOUND_CODE = -32601

#: The URI forms an MCP resource may use. The SDK accepts the same set.
_RESOURCE_URI: TypeAdapter[AnyUrl] = TypeAdapter(
    Annotated[AnyUrl, UrlConstraints(host_required=False)]
)

#: The wordings a server uses when it does not publish a resource URI. The
#: specification reserves an error code for that answer, but servers report it
#: in the message as well, and the official SDK reports it only in the message.
_NOT_PUBLISHED_MARKERS = (
    "not found",
    "not be found",
    "unknown",
    "no such",
    "does not exist",
    "not published",
)

#: The largest number of listing pages one server may publish.
MAX_LIST_PAGES = 100

#: The largest number of resources one listing returns.
MAX_RESOURCES = 200

#: The largest number of resource templates one listing returns.
MAX_RESOURCE_TEMPLATES = 200

#: The largest number of content items one resource read returns.
MAX_RESOURCE_CONTENTS = 100


class MCPResourceError(RuntimeError):
    """An MCP resource operation could not be completed."""


def _leaf_failures(error: BaseException) -> list[BaseException]:
    """Return the individual failures one failure carries.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        The failures that are not exception groups themselves.
    """
    if isinstance(error, BaseExceptionGroup):
        failures: list[BaseException] = []
        for nested in error.exceptions:
            failures.extend(_leaf_failures(nested))
        return failures
    return [error]


def _reported_failure(error: BaseException) -> BaseException:
    """Return the failure the remote service reported.

    The MCP client keeps its session in an anyio task group, and anyio
    re-raises an exception that leaves the group as an exception group.  The
    JSON-RPC error the server sent is one level down, and the group's own text
    names neither the code nor the message.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        The failure the remote service reported.
    """
    failures = _leaf_failures(error)
    for failure in failures:
        if getattr(failure, "error", None) is not None:
            return failure
    return failures[0] if failures else error


def _error_code(error: BaseException) -> int | None:
    """Return the JSON-RPC error code of a failure, when it carries one.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        The error code, or None when the failure is not a JSON-RPC error.
    """
    data = getattr(_reported_failure(error), "error", None)
    code = getattr(data, "code", None)
    return code if isinstance(code, int) else None


def _error_message(error: BaseException) -> str:
    """Return the message of a failure.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        The message the remote service sent, or the failure's own text.
    """
    reported = _reported_failure(error)
    data = getattr(reported, "error", None)
    message = getattr(data, "message", None)
    if isinstance(message, str) and message.strip():
        return message
    return str(reported)


def _is_resource_not_found(error: BaseException) -> bool:
    """Report whether a failure is the MCP "resource not found" answer.

    A server reports a URI it does not publish in several ways.  The
    specification reserves the JSON-RPC code -32002 for that answer, a server
    without resource support answers -32601, and the official SDK answers a
    failed handler with code 0 and a message such as
    "Unknown resource: file:///notes.md".  All of them mean the same thing to
    the caller: this server does not publish that URI.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        Whether the server reported that it does not publish the URI.
    """
    code = _error_code(error)
    if code in {MCP_RESOURCE_NOT_FOUND_CODE, MCP_METHOD_NOT_FOUND_CODE}:
        return True
    text = _error_message(error).lower()
    if "resource" not in text:
        return False
    return any(marker in text for marker in _NOT_PUBLISHED_MARKERS)


def _publishes_no_resources(error: BaseException) -> bool:
    """Report whether a server does not implement the resource methods.

    A server that publishes tools but no resources answers the resource
    listings with the JSON-RPC "method not found" code.  Such a server
    publishes nothing, which is not a failure of the listing.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        Whether the server has no resource support at all.
    """
    return _error_code(error) == MCP_METHOD_NOT_FOUND_CODE


def _failure_message(error: BaseException) -> str:
    """Return a message that names a remote failure without its details.

    Args:
        error: The failure raised by the MCP client.

    Returns:
        The message to show the model.
    """
    if isinstance(error, MCPResourceError):
        return str(error)
    return str(classify_mcp_error(_reported_failure(error))["message"])


async def _collect_pages(
    fetch: Callable[..., Awaitable[Any]],
    attribute: str,
) -> list[Any]:
    """Read every page of one MCP listing.

    Args:
        fetch: The listing method of the MCP client session.
        attribute: The result attribute that holds one page of items.

    Returns:
        Every item the server published.
    """
    items: list[Any] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        page = await fetch(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        items.extend(getattr(page, attribute, None) or [])
        cursor = getattr(page, "nextCursor", None)
        if not cursor:
            break
    return items


def _resource_entry(resource: Any) -> DeepSpaceMCPResource:
    """Return one published resource as a bridge entry.

    Args:
        resource: One resource of an MCP listing.

    Returns:
        The resource's URI, name, and media type.
    """
    return DeepSpaceMCPResource(
        uri=str(getattr(resource, "uri", "") or ""),
        name=str(getattr(resource, "name", "") or ""),
        mime_type=getattr(resource, "mimeType", None),
    )


def _template_entry(template: Any) -> DeepSpaceMCPResourceTemplate:
    """Return one published resource template as a bridge entry.

    Args:
        template: One resource template of an MCP listing.

    Returns:
        The template's URI template, name, and media type.
    """
    return DeepSpaceMCPResourceTemplate(
        uri_template=str(getattr(template, "uriTemplate", "") or ""),
        name=str(getattr(template, "name", "") or ""),
        mime_type=getattr(template, "mimeType", None),
    )


def _content_entry(content: Any) -> DeepSpaceMCPResourceContent:
    """Return one content item of a read resource as a bridge entry.

    Args:
        content: One content item of an MCP resource read.

    Returns:
        The content's text or base64 payload, with its media type.
    """
    text = getattr(content, "text", None)
    return DeepSpaceMCPResourceContent(
        text=None if text is None else str(text),
        blob=None if text is not None else str(getattr(content, "blob", "") or ""),
        mime_type=getattr(content, "mimeType", None),
    )


def _request_catalog_refresh(server: MCPServer, tenant_id: uuid.UUID) -> None:
    """Ask the maintenance worker to refresh a stale MCP catalog.

    Discovery must remain non-blocking: a temporarily slow remote MCP server
    must not prevent ordinary chat from starting.  The current cached catalog
    is still usable for this request while the worker refreshes it.
    """
    try:
        from app.integrations.workers.tasks_mcp import refresh_server_catalog

        refresh_server_catalog.delay(str(server.id), str(tenant_id))
    except Exception:  # noqa: BLE001
        # Queue availability must never make connected tools disappear.
        logger.warning("Unable to schedule MCP catalog refresh for %s", server.id, exc_info=True)


def _safe_function_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "tool"
    return normalized[:48]


@dataclass(frozen=True, slots=True)
class DeepSpaceMCPTool:
    """One namespaced, conversation-scoped MCP tool binding."""

    exposed_name: str
    server: MCPServer
    raw_name: str
    catalog: dict[str, Any]
    # Capture this while the discovery query is still live.  MCP execution
    # commits its own audit/policy work, which may expire the ORM server
    # instance.  Policy checks must not lazily refresh that instance later.
    catalog_revision: int
    server_name: str


@dataclass(frozen=True, slots=True)
class DeepSpaceMCPResource:
    """One resource a connected MCP server publishes."""

    uri: str
    name: str
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class DeepSpaceMCPResourceTemplate:
    """One parameterized resource URI a connected MCP server publishes."""

    uri_template: str
    name: str
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class DeepSpaceMCPResourceContent:
    """One content item of a read MCP resource."""

    text: str | None
    blob: str | None
    mime_type: str | None


class DeepSpaceMCPBridge:
    """Discover and execute tools from the user's connected MCP accounts."""

    MAX_SERVERS = 50
    MAX_TOOLS_PER_SERVER = 100

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    @staticmethod
    def _exposed_name(server: MCPServer, raw_name: str) -> str:
        # The short server id prevents collisions between two providers that
        # publish the same tool name while keeping the model-facing name small.
        return f"mcp_{server.id.hex[:10]}_{_safe_function_name(raw_name)}"[:64]

    def tools_for_conversation(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
    ) -> dict[str, DeepSpaceMCPTool]:
        # Lightweight provider/service test doubles do not expose a database
        # execute method; MCP remains simply unavailable in that environment.
        if not callable(getattr(self.db, "execute", None)):
            return {}
        servers = (
            self.db.execute(
                select(MCPServer)
                .where(
                    MCPServer.tenant_id == auth.tenant_id,
                    MCPServer.user_id == auth.user_id,
                    MCPServer.enabled.is_(True),
                    MCPServer.status == "connected",
                )
                .order_by(MCPServer.created_at.desc())
                .limit(self.MAX_SERVERS)
            )
            .scalars()
            .all()
        )

        bindings: dict[str, DeepSpaceMCPTool] = {}
        max_age = int(getattr(self.settings, "mcp_catalog_max_age_seconds", 3600))
        for server in servers:
            provider_available, _reason = mcp_server_provider_available(self.db, server)
            if not provider_available:
                continue

            config = server.config if isinstance(server.config, dict) else {}
            cached_tools = config.get("mcp_tools_cache")
            if not isinstance(cached_tools, list):
                continue
            if not mcp_catalog_is_fresh(server, max_age_seconds=max_age):
                # Keep the last known catalog available to DeepSpace while a
                # worker refreshes it.  Previously this branch silently hid a
                # connected Gmail/GitHub/etc. server after the cache aged out.
                # The execution policy still verifies ownership, connection,
                # risk, and approval before every call.
                _request_catalog_refresh(server, auth.tenant_id)

            policy = self.db.execute(
                select(MCPConnectionPolicy).where(
                    MCPConnectionPolicy.server_id == server.id,
                    MCPConnectionPolicy.tenant_id == auth.tenant_id,
                    MCPConnectionPolicy.user_id == auth.user_id,
                )
            ).scalar_one_or_none()
            if policy is None or not policy.default_enabled:
                continue
            for raw_tool in cached_tools[: self.MAX_TOOLS_PER_SERVER]:
                if not isinstance(raw_tool, dict):
                    continue
                raw_name = str(raw_tool.get("name") or "").strip()
                if not raw_name:
                    continue
                exposed_name = self._exposed_name(server, raw_name)
                bindings[exposed_name] = DeepSpaceMCPTool(
                    exposed_name=exposed_name,
                    server=server,
                    raw_name=raw_name,
                    catalog=dict(raw_tool),
                    catalog_revision=int(server.catalog_revision or 0),
                    server_name=str(server.name),
                )
        return bindings

    def policy_for_tool(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        binding: DeepSpaceMCPTool,
    ) -> MCPToolPolicyDecision:
        # Always use a fresh, tenant-owned row.  A previous MCP call may have
        # committed on this Session and expired ``binding.server``; touching
        # an expired attribute would issue an implicit refresh in the middle
        # of the stream and can poison the transaction.
        try:
            server = self.db.execute(
                select(MCPServer)
                .execution_options(populate_existing=True)
                .where(
                    MCPServer.id == binding.server.id,
                    MCPServer.tenant_id == auth.tenant_id,
                    MCPServer.user_id == auth.user_id,
                )
            ).scalar_one_or_none()
        except SQLAlchemyError:
            self.db.rollback()
            logger.exception("MCP policy lookup failed for %s", binding.exposed_name)
            return MCPToolPolicyDecision(False, reason="MCP connection database check failed.")
        if server is None:
            return MCPToolPolicyDecision(False, reason="MCP connection is no longer available.")
        return evaluate_mcp_tool_policy(
            db=self.db,
            server=server,
            tool_name=binding.raw_name,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            tool=binding.catalog,
            expected_catalog_revision=binding.catalog_revision,
            # Catalog freshness is handled with stale-while-revalidate during
            # discovery.  Do not reject a connected tool between refreshes.
            max_age_seconds=None,
        )

    async def execute(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        binding: DeepSpaceMCPTool,
        arguments: dict[str, Any],
        approval_granted: bool = False,
    ) -> dict[str, Any]:
        # Do not execute against an expired ORM object after a prior tool
        # commit.  The runtime receives the same tenant/user-owned row that
        # policy evaluation verified.
        server = self.db.execute(
            select(MCPServer)
            .execution_options(populate_existing=True)
            .where(
                MCPServer.id == binding.server.id,
                MCPServer.tenant_id == auth.tenant_id,
                MCPServer.user_id == auth.user_id,
            )
        ).scalar_one_or_none()
        if server is None:
            raise RuntimeError("MCP connection is no longer available.")
        return await execute_mcp_server_tool(
            db=self.db,
            settings=self.settings,
            server=server,
            tool_name=binding.raw_name,
            arguments=arguments,
            conversation_id=conversation_id,
            approval_granted=approval_granted,
        )

    def resource_servers(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
    ) -> list[MCPServer]:
        """Return the connected servers this conversation may read resources from.

        The scope matches the tool surface: the server belongs to the
        authenticated tenant and user, its connection is enabled and
        connected, its provider is still approved, and its connection policy
        enables it.  A policy that switches the server off for this
        conversation keeps its resources out of reach.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the call belongs to.

        Returns:
            The servers this conversation can read resources from.
        """
        # Lightweight provider/service test doubles do not expose a database
        # execute method; MCP remains simply unavailable in that environment.
        if not callable(getattr(self.db, "execute", None)):
            return []
        servers = (
            self.db.execute(
                select(MCPServer)
                .where(
                    MCPServer.tenant_id == auth.tenant_id,
                    MCPServer.user_id == auth.user_id,
                    MCPServer.enabled.is_(True),
                    MCPServer.status == "connected",
                )
                .order_by(MCPServer.created_at.desc())
                .limit(self.MAX_SERVERS)
            )
            .scalars()
            .all()
        )
        scoped: list[MCPServer] = []
        for server in servers:
            provider_available, _reason = mcp_server_provider_available(self.db, server)
            if not provider_available:
                continue
            policy = self.db.execute(
                select(MCPConnectionPolicy).where(
                    MCPConnectionPolicy.server_id == server.id,
                    MCPConnectionPolicy.tenant_id == auth.tenant_id,
                    MCPConnectionPolicy.user_id == auth.user_id,
                )
            ).scalar_one_or_none()
            if policy is None or not policy.default_enabled:
                continue
            if self._switched_off_for_conversation(policy, conversation_id):
                continue
            scoped.append(server)
        return scoped

    @staticmethod
    def _switched_off_for_conversation(
        policy: MCPConnectionPolicy,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Report whether the policy switches this server off for the conversation.

        Args:
            policy: The connection policy of the server.
            conversation_id: The conversation the call belongs to.

        Returns:
            Whether the conversation has no connection to the server.
        """
        overrides = (
            policy.conversation_overrides if isinstance(policy.conversation_overrides, dict) else {}
        )
        return overrides.get(str(conversation_id)) is False

    @asynccontextmanager
    async def _open_resource_session(self, server: MCPServer) -> AsyncIterator[Any]:
        """Open one MCP client session for a connected server.

        Args:
            server: The connected server to read from.

        Yields:
            The MCP client session.

        Raises:
            MCPResourceError: When the server has no usable runtime.
        """
        runtime = build_mcp_server_runtime(db=self.db, settings=self.settings, server=server)
        if runtime is None:
            raise MCPResourceError("The MCP connection is not available.")
        async with runtime.session() as session:
            yield session

    async def list_resources(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
    ) -> list[DeepSpaceMCPResource]:
        """List the resources the connected servers publish.

        A server that fails does not hide the resources the other servers
        published; the failure is logged and reported only when no server
        answered.  A server without resource support publishes nothing and
        contributes nothing.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the call belongs to.

        Returns:
            The published resources.

        Raises:
            MCPResourceError: When no connected server could list its
                resources.
        """
        entries: list[DeepSpaceMCPResource] = []
        failure: Exception | None = None
        for server in self.resource_servers(auth=auth, conversation_id=conversation_id):
            try:
                async with self._open_resource_session(server) as session:
                    items = await _collect_pages(session.list_resources, "resources")
            except Exception as exc:  # noqa: BLE001
                if _publishes_no_resources(exc):
                    logger.info("MCP server %s publishes no resources", server.id)
                    continue
                failure = exc
                logger.warning(
                    "MCP resource listing failed for server %s", server.id, exc_info=True
                )
                continue
            entries.extend(_resource_entry(item) for item in items)
            if len(entries) >= MAX_RESOURCES:
                break
        if not entries and failure is not None:
            raise MCPResourceError(_failure_message(failure)) from failure
        return entries[:MAX_RESOURCES]

    async def list_resource_templates(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
    ) -> list[DeepSpaceMCPResourceTemplate]:
        """List the resource URI templates the connected servers publish.

        A server that fails does not hide the templates the other servers
        published; the failure is logged and reported only when no server
        answered.  A server without resource support publishes no template and
        contributes nothing.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the call belongs to.

        Returns:
            The published resource templates.

        Raises:
            MCPResourceError: When no connected server could list its
                resource templates.
        """
        entries: list[DeepSpaceMCPResourceTemplate] = []
        failure: Exception | None = None
        for server in self.resource_servers(auth=auth, conversation_id=conversation_id):
            try:
                async with self._open_resource_session(server) as session:
                    items = await _collect_pages(
                        session.list_resource_templates, "resourceTemplates"
                    )
            except Exception as exc:  # noqa: BLE001
                if _publishes_no_resources(exc):
                    logger.info("MCP server %s publishes no resource templates", server.id)
                    continue
                failure = exc
                logger.warning(
                    "MCP resource template listing failed for server %s", server.id, exc_info=True
                )
                continue
            entries.extend(_template_entry(item) for item in items)
            if len(entries) >= MAX_RESOURCE_TEMPLATES:
                break
        if not entries and failure is not None:
            raise MCPResourceError(_failure_message(failure)) from failure
        return entries[:MAX_RESOURCE_TEMPLATES]

    async def read_resource(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        uri: str,
    ) -> list[DeepSpaceMCPResourceContent]:
        """Read one resource from the connected servers that publish it.

        The servers are asked in turn, because only the publishing server
        knows the URI.  A server that does not publish the URI is not a
        failure of the read, whether it answers with the specification's
        "resource not found" code, with the "method not found" code of a
        server without resource support, or with a message that names the
        resource as unknown.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the call belongs to.
            uri: The resource URI to read.

        Returns:
            The content items of the resource.

        Raises:
            MCPResourceError: When the URI is not a valid URI, when no
                connected server answers, or when no connected server
                publishes the URI.
        """
        target = self._validated_uri(uri)
        servers = self.resource_servers(auth=auth, conversation_id=conversation_id)
        if not servers:
            raise MCPResourceError("No connected MCP server is available in this conversation.")
        failure: Exception | None = None
        for server in servers:
            try:
                async with self._open_resource_session(server) as session:
                    result = await session.read_resource(target)
            except Exception as exc:  # noqa: BLE001
                if _is_resource_not_found(exc):
                    continue
                failure = exc
                logger.warning("MCP resource read failed for server %s", server.id, exc_info=True)
                continue
            contents = getattr(result, "contents", None) or []
            return [_content_entry(item) for item in contents[:MAX_RESOURCE_CONTENTS]]
        if failure is not None:
            raise MCPResourceError(_failure_message(failure)) from failure
        raise MCPResourceError("No connected MCP server publishes that resource URI.")

    @staticmethod
    def _validated_uri(uri: str) -> AnyUrl:
        """Return the validated form of one resource URI.

        Args:
            uri: The URI the model asked for.

        Returns:
            The validated URI.

        Raises:
            MCPResourceError: When the URI is not a valid URI.
        """
        try:
            return _RESOURCE_URI.validate_python(str(uri).strip())
        except ValidationError as exc:
            raise MCPResourceError("The resource URI is not a valid URI.") from exc
