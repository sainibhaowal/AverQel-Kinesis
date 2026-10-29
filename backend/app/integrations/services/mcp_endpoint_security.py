"""Security checks for user-supplied MCP remote endpoints."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class MCPEndpointRejectedError(ValueError):
    pass


# Backward-compatible name for callers that imported the original exception.
MCPEndpointRejected = MCPEndpointRejectedError


def _allow_local_endpoints() -> bool:
    return os.getenv("ALLOW_LOCAL_MCP_ENDPOINTS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a host and return only public or safe local addresses."""
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise MCPEndpointRejectedError("MCP endpoint host could not be resolved") from exc

    # Always block cloud metadata service (169.254.169.254)
    for address in addresses:
        if str(address) == "169.254.169.254":
            raise MCPEndpointRejectedError("MCP endpoint resolves to cloud metadata service")

    allow_local = _allow_local_endpoints()
    if not allow_local:
        if not addresses or any(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            for address in addresses
        ):
            raise MCPEndpointRejectedError("MCP endpoint resolves to a restricted network")
    else:
        if not addresses or any(
            address.is_multicast or address.is_reserved or address.is_unspecified
            for address in addresses
        ):
            raise MCPEndpointRejectedError("MCP endpoint resolves to a restricted network")

    return tuple(sorted(str(address) for address in addresses))


def validate_remote_endpoint(raw: str) -> str:
    value = str(raw or "").strip()
    parsed = urlparse(value)
    allow_local = _allow_local_endpoints()
    allowed_schemes = {"https", "http"} if allow_local else {"https"}

    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        if allow_local:
            raise MCPEndpointRejectedError(
                "MCP endpoint must be an HTTP or HTTPS URL without embedded credentials"
            )
        raise MCPEndpointRejectedError(
            "MCP endpoint must be an HTTPS URL without embedded credentials"
        )
    host = parsed.hostname.rstrip(".").lower()
    if host == "metadata.google.internal":
        raise MCPEndpointRejectedError("MCP endpoint host is not allowed")
    if not allow_local and host in {"localhost", "localhost.localdomain"}:
        raise MCPEndpointRejectedError("MCP endpoint host is not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise MCPEndpointRejectedError("MCP endpoint has an invalid port") from exc
    resolve_public_addresses(host, port)
    return value
