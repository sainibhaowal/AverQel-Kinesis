"""DeepSpace Host Web Server Service.

Provides the HTTP server with route registry, fallback seat, index rendering,
TLS support, and integration with Starlette/FastAPI.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import (
    Response,
)
from starlette.routing import Mount, WebSocketRoute
from starlette.routing import Route as StarletteRoute
from starlette.staticfiles import StaticFiles

from app.deepspace.services.host.index_renderer import IndexInjection, IndexRenderer
from app.deepspace.services.host.route_registry import RouteRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebServerConfig:
    """Configuration for the host web server."""

    host: str = "127.0.0.1"
    port: int = 3000
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    compression: bool = False
    compression_threshold_bytes: int = 1024
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    dist_index: str | None = None  # Path to SPA index.html


class WebServerService:
    """
    Host web server service with route registry, fallback seat, index rendering,
    TLS support, and SPA dist serving.
    """

    def __init__(self, config: WebServerConfig) -> None:
        self.config = config
        self.route_registry = RouteRegistry()
        self.index_renderer = IndexRenderer()
        self._app: Starlette | None = None
        self._server: Any | None = None
        self._dist_root: Path | None = None
        self._fallback_claimed = False

    @property
    def protocol(self) -> str:
        """Return 'http:' or 'https:' based on TLS configuration."""
        return "https:" if self.config.tls_cert_file and self.config.tls_key_file else "http:"

    @property
    def origin(self) -> str:
        """Return the full origin URL (protocol + host + port)."""
        proto = self.protocol
        host = self.config.host
        port = self.config.port
        if (proto == "https:" and port == 443) or (proto == "http:" and port == 80):
            return f"{proto}//{host}"
        return f"{proto}//{host}:{port}"

    def is_loopback_host(self, host: str) -> bool:
        """Check if a host is loopback."""
        return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")

    def is_wildcard_host(self, host: str) -> bool:
        """Check if host is a wildcard (0.0.0.0 or ::)."""
        return host in ("0.0.0.0", "::", "::0")

    def register_exact_route(
        self,
        path: str,
        handler: Callable[[Request], Awaitable[Response]],
        methods: set[str] | None = None,
    ) -> Callable[[], None]:
        """Register an exact-match route. Returns a disposer."""
        return self.route_registry.register_exact(path, handler, methods)

    def register_prefix_route(
        self,
        prefix: str,
        handler: Callable[[Request], Awaitable[Response]],
        methods: set[str] | None = None,
    ) -> Callable[[], None]:
        """Register a prefix-match route. Returns a disposer."""
        return self.route_registry.register_prefix(prefix, handler, methods)

    def register_upgrade_route(
        self,
        path: str,
        handler: Callable[[Request], Awaitable[None]],
    ) -> Callable[[], None]:
        """Register a WebSocket upgrade route. Returns a disposer."""
        return self.route_registry.register_upgrade(path, handler)

    def register_fallback(
        self,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[], None]:
        """Register the fallback handler. Returns a disposer."""
        if self._fallback_claimed:
            raise RuntimeError("Fallback seat already claimed")
        self._fallback_claimed = True
        return self.route_registry.register_fallback(handler)

    def add_index_injection(self, injection: IndexInjection) -> Callable[[], None]:
        """Add an index injection row. Returns a disposer."""
        return self.index_renderer.add_injection(injection)

    def add_index_tap(self, transform: Callable[[str], str]) -> Callable[[], None]:
        """Add an index tap transform. Returns a disposer."""
        return self.index_renderer.add_tap(transform)

    def render_index(self, html: str, peer: str | None = None) -> str:
        """Render index.html with injections and taps for a peer."""
        rendered = self.index_renderer.render_index(html, peer)
        return self.index_renderer.apply_taps(rendered)

    def get_origin(self) -> str:
        """Get the origin URL for this server."""
        return self.origin

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build SSL context from cert/key files."""
        if not self.config.tls_cert_file or not self.config.tls_key_file:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.config.tls_cert_file, self.config.tls_key_file)
        return ctx

    def _create_app(self) -> Starlette:
        """Create the Starlette application with all routes."""
        routes: list[StarletteRoute | WebSocketRoute | Mount] = []

        # Add exact routes from registry
        for path, route in self.route_registry._exact_routes.items():
            routes.append(StarletteRoute(path, route.handler, methods=list(route.methods)))

        # Add prefix routes
        for route in self.route_registry._prefix_routes:
            routes.append(StarletteRoute(route.pattern, route.handler, methods=list(route.methods)))

        # Add WebSocket upgrade routes
        for path, upgrade in self.route_registry._upgrade_routes.items():
            routes.append(WebSocketRoute(path, upgrade.handler))

        # Add SPA dist server if configured
        if self.config.dist_index:
            self._dist_root = Path(self.config.dist_index).parent
            routes.append(
                Mount("/", StaticFiles(directory=self._dist_root, html=True), name="static")
            )

        # Add fallback handler
        fallback = self.route_registry.get_fallback()
        if fallback:
            routes.append(StarletteRoute("/{path:path}", fallback))

        middleware = [
            Middleware(
                GZipMiddleware,
                minimum_size=(
                    self.config.compression_threshold_bytes if self.config.compression else 1024
                ),
            ),
            Middleware(
                CORSMiddleware,
                allow_origins=self.config.cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        ]

        return Starlette(routes=routes, middleware=middleware)

    async def start(self) -> None:
        """Start the server."""
        import uvicorn

        self._app = self._create_app()
        ssl_context = self._build_ssl_context()

        config = uvicorn.Config(
            app=self._app,
            host=self.config.host,
            port=self.config.port,
            ssl_certfile=self.config.tls_cert_file if ssl_context else None,
            ssl_keyfile=self.config.tls_key_file if ssl_context else None,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        """Stop the server."""
        if self._server:
            self._server.should_exit = True


@dataclass(frozen=True, slots=True)
class WebServerStatus:
    """Runtime status of the web server."""

    configured_backend: str
    selected_backend: str
    can_cage: bool
    network_enabled: bool
    platform: str
    enforcement_level: str
    error_code: str | None
    runners: list[str]


def is_loopback_host(host: str) -> bool:
    """Check if a host is loopback (module-level function for external use)."""
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def is_wildcard_host(host: str) -> bool:
    """Check if host is a wildcard (0.0.0.0 or ::)."""
    return host in ("0.0.0.0", "::", "::0")


def normalize_bind_address(host: str) -> str:
    """Normalize a bind address to its canonical form."""
    if host in ("0.0.0.0", "::", "::0"):
        return "0.0.0.0"
    if host.startswith("::ffff:"):
        return host[7:]
    if host.startswith("::0.0.0."):
        return "::1"
    return host


def bind_url_host(host: str) -> str | None:
    """Return the host as a URL host, or None for non-loopback zoned addresses."""
    normalized = normalize_bind_address(host)
    if ":" in normalized and not normalized.startswith("["):
        return f"[{normalized}]"
    return normalized


__all__ = [
    "WebServerConfig",
    "WebServerService",
    "WebServerStatus",
    "is_loopback_host",
    "is_wildcard_host",
    "normalize_bind_address",
    "bind_url_host",
]
