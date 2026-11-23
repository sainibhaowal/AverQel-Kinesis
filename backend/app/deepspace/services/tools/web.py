"""The unified web tools for DeepSpace.

This module provides web search, URL reading, and image reading capabilities.
All tools reuse the existing safe reader and browser fallback infrastructure.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

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

# ============================================================
# Argument Models
# ============================================================


class WebSearchArguments(ToolArguments):
    """Search the public web through the configured provider."""

    query: str = Field(min_length=2, max_length=512)
    max_results: int | None = Field(default=None, ge=1, le=1000)
    domains: list[str] | None = Field(default=None, max_length=20)
    time_range: Literal["day", "week", "month", "year"] | None = None
    provider: Literal["searxng", "tavily"] | None = None


class UrlReadArguments(ToolArguments):
    """Read a public HTTPS web URL for source-backed research."""

    url: str = Field(min_length=8, max_length=2048)
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


class GetToolResultArguments(ToolArguments):
    """Retrieve a bounded continuation of a previously referenced tool result."""

    result_ref: str = Field(min_length=8, max_length=128)


class ImageReadArguments(ToolArguments):
    """Inspect a public raster or SVG image URL for dimensions and visual context."""

    url: str = Field(min_length=8, max_length=2048)
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


class WebFetchArguments(ToolArguments):
    """Fetch one public web URL as text or as an image (unified interface)."""

    url: str = Field(min_length=8, max_length=2048)
    mode: Literal["auto", "text", "image"] = "auto"
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


# ============================================================
# Helper Functions
# ============================================================


def _allowed_domains(context: ToolContext, requested: list[str] | None) -> list[str]:
    """Resolve the domain allowlist for one fetch."""
    if isinstance(requested, list) and requested:
        return [str(item) for item in requested]
    configured = getattr(context.settings, "deepspace_url_allowed_domains", [])
    return list(configured) if isinstance(configured, list) else []


def _blocked_result(
    url: str,
    detail: str,
    *,
    status: str,
    guidance: str | None = None,
    source: str = "web_fetch",
) -> dict[str, Any]:
    """Build the result for a source that could not be read."""
    return {
        "url": url,
        "title": None,
        "content_type": None,
        "text": "",
        "retrieval_status": status,
        "error_category": "source_unavailable",
        "message": ("The public source was not read. " f"{detail} {guidance or ''}".strip()),
        "citations": [
            {
                "title": "Source unavailable",
                "url": url,
                "snippet": "The public source could not be read.",
                "source": source,
                "retrieval_status": status,
            }
        ],
    }


# ============================================================
# Handler: web_search
# ============================================================


async def _web_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Search the web and return results with URLs for follow-up reading."""
    from app.deepspace.services.web_search import search_web

    try:
        options = context.web_search_options
        provider_name = str(arguments.get("provider") or "").casefold()
        selected = options.get(provider_name) if provider_name else None
        if selected is None and options:
            selected = next(iter(options.values()))
        if selected is None:
            results = await search_web(
                query=arguments["query"],
                max_results=arguments.get("max_results"),
                domains=arguments.get("domains"),
                time_range=arguments.get("time_range"),
                provider=arguments.get("provider"),
                settings=context.settings,
            )
        else:
            provider, candidate = selected
            from app.providers.services.types import WebSearchRequest

            max_results = max(1, min(int(arguments.get("max_results") or 5), 10))
            candidate_metadata = getattr(candidate, "metadata", {})
            metadata = dict(candidate_metadata) if isinstance(candidate_metadata, dict) else {}
            requested_domains = arguments.get("domains")
            if isinstance(requested_domains, list) and requested_domains:
                configured_domains = metadata.get("allowed_domains")
                requested = [str(item).strip().lower().lstrip(".") for item in requested_domains]
                if isinstance(configured_domains, list) and configured_domains:
                    configured = [
                        str(item).strip().lower().lstrip(".")
                        for item in configured_domains
                        if str(item).strip()
                    ]
                    requested = [
                        domain
                        for domain in requested
                        if domain
                        and any(
                            domain == allowed or domain.endswith(f".{allowed}")
                            for allowed in configured
                        )
                    ]
                metadata["allowed_domains"] = requested[:20]
            metadata.update(
                {
                    "time_range": arguments.get("time_range"),
                    "tenant_id": str(context.auth.tenant_id),
                    "user_id": str(context.auth.user_id),
                }
            )
            request = WebSearchRequest(
                query=str(arguments["query"])[:512],
                max_results=max_results,
                timeout_seconds=15,
                include_answer=False,
                include_raw_content=False,
                provider_name=str(getattr(candidate, "provider_type", "")),
                metadata=metadata,
            )
            response = await asyncio.to_thread(provider.search, request)
            results = [
                {
                    "title": str(item.title or "Source"),
                    "url": str(item.url),
                    "snippet": str(item.content or "")[:800],
                    "source": str(getattr(item, "source", "web_search")),
                }
                for item in response.results[:max_results]
            ]
        citations = [
            {
                "id": index,
                "title": str(item.get("title") or "Source"),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or "")[:800],
                "source": str(item.get("source") or "web_search"),
                "retrieval_status": "search_snippet_only",
            }
            for index, item in enumerate(results, start=1)
            if isinstance(item, dict)
        ]
        return {"results": results, "citations": citations}
    except Exception as e:
        return {"status": "error", "error_code": "WEB_SEARCH_FAILED", "message": str(e)}


