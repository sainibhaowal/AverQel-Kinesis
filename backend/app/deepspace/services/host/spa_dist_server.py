"""SPA Dist Server for DeepSpace host.

Claims the webserver fallback seat and serves the built frontend with
traversal rejection, index authorization, SPA index fallback, ETags, and
document base injection.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app.deepspace.services.host.web_server import WebServerService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SPADistConfig:
    """Configuration for the SPA dist server."""

    dist_index: str  # Absolute path to index.html
    authorize_index: Callable[[str | None], Awaitable[bool]]  # Returns True if authorized
    render_index: Callable[[str, str | None], str]  # Renders index with injections/taps
    index_path: str = "/"


class SPADistServer:
    """
    SPA dist server that claims the webserver fallback seat and serves
    the built frontend with security and performance features.
    """

    def __init__(self, web_server: WebServerService, config: SPADistConfig) -> None:
        self.web_server = web_server
        self.config = config
        self.dist_root = Path(config.dist_index).parent
        self._index_path = config.index_path
        self._index_cache: str | None = None
        self._etag_cache: dict[str, str] = {}
        self._seat_claimed = False

        # Claim the fallback seat
        self._fallback_disposer = web_server.register_fallback(self._handle_fallback)
        self._seat_claimed = True

        # Register index route
        web_server.register_exact_route(self._index_path, self._handle_index, {"GET", "HEAD"})

        logger.info(f"SPA dist server claimed fallback seat, serving from {self.dist_root}")

    @property
    def dist_root_path(self) -> Path:
        return self.dist_root

    def dispose(self) -> None:
        """Release the fallback seat."""
        if self._seat_claimed:
            self._fallback_disposer()
            self._seat_claimed = False
            self._index_cache = None
            self._etag_cache.clear()

    async def _handle_index(self, request: Request) -> Response:
        """Handle index.html requests with authorization."""
        # Check authorization
        peer = request.headers.get("x-deepspace-peer")
        authorized = await self.config.authorize_index(peer)

        if not authorized:
            # Return 401 or redirect to login
            return RedirectResponse(url="/login", status_code=303)

        # Read and cache index.html
        if self._index_cache is None:
            index_file = self.dist_root / "index.html"
            if not index_file.is_file():
                return PlainTextResponse("index.html not found", status_code=404)
            self._index_cache = index_file.read_text(encoding="utf-8")

        # Render with injections and taps
        rendered = self.config.render_index(self._index_cache, peer)

        # Inject document base
        rendered = self._inject_document_base(rendered)

        return HTMLResponse(content=rendered, status_code=200)

    async def _handle_fallback(self, request: Request) -> Response:
        """Handle fallback requests - serve static files or 404."""
        path = request.url.path

        # Skip index path - already handled
        if path == self._index_path or path == self._index_path + "/":
            return await self._handle_index(request)

        # Resolve path against dist root
        try:
            resolved = self._resolve_path(path)
        except ValueError:
            return PlainTextResponse("Forbidden", status_code=403)

        # Check if file exists
        if not resolved.is_file():
            # For SPA, we could serve index.html here, but DSH returns 404
            return PlainTextResponse("Not Found", status_code=404)

        # Generate ETag
        etag = self._get_etag(resolved)
        if_none_match = request.headers.get("if-none-match")
        if if_none_match == etag:
            return Response(status_code=304)

        # Serve file
        response = FileResponse(
            path=str(resolved),
            headers={
                "ETag": etag,
                "Cache-Control": "no-cache",
            },
        )
        return response

    def _resolve_path(self, path: str) -> Path:
        """Resolve a request path to a file path, rejecting traversal."""
        # Remove leading slash
        path = path.lstrip("/")

        # Normalize path
        normalized = posixpath.normpath(path)

        # Check for traversal
        if normalized.startswith("..") or os.path.isabs(normalized):
            raise ValueError("Traversal attempt")

        # Resolve against dist root
        resolved = (self.dist_root / normalized).resolve()

        # Ensure it's under dist root
        try:
            resolved.relative_to(self.dist_root.resolve())
        except ValueError as exc:
            raise ValueError("Traversal attempt") from exc

        return resolved

    def _get_etag(self, file_path: Path) -> str:
        """Get or compute ETag for a file."""
        cache_key = str(file_path)
        if cache_key in self._etag_cache:
            return self._etag_cache[cache_key]

        # Compute weak ETag from SHA-1 of file content
        hasher = hashlib.sha1()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        etag = f'W/"{hasher.hexdigest()}"'
        self._etag_cache[cache_key] = etag
        return etag

    def _inject_document_base(self, html: str) -> str:
        """Inject <base href="./"> after <head> tag."""
        # Insert after <head> tag
        head_end = html.find("</head>")
        if head_end != -1:
            base_tag = '<base href="./">'
            return html[:head_end] + base_tag + "\n" + html[head_end:]

        # Fallback: inject after <head>
        head_start = html.find("<head")
        if head_start != -1:
            head_end = html.find(">", head_start)
            if head_end != -1:
                base_tag = '<base href="./">'
                return html[: head_end + 1] + base_tag + "\n" + html[head_end + 1 :]

        return html

    def invalidate_cache(self) -> None:
        """Invalidate index and ETag caches."""
        self._index_cache = None
        self._etag_cache.clear()


def create_spa_dist_server(
    web_server: WebServerService,
    dist_index: str,
    authorize_index: Callable[[str | None], Awaitable[bool]],
    render_index: Callable[[str, str | None], str],
    index_path: str = "/",
) -> SPADistServer:
    """Factory function to create and configure an SPA dist server."""
    config = SPADistConfig(
        dist_index=dist_index,
        authorize_index=authorize_index,
        render_index=render_index,
        index_path=index_path,
    )
    return SPADistServer(web_server, config)


__all__ = ["SPADistServer", "SPADistConfig", "create_spa_dist_server"]
