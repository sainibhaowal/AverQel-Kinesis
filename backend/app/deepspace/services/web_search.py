"""Web search service for DeepSpace.

Provides a unified search interface that delegates to the configured
search provider (SearXNG, Tavily, etc.).
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.config import Settings
from app.providers.services.searxng_provider import SearXNGProvider
from app.providers.services.tavily_provider import TavilyProvider
from app.providers.services.types import WebSearchRequest


async def search_web(
    query: str,
    *,
    max_results: int | None = None,
    domains: list[str] | None = None,
    time_range: Literal["day", "week", "month", "year"] | None = None,
    provider: Literal["searxng", "tavily"] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """
    Search the web using the configured provider.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 6).
        domains: Optional list of domains to restrict results to.
        time_range: Optional time range filter.
        provider: Explicit provider to use ("searxng" or "tavily").
        settings: Application settings for provider configuration.

    Returns:
        List of search result dictionaries.
    """
    if not query or not query.strip():
        return []

    max_results = max_results or 6

    # Determine which provider to use
    provider_type = provider or "searxng"

    if provider_type == "tavily":
        tavily = TavilyProvider()
        request = WebSearchRequest(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=False,
            include_raw_content=False,
            timeout_seconds=30,
        )
        response = tavily.search(request)
    else:
        # Default to SearXNG
        searxng = SearXNGProvider()
        request = WebSearchRequest(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=False,
            include_raw_content=False,
            timeout_seconds=30,
        )
        response = searxng.search(request)

    results = []
    for item in response.results:
        result_dict: dict[str, Any] = {
            "url": str(item.url),
            "title": item.title,
            "snippet": item.content,
            "score": getattr(item, "score", None),
        }
        # Filter by domains if specified
        if domains:
            from urllib.parse import urlparse

            parsed = urlparse(result_dict["url"])
            if not any(parsed.netloc.endswith(d) for d in domains):
                continue
        results.append(result_dict)

    return results[:max_results]


async def search_web_multi_provider(
    query: str,
    *,
    max_results: int | None = None,
    domains: list[str] | None = None,
    time_range: Literal["day", "week", "month", "year"] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """
    Search using multiple providers and merge results.

    Falls back gracefully if one provider fails.
    """
    all_results = []
    providers_tried = []

    for provider_name in ("searxng", "tavily"):
        try:
            results = await search_web(
                query,
                max_results=max_results,
                domains=domains,
                provider=provider_name,
            )
            if results:
                for r in results:
                    r["provider"] = provider_name
                all_results.extend(results)
            providers_tried.append(provider_name)
        except Exception:
            # Continue to next provider on failure
            continue

    # Deduplicate by URL
    seen = set()
    unique_results = []
    for r in all_results:
        url = r.get("url")
        if url and url not in seen:
            seen.add(url)
            unique_results.append(r)

    return unique_results[: (max_results or 6)]