# ============================================================
# Handler: url_read
# ============================================================


async def _url_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a public HTTPS URL and return extracted content."""
    url = arguments["url"]
    allowed = _allowed_domains(context, arguments.get("allowed_domains"))

    try:
        result = read_url_with_browser_fallback(
            url=url,
            settings=context.settings,
            allowed_domains=allowed,
            timeout_seconds=getattr(context.settings, "deepspace_url_read_timeout_seconds", 15),
            max_bytes=int(getattr(context.settings, "deepspace_url_read_max_bytes", 2_000_000)),
        )
        payload = asdict(result)
        max_chars = max(1, int(getattr(context.settings, "deepspace_url_read_max_chars", 48_000)))
        if len(payload["text"]) > max_chars:
            payload["text"] = payload["text"][:max_chars]
            payload["truncated"] = True
        payload["retrieval_status"] = "read_full"
        payload["citations"] = [
            {
                "title": str(payload.get("title") or url),
                "url": str(payload.get("final_url") or url),
                "snippet": str(payload["text"])[:800],
                "source": "web_fetch",
                "retrieval_status": "read_full",
            }
        ]
        return payload
    except ProviderRequestError as e:
        status = "blocked" if e.status_code in {401, 403, 429} else "unavailable"
        return _blocked_result(url, str(e), status=status)
    except Exception as e:
        return _blocked_result(url, str(e), status="fetch_failed")


# ============================================================
# Handler: get_tool_result
# ============================================================


async def _get_tool_result(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Retrieve a previously stored tool result by reference."""
    if context.tool_result_store is None:
        return {
            "status": "error",
            "error_code": "RESULT_STORE_UNAVAILABLE",
            "message": "Tool result store not available",
        }

    result = context.tool_result_store.get(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        ref=arguments["result_ref"],
    )
    if result is None:
        return {"status": "not_found", "result_ref": arguments["result_ref"]}
    if not isinstance(result, dict):
        return {
            "status": "error",
            "error_code": "INVALID_RESULT",
            "message": "The stored tool result has an invalid format.",
        }
    return result


# ============================================================
# Handler: image_read
# ============================================================


async def _image_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read and analyze an image from a public URL."""
    url = arguments["url"]
    allowed = _allowed_domains(context, arguments.get("allowed_domains"))

    try:
        result = read_image(
            url=url,
            allowed_domains=allowed,
            timeout_seconds=getattr(context.settings, "deepspace_url_read_timeout_seconds", 15),
            max_bytes=int(getattr(context.settings, "deepspace_url_read_max_bytes", 2_000_000)),
        )
        result["retrieval_status"] = "read_full"
        result["citations"] = [
            {
                "title": "Inspected image",
                "url": str(result.get("url") or url),
                "snippet": "Image dimensions and format were inspected.",
                "source": "image_read",
                "retrieval_status": "read_full",
            }
        ]
        return result
    except ProviderRequestError as e:
        if e.status_code == 415:
            return _blocked_result(
                url,
                str(e),
                status="not_an_image",
                guidance="Use url_read for non-image content.",
                source="image_read",
            )
        return _blocked_result(url, str(e), status="unavailable", source="image_read")
    except Exception as e:
        return _blocked_result(url, str(e), status="unavailable", source="image_read")


# ============================================================
# Handler: web_fetch (unified)
# ============================================================


async def _web_fetch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Unified web fetch: auto-detect content type or read as specified mode."""
    mode = arguments.get("mode", "auto")
    url = arguments["url"]
    allowed = _allowed_domains(context, arguments.get("allowed_domains"))

    if mode == "image":
        return await _image_read({"url": url, "allowed_domains": allowed}, context)
    else:
        # For "auto" or "text", use url_read
        result = await _url_read({"url": url, "allowed_domains": allowed}, context)
        if (
            mode == "auto"
            and result.get("retrieval_status") == "success"
            and result.get("content_type", "").startswith("image/")
        ):
            # Auto-detected image, re-read as image
            return await _image_read({"url": url, "allowed_domains": allowed}, context)
        return result


