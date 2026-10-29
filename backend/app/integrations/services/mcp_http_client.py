"""SSRF-safe HTTP clients used by native MCP and MCP OAuth flows."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from httpcore import SOCKET_OPTION, AsyncNetworkStream, NetworkStream
from httpx._client import UseClientDefault
from httpx._types import AuthTypes

from app.integrations.services.mcp_endpoint_security import (
    resolve_public_addresses,
    validate_remote_endpoint,
)


class MCPRedirectRejectedError(ValueError):
    """Raised when an MCP endpoint attempts an unvalidated redirect."""


class _PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to the exact public address resolved immediately before connect."""

    def __init__(self) -> None:
        # ``AsyncNetworkBackend`` is an interface in httpcore 1.x.  It can be
        # instantiated, but its ``connect_tcp`` implementation raises
        # ``NotImplementedError``.  That made every async native-MCP request
        # fail before it reached a public endpoint (including Google Gmail
        # MCP).  Use the concrete AnyIO implementation instead, while this
        # wrapper continues to pin every connection to a freshly validated
        # public address.
        self._backend = getattr(httpcore, "AnyIOBackend", httpcore.AsyncNetworkBackend)()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        errors: list[Exception] = []
        for address in resolve_public_addresses(host, port):
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        raise MCPRedirectRejectedError("MCP endpoint could not establish a safe connection") from (
            errors[-1] if errors else None
        )


class _PinnedSyncNetworkBackend(httpcore.NetworkBackend):
    """Synchronous equivalent of the pinned MCP network backend."""

    def __init__(self) -> None:
        self._backend = (
            httpcore.SyncBackend()
            if hasattr(httpcore, "SyncBackend")
            else getattr(httpcore, "AutoBackend", httpcore.SyncBackend)()
        )

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        errors: list[Exception] = []
        for address in resolve_public_addresses(host, port):
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        raise MCPRedirectRejectedError("MCP endpoint could not establish a safe connection") from (
            errors[-1] if errors else None
        )


#: The most redirects one MCP request may follow.
MAX_MCP_REDIRECTS = 5


def _same_origin_redirect(request: httpx.Request, response: httpx.Response) -> httpx.Request | None:
    """Return the request a same-origin redirect asks for.

    A server that mounts its endpoint at a path answers the request for the
    path without the trailing slash with a redirect to the path with it.  That
    redirect stays on the same host, so it is followed; a redirect to another
    host, or to another port, is refused as before.

    Args:
        request: The request that was sent.
        response: The redirect answer.

    Returns:
        The request to send next, or None when the redirect is refused.
    """
    location = response.headers.get("location")
    if not location:
        return None
    target = request.url.join(location)
    if (target.scheme, target.host, target.port) != (
        request.url.scheme,
        request.url.host,
        request.url.port,
    ):
        return None
    validate_remote_endpoint(str(target))
    return httpx.Request(
        method=request.method,
        url=target,
        headers=request.headers,
        content=request.content,
    )


class SafeMCPClient(httpx.Client):
    """Synchronous client that validates every destination and never follows redirects."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["follow_redirects"] = False
        super().__init__(*args, **kwargs)
        if hasattr(self, "_transport") and hasattr(self._transport, "_pool"):
            self._transport._pool._network_backend = _PinnedSyncNetworkBackend()

    def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: AuthTypes | UseClientDefault | None = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: bool | UseClientDefault = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        validate_remote_endpoint(str(request.url))
        current = request
        for _ in range(MAX_MCP_REDIRECTS):
            response = super().send(
                current,
                stream=stream,
                auth=auth,
                follow_redirects=False,
            )
            if not 300 <= response.status_code < 400:
                return response
            redirected = _same_origin_redirect(current, response)
            response.close()
            if redirected is None:
                raise MCPRedirectRejectedError("MCP endpoint redirects are not permitted")
            current = redirected
        raise MCPRedirectRejectedError("MCP endpoint redirected too many times")


class SafeMCPAsyncClient(httpx.AsyncClient):
    """Asynchronous client that validates every destination and never follows redirects."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["follow_redirects"] = False
        super().__init__(*args, **kwargs)
        if hasattr(self, "_transport") and hasattr(self._transport, "_pool"):
            self._transport._pool._network_backend = _PinnedAsyncNetworkBackend()

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: AuthTypes | UseClientDefault | None = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: bool | UseClientDefault = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        validate_remote_endpoint(str(request.url))
        current = request
        for _ in range(MAX_MCP_REDIRECTS):
            response = await super().send(
                current,
                stream=stream,
                auth=auth,
                follow_redirects=False,
            )
            if not 300 <= response.status_code < 400:
                return response
            redirected = _same_origin_redirect(current, response)
            await response.aclose()
            if redirected is None:
                raise MCPRedirectRejectedError("MCP endpoint redirects are not permitted")
            current = redirected
        raise MCPRedirectRejectedError("MCP endpoint redirected too many times")


def build_safe_sync_client(
    *, timeout: float = 30.0, headers: dict[str, str] | None = None
) -> SafeMCPClient:
    return SafeMCPClient(timeout=timeout, headers=headers, trust_env=False)


def build_safe_async_client(
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | float | None = None,
    auth: httpx.Auth | None = None,
) -> SafeMCPAsyncClient:
    return SafeMCPAsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)
