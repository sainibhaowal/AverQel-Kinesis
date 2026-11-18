"""Index rendering for DeepSpace host web server.

Handles index injection collection, rendering, and tap transforms.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexInjection:
    """An injection row for index.html rendering."""

    # Row type: "script-preload", "meta", "link", "script", "style", etc.
    type: str
    # Attributes for the HTML element
    attrs: dict[str, str] = field(default_factory=dict)
    # Optional inline content for script/style tags
    content: str | None = None
    # Optional Peer identifier this injection is for
    peer: str | None = None


class IndexRenderer:
    """Handles index.html injection collection and rendering."""

    def __init__(self) -> None:
        self._injections: list[IndexInjection] = []
        self._taps: list[Callable[[str], str]] = []

    def collect_injections(self, peer: str | None = None) -> list[IndexInjection]:
        """Collect injection rows for a peer. Emits index-inject event."""
        # In a real implementation, this would emit an event to gather injections
        # from subscribers. For now, return the collected injections.
        return [inj for inj in self._injections if inj.peer is None or inj.peer == peer]

    def add_injection(self, injection: IndexInjection) -> Callable[[], None]:
        """Add an injection row. Returns a disposer."""
        self._injections.append(injection)
        return lambda: self._injections.remove(injection)

    def render_index(self, html: str, peer: str | None = None) -> str:
        """Render injections into the index.html body."""
        injections = self.collect_injections(peer)
        if not injections:
            return html

        # Build injection HTML
        injection_html_parts = []
        for inj in injections:
            attrs = " ".join(f'{k}="{v}"' for k, v in inj.attrs.items())
            if inj.content is not None:
                injection_html_parts.append(f"<{inj.type} {attrs}>{inj.content}</{inj.type}>")
            else:
                injection_html_parts.append(f"<{inj.type} {attrs}>")

        injection_html = "\n".join(injection_html_parts)

        # Inject after <head> tag
        head_end = html.find("</head>")
        if head_end != -1:
            return html[:head_end] + injection_html + "\n" + html[head_end:]
        # Fallback: inject at start of body
        body_start = html.find("<body")
        if body_start != -1:
            body_end = html.find(">", body_start)
            if body_end != -1:
                return html[: body_end + 1] + injection_html + "\n" + html[body_end + 1 :]
        # Last resort: prepend
        return injection_html + "\n" + html

    def add_tap(self, transform: Callable[[str], str]) -> Callable[[], None]:
        """Add a raw HTML transform tap. Returns a disposer."""
        self._taps.append(transform)
        return lambda: self._taps.remove(transform)

    def apply_taps(self, html: str) -> str:
        """Apply all registered taps in registration order."""
        for tap in self._taps:
            html = tap(html)
        return html


class ScriptPreloadRow:
    """A script-preload injection row."""

    @staticmethod
    def create(
        href: str, as_type: str = "script", crossorigin: str = "anonymous"
    ) -> IndexInjection:
        return IndexInjection(
            type="link",
            attrs={
                "rel": "preload",
                "href": href,
                "as": as_type,
                "crossorigin": crossorigin,
            },
        )


__all__ = [
    "IndexInjection",
    "IndexRenderer",
    "ScriptPreloadRow",
]