# ============================================================
# Tool Specs
# ============================================================

WEB_SEARCH_TOOL = define_native_tool(
    name="web_search",
    description=(
        "Search the public web through the configured self-hosted search provider. "
        "Use this for current, time-sensitive, unfamiliar, or source-backed information. "
        "After finding a relevant result, call url_read on its URL before summarizing."
    ),
    properties={
        "query": string_property("The search query.", min_length=2, max_length=512),
        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
        "domains": array_property("Restrict results to these domains.", max_items=20),
        "time_range": string_property(
            "Limit results to a time window.",
            enum=["day", "week", "month", "year"],
        ),
        "provider": string_property(
            "Choose one configured search provider offered for this turn.",
            enum=["searxng", "tavily"],
        ),
    },
    required=["query"],
    arguments_model=WebSearchArguments,
    mode="read",
    handler=_web_search,
)

URL_READ_TOOL = define_native_tool(
    name="url_read",
    description=(
        "Read a public HTTPS web URL for source-backed research. Prefer this on URLs "
        "returned by web_search so the answer is based on fetched page text, not only a snippet. "
        "Returns the final source URL, title, extracted text, and links."
    ),
    properties={
        "url": string_property("The public HTTPS URL to read.", min_length=8, max_length=2048),
        "allowed_domains": array_property("Domains the read is restricted to.", max_items=20),
    },
    required=["url"],
    arguments_model=UrlReadArguments,
    mode="read",
    handler=_url_read,
)

GET_TOOL_RESULT_TOOL = define_native_tool(
    name="get_tool_result",
    description="Retrieve a bounded continuation of a previously referenced native tool result. Use only with a result_ref returned in this conversation.",
    properties={
        "result_ref": string_property(
            "The reference returned by a previous tool call.", min_length=8, max_length=128
        ),
    },
    required=["result_ref"],
    arguments_model=GetToolResultArguments,
    mode="read",
    handler=_get_tool_result,
)

IMAGE_READ_TOOL = define_native_tool(
    name="image_read",
    description=(
        "Inspect a public raster or SVG image URL for dimensions and visual context. "
        "Call this only for a URL verified as an image; use url_read for HTML/CSV/JSON/text documents. "
        "The image is passed to compatible vision models; this never accesses local files."
    ),
    properties={
        "url": string_property("The public HTTPS URL of the image.", min_length=8, max_length=2048),
        "allowed_domains": array_property("Domains the fetch is restricted to.", max_items=20),
    },
    required=["url"],
    arguments_model=ImageReadArguments,
    mode="read",
    handler=_image_read,
)

WEB_FETCH_TOOL = define_native_tool(
    name="web_fetch",
    description=(
        "Fetch one public web URL as text or as an image. "
        "Use mode='auto' to let the tool detect the content type, "
        "mode='text' for HTML/text documents, or mode='image' for images."
    ),
    properties={
        "url": string_property("The public HTTPS URL to fetch.", min_length=8, max_length=2048),
        "mode": string_property(
            "How to read the URL. 'auto' reads text and falls back to an image.",
            enum=["auto", "text", "image"],
        ),
        "allowed_domains": array_property("Domains the fetch is restricted to.", max_items=20),
    },
    required=["url"],
    arguments_model=WebFetchArguments,
    mode="read",
    handler=_web_fetch,
)

# All web tools exported
WEB_TOOLS: list[NativeToolSpec] = [
    WEB_SEARCH_TOOL,
    URL_READ_TOOL,
    GET_TOOL_RESULT_TOOL,
    IMAGE_READ_TOOL,
    WEB_FETCH_TOOL,
]

__all__ = [
    "WEB_TOOLS",
    "WEB_SEARCH_TOOL",
    "URL_READ_TOOL",
    "GET_TOOL_RESULT_TOOL",
    "IMAGE_READ_TOOL",
    "WEB_FETCH_TOOL",
    "WebSearchArguments",
    "UrlReadArguments",
    "GetToolResultArguments",
    "ImageReadArguments",
    "WebFetchArguments",
    "_web_search",
    "_url_read",
    "_get_tool_result",
    "_image_read",
    "_web_fetch",
]
