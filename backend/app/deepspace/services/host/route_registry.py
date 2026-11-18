"""HTTP route registry for DeepSpace host web server.

Provides route registration with exact/prefix/upgrade/fallback routes,
disposers for cleanup, and route matching.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Route:
    """An HTTP route registration."""

    pattern: str
    handler: Callable[[Request], Awaitable[Response]]
    methods: frozenset[str] = field(default_factory=lambda: frozenset({"GET", "HEAD"}))
    is_prefix: bool = False


@dataclass(frozen=True, slots=True)
class UpgradeRoute:
    """A WebSocket upgrade route."""

    path: str
    handler: Callable[[Request], Awaitable[None]]


class RouteRegistry:
    """Registry for HTTP routes with exact/prefix/upgrade/fallback support."""

    def __init__(self) -> None:
        self._exact_routes: dict[str, Route] = {}
        self._prefix_routes: list[Route] = []
        self._upgrade_routes: dict[str, UpgradeRoute] = {}
        self._fallback_handler: Callable[[Request], Awaitable[Response]] | None = None

    def register_exact(
        self,
        path: str,
        handler: Callable[[Request], Awaitable[Response]],
        methods: set[str] | None = None,
    ) -> Callable[[], None]:
        """Register an exact-match route. Returns a disposer."""
        if path in self._exact_routes:
            raise ValueError(f"Exact route already registered: {path}")
        route = Route(
            pattern=path,
            handler=handler,
            methods=frozenset(methods or {"GET", "HEAD"}),
            is_prefix=False,
        )
        self._exact_routes[path] = route
        return lambda: self._exact_routes.pop(path, None)

    def register_prefix(
        self,
        prefix: str,
        handler: Callable[[Request], Awaitable[Response]],
        methods: set[str] | None = None,
    ) -> Callable[[], None]:
        """Register a prefix-match route. Returns a disposer."""
        for existing in self._prefix_routes:
            if existing.pattern == prefix:
                raise ValueError(f"Prefix route already registered: {prefix}")
        route = Route(
            pattern=prefix,
            handler=handler,
            methods=frozenset(methods or {"GET", "HEAD"}),
            is_prefix=True,
        )
        self._prefix_routes.append(route)
        # Sort by length descending for longest-prefix-first matching
        self._prefix_routes.sort(key=lambda r: len(r.pattern), reverse=True)
        return lambda: self._prefix_routes.remove(route)

    def register_upgrade(
        self,
        path: str,
        handler: Callable[[Request], Awaitable[None]],
    ) -> Callable[[], None]:
        """Register a WebSocket upgrade route. Returns a disposer."""
        if path in self._upgrade_routes:
            raise ValueError(f"Upgrade route already registered: {path}")
        self._upgrade_routes[path] = UpgradeRoute(path=path, handler=handler)
        return lambda: self._upgrade_routes.pop(path, None)

    def register_fallback(
        self,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[], None]:
        """Register the fallback handler. Returns a disposer."""
        if self._fallback_handler is not None:
            raise ValueError("Fallback handler already registered")
        self._fallback_handler = handler
        return lambda: setattr(self, "_fallback_handler", None)

    def match(self, path: str, method: str) -> Route | None:
        """Match a request path to a route."""
        # Exact match first
        exact = self._exact_routes.get(path)
        if exact and method in exact.methods:
            return exact
        # Prefix match (longest first)
        for route in self._prefix_routes:
            if path.startswith(route.pattern) and method in route.methods:
                return route
        return None

    def match_upgrade(self, path: str) -> UpgradeRoute | None:
        """Match a WebSocket upgrade path."""
        return self._upgrade_routes.get(path)

    def get_fallback(self) -> Callable[[Request], Awaitable[Response]] | None:
        return self._fallback_handler

    def clear(self) -> None:
        """Clear all routes."""
        self._exact_routes.clear()
        self._prefix_routes.clear()
        self._upgrade_routes.clear()
        self._fallback_handler = None


__all__ = ["RouteRegistry", "Route", "UpgradeRoute"]
