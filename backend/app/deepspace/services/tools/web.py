"""The unified web fetch tool for DeepSpace.

``url_read`` and ``image_read`` remain available, and ``web_fetch`` adds one
tool that covers both shapes.  It reuses the existing safe reader and the
existing browser fallback, so the URL validation, the domain allowlist, the
response size ceiling, and the rendered-page fallback are the same code paths
the narrower tools use — there is no second fetch implementation to keep in
step.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from app.deepspace.services.browser_reader import read_url_with_browser_fallback
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    string_property,
)
from app.deepspace.services.url_reader import read_image
from app.providers.services.base import ProviderRequestError


class WebFetchArguments(ToolArguments):
    """Fetch one public web URL as text or as an image."""

    url: str = Field(min_length=8, max_length=2048)
    mode: Literal["auto", "text", "image"] = "auto"
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


def _allowed_domains(context: ToolContext, requested: list[str] | None) -> list[str]:
    """Resolve the domain allowlist for one fetch.

    Args:
        context: The calling request's tool context.
        requested: The domains the caller asked for, when it named any.

    Returns:
        The effective allowlist, empty when the deployment allows any public
        host.
    """
    if isinstance(requested, list) and requested:
        return [str(item) for item in requested]
    configured = getattr(context.settings, "deepspace_url_allowed_domains", [])
    return list(configured) if isinstance(configured, list) else []


def _blocked_result(
    url: str, detail: str, *, status: str, guidance: str | None = None
) -> dict[str, Any]:
    """Build the result for a source that could not be read.

    Args:
        url: The requested URL.
        detail: The reason the source was not read.
        status: The retrieval status label.
        guidance: What the model should use instead, when there is a better
            tool for the source.

    Returns:
        A result that names the failure and never claims page access.
    """
    return {
        "url": url,
        "title": None,
        "content_type": None,
        "text": "",
        "retrieval_status": status,
        "error_category": "source_unavailable",
        "message": (
            "The public source was not read. "
            f"{detail} {guidance or 'Use another source; do not claim this page was opened.'}"
        ),
        "citations": [
            {
                "title": "Unavailable source",
                "url": url,
                "snippet": "The source was not read.",
                "source": "web_fetch",
                "retrieval_status": status,
            }
        ],
    }


async def _fetch_text(url: str, context: ToolContext, domains: list[str]) -> dict[str, Any]:
    """Read one URL as text, mirroring the ``url_read`` result shape."""
    try:
        result = await asyncio.to_thread(
            read_url_with_browser_fallback,
            url,
            settings=context.settings,
            timeout_seconds=min(
                30, int(getattr(context.settings, "deepspace_url_read_timeout_seconds", 15))
            ),
            max_bytes=int(getattr(context.settings, "deepspace_url_read_max_bytes", 2_000_000)),
            allowed_domains=domains,
        )
    except ProviderRequestError as exc:
        status = "blocked" if exc.status_code in {400, 401, 403} else "unavailable"
        return _blocked_result(url, str(exc), status=status)
    max_model_chars = int(getattr(context.settings, "deepspace_url_read_max_chars", 48_000))
    model_text = result.text[:max_model_chars]
    was_truncated = len(result.text) > max_model_chars
    if was_truncated:
        model_text = model_text.rstrip() + "\n[Source text truncated to protect model context.]"
    return {
        "url": result.url,
        "title": result.title,
        "content_type": result.content_type,
        "kind": "text",
        "text": model_text,
        "truncated": result.truncated or was_truncated,
        "links": result.links,
        "retrieval_method": result.retrieval_method,
        "citations": [
            {
                "title": result.title or result.url,
                "url": result.url,
                "snippet": model_text[:800],
                "source": "web_fetch",
                "retrieval_method": result.retrieval_method,
                "retrieval_status": "read_full",
            }
        ],
    }


async def _fetch_image(url: str, context: ToolContext, domains: list[str]) -> dict[str, Any]:
    """Read one URL as an image, mirroring the ``image_read`` result shape."""
    # A document URL is not an image.  Refusing it here keeps a broad search
    # result from downloading a data file into the vision reader.
    path = urlparse(url).path.casefold()
    if path.endswith((".csv", ".tsv", ".json", ".txt", ".md", ".html", ".htm")):
        return _blocked_result(
            url,
            "This URL looks like a document or data file, not an image.",
            status="not_an_image",
            guidance="Use url_read or sandbox_execute instead.",
        )
    try:
        image = await asyncio.to_thread(
            read_image,
            url,
            timeout_seconds=min(
                30, int(getattr(context.settings, "deepspace_url_read_timeout_seconds", 15))
            ),
            max_bytes=int(getattr(context.settings, "deepspace_url_read_max_bytes", 2_000_000)),
            allowed_domains=domains,
        )
    except ProviderRequestError as exc:
        status = "not_an_image" if exc.status_code == 415 else "unavailable"
        return _blocked_result(url, str(exc), status=status)
    return {
        "url": image.get("url"),
        "title": None,
        "content_type": image.get("content_type"),
        "kind": "image",
        "format": image.get("format"),
        "width": image.get("width"),
        "height": image.get("height"),
        "bytes": image.get("bytes"),
        "text": "",
        "retrieval_status": "read_full",
        "citations": [
            {
                "title": "Image",
                "url": str(image.get("url") or url),
                "snippet": "The image was fetched and inspected.",
                "source": "web_fetch",
                "retrieval_status": "read_full",
            }
        ],
        # Consumed internally and removed before the tool output is persisted
        # or shown. A vision-capable provider receives it.
        "_image_base64": image.get("_image_base64"),
    }


async def _web_fetch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Fetch one public URL as text or as an image."""
    url = str(arguments["url"]).strip()
    mode = str(arguments.get("mode") or "auto")
    domains = _allowed_domains(context, arguments.get("allowed_domains"))
    if mode == "image":
        return await _fetch_image(url, context, domains)
    if mode == "text":
        return await _fetch_text(url, context, domains)
    text_result = await _fetch_text(url, context, domains)
    if text_result.get("retrieval_status") == "read_full":
        return text_result
    image_result = await _fetch_image(url, context, domains)
    if image_result.get("retrieval_status") == "read_full":
        return image_result
    return text_result


WEB_FETCH_DESCRIPTION = (
    "Fetch a public HTTPS URL. Returns the page text for documents and the "
    "image metadata for pictures, so one tool covers both. Use web_search "
    "first to find the URL."
)

WEB_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="web_fetch",
        description=WEB_FETCH_DESCRIPTION,
        properties={
            "url": string_property("The public HTTPS URL to fetch.", min_length=8, max_length=2048),
            "mode": string_property(
                "How to read the URL. 'auto' reads text and falls back to an image.",
                enum=["auto", "text", "image"],
            ),
            "allowed_domains": array_property(
                "Domains the fetch is restricted to, when the deployment requires it.",
                max_items=20,
            ),
        },
        required=["url"],
        arguments_model=WebFetchArguments,
        mode="read",
        handler=_web_fetch,
    ),
]

__all__ = ["WEB_TOOLS", "WebFetchArguments"]
