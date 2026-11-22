"""
Universal Reasoning Capabilities Cache

Persists model reasoning metadata in ~/.averqel-harness/reasoning-cache/
so that thinking buttons show real capabilities immediately on startup,
with background auto-discovery from providers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.core.averqel_home import averqel_storage_path

logger = logging.getLogger(__name__)

# Cache directory: ~/.averqel-harness/reasoning-cache/
CACHE_DIR = averqel_storage_path() / "reasoning-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# TTL for cache entries (7 days)
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Background refresh interval (1 hour)
REFRESH_INTERVAL_SECONDS = 60 * 60


@dataclass(slots=True)
class ReasoningCacheEntry:
    """Cached reasoning capabilities for a model from a specific provider."""

    provider_type: str
    model_name: str
    base_url: str | None
    capabilities: dict[str, Any]
    fetched_at: float = field(default_factory=time.time)
    source: Literal["discovery", "static", "user"] = "discovery"

    def is_expired(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
        return (time.time() - self.fetched_at) > ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReasoningCacheEntry:
        return cls(**data)


class ReasoningCache:
    """
    File-based cache for reasoning capabilities.

    Key format: {provider_type}:{model_name}:{base_url_hash}
    Stored in ~/.averqel-harness/reasoning-cache/
    """

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_cache: dict[str, ReasoningCacheEntry] = {}
        self._loaded = False

    def _make_key(self, provider_type: str, model_name: str, base_url: str | None = None) -> str:
        """Create a filesystem-safe cache key (without .json extension)."""
        import hashlib

        url_part = base_url or "default"
        url_hash = hashlib.sha256(url_part.encode()).hexdigest()[:16]
        safe_provider = provider_type.replace("/", "_").replace(":", "_").replace(".", "_")
        safe_model = model_name.replace("/", "_").replace(":", "_").replace(".", "_")
        return f"{safe_provider}:{safe_model}:{url_hash}"

    def _parse_key(self, key: str) -> tuple[str, str, str] | None:
        """Parse a cache key back into components."""
        # key format: {safe_provider}:{safe_model}:{url_hash}
        parts = key.split(":", 2)
        if len(parts) != 3:
            return None
        return parts[0], parts[1], parts[2]

    def _load_all(self) -> None:
        """Load all cache entries into memory."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open(cache_file) as f:
                        data = json.load(f)
                    entry = ReasoningCacheEntry.from_dict(data)
                    self._memory_cache[cache_file.stem] = entry
                except Exception as e:
                    logger.warning("Failed to load reasoning cache %s: %s", cache_file, e)
            self._loaded = True

    def get(
        self, provider_type: str, model_name: str, base_url: str | None = None
    ) -> ReasoningCacheEntry | None:
        """Get cached reasoning capabilities."""
        self._load_all()
        key = self._make_key(provider_type, model_name, base_url)
        with self._lock:
            entry = self._memory_cache.get(key)
            if entry and not entry.is_expired():
                return entry
            elif entry:
                # Expired - remove from memory, keep file for fallback
                del self._memory_cache[key]
            return None

    def set(self, entry: ReasoningCacheEntry) -> None:
        """Save reasoning capabilities to cache."""
        key = self._make_key(entry.provider_type, entry.model_name, entry.base_url)
        cache_file = self.cache_dir / f"{key}.json"
        with self._lock:
            self._memory_cache[key] = entry
            try:
                with open(cache_file, "w") as f:
                    json.dump(entry.to_dict(), f, separators=(",", ":"))
            except Exception as e:
                logger.warning("Failed to save reasoning cache %s: %s", cache_file, e)

    def get_all_for_provider(self, provider_type: str) -> list[ReasoningCacheEntry]:
        """Get all cached entries for a provider."""
        self._load_all()
        safe_provider = provider_type.replace("/", "_").replace(":", "_").replace(".", "_")
        with self._lock:
            return [
                entry
                for key, entry in self._memory_cache.items()
                if key.startswith(f"{safe_provider}:") and not entry.is_expired()
            ]

    def clear_expired(self) -> int:
        """Remove expired cache entries from disk and memory."""
        removed = 0
        self._load_all()
        with self._lock:
            expired_keys = [key for key, entry in self._memory_cache.items() if entry.is_expired()]
            for key in expired_keys:
                del self._memory_cache[key]
                cache_file = self.cache_dir / f"{key}.json"
                try:
                    cache_file.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
        return removed

    def clear_provider(self, provider_type: str) -> int:
        """Clear all cache entries for a provider."""
        removed = 0
        self._load_all()
        with self._lock:
            keys_to_remove = [
                key
                for key, entry in self._memory_cache.items()
                if entry.provider_type == provider_type
            ]
            for key in keys_to_remove:
                del self._memory_cache[key]
                cache_file = self.cache_dir / f"{key}.json"
                try:
                    cache_file.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
        return removed


# Global cache instance
_reasoning_cache: ReasoningCache | None = None
_cache_lock = threading.Lock()


def get_reasoning_cache() -> ReasoningCache:
    """Get the global reasoning cache instance."""
    global _reasoning_cache
    if _reasoning_cache is None:
        with _cache_lock:
            if _reasoning_cache is None:
                _reasoning_cache = ReasoningCache()
    return _reasoning_cache


async def discover_reasoning_capabilities(
    provider_type: str,
    model_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any] | None:
    """
    Discover reasoning capabilities from a provider's API.

    Returns the raw capabilities dict or None if discovery fails.
    """
    import httpx

    from app.providers.services.reasoning_capabilities import (
        merge_reasoning_capabilities,
        reasoning_capabilities,
    )

    # First, get static profile as fallback
    static_profile = reasoning_capabilities(provider_type, model_name, base_url=base_url)

    # Try to fetch from provider API
    if not base_url:
        return static_profile

    cache = get_reasoning_cache()
    cached = cache.get(provider_type, model_name, base_url)
    if cached and not cached.is_expired():
        return cached.capabilities

    try:
        # Build provider-specific discovery endpoint
        httpx_module = httpx
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Try multiple common endpoints for model capabilities
        endpoints = _get_discovery_endpoints(provider_type, base_url, model_name)

        async with httpx_module.AsyncClient(timeout=timeout_seconds) as client:
            for endpoint in endpoints:
                try:
                    response = await client.get(endpoint, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        advertised = _extract_reasoning_from_response(
                            provider_type, data, model_name
                        )
                        if advertised:
                            merged = merge_reasoning_capabilities(
                                provider_type,
                                model_name,
                                base_url=base_url,
                                advertised=advertised,
                            )
                            # Cache the result
                            cache.set(
                                ReasoningCacheEntry(
                                    provider_type=provider_type,
                                    model_name=model_name,
                                    base_url=base_url,
                                    capabilities=merged,
                                    source="discovery",
                                )
                            )
                            return merged
                except Exception:
                    continue

    except Exception as e:
        logger.debug("Reasoning discovery failed for %s/%s: %s", provider_type, model_name, e)

    # Cache static profile as fallback
    cache.set(
        ReasoningCacheEntry(
            provider_type=provider_type,
            model_name=model_name,
            base_url=base_url,
            capabilities=static_profile,
            source="static",
        )
    )
    return static_profile


def _get_discovery_endpoints(provider_type: str, base_url: str, model_name: str) -> list[str]:
    """Get provider-specific model capability endpoints."""
    base = base_url.rstrip("/")
    provider = provider_type.lower()

    if provider == "lmstudio":
        return [
            f"{base}/models/{model_name}",
            f"{base}/api/v1/models/{model_name}",
            f"{base}/v1/models/{model_name}",
        ]
    elif provider == "ollama":
        return [
            f"{base}/api/show",
        ]
    elif provider == "openai":
        return [
            f"{base}/models/{model_name}",
        ]
    elif provider in {"groq", "groq-openai-compatible"}:
        return [
            f"{base}/models/{model_name}",
        ]
    elif provider in {
        "together",
        "fireworks",
        "perplexity",
        "mistral",
        "moonshot",
        "zai",
        "nvidia-nim",
    }:
        return [
            f"{base}/models/{model_name}",
        ]
    elif provider == "openrouter":
        return [
            f"{base}/models/{model_name}",
        ]
    elif provider == "custom":
        return [
            f"{base}/models/{model_name}",
        ]
    else:
        # Generic OpenAI-compatible
        return [
            f"{base}/models/{model_name}",
        ]


def _extract_reasoning_from_response(
    provider_type: str,
    response_data: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    """Extract reasoning capabilities from provider API response."""
    # Check common locations for reasoning metadata
    sources = [response_data]
    for source in list(sources):
        if isinstance(source, dict):
            for key in ("capabilities", "reasoning_capabilities", "reasoning", "model_info"):
                nested = source.get(key)
                if isinstance(nested, dict):
                    sources.append(nested)

    # Look for reasoning fields
    found: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "supports_reasoning",
            "reasoning_capabilities_known",
            "reasoning_visibility",
            "supports_thinking_summary_stream",
            "supported_reasoning_efforts",
            "supports_thinking_toggle",
            "request_controls_on",
            "request_controls_off",
            "reasoning_effort_values",
            "response_formats",
        ):
            if key not in found and key in source:
                found[key] = source[key]

    # Also check for LM Studio style options
    if "capabilities" in response_data:
        caps = response_data["capabilities"]
        if isinstance(caps, dict) and "reasoning" in caps:
            reasoning = caps["reasoning"]
            if isinstance(reasoning, dict) and "allowed_options" in reasoning:
                options = reasoning["allowed_options"]
                if isinstance(options, list):
                    # This is LM Studio format - will be handled by merge_reasoning_capabilities
                    found["_lmstudio_reasoning_options"] = options

    return found if found else None


async def ensure_model_reasoning_cached(
    provider_type: str,
    model_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    Ensure model reasoning capabilities are cached.
    Returns cached or freshly discovered capabilities.
    """
    cache = get_reasoning_cache()
    cached = cache.get(provider_type, model_name, base_url)
    if cached and not cached.is_expired():
        return cached.capabilities

    # Discover and cache
    capabilities = await discover_reasoning_capabilities(
        provider_type,
        model_name,
        base_url=base_url,
        api_key=api_key,
    )
    return capabilities or {}


async def refresh_all_provider_models(
    provider_type: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_names: list[str] | None = None,
) -> int:
    """
    Background refresh of all models for a provider.
    Returns number of models refreshed.
    """
    if not base_url or not model_names:
        return 0

    refreshed = 0
    for model_name in model_names:
        try:
            await discover_reasoning_capabilities(
                provider_type,
                model_name,
                base_url=base_url,
                api_key=api_key,
            )
            refreshed += 1
        except Exception as e:
            logger.debug("Failed to refresh reasoning for %s/%s: %s", provider_type, model_name, e)
    return refreshed


def clear_reasoning_cache(provider_type: str | None = None) -> int:
    """Clear reasoning cache (for a specific provider or all)."""
    cache = get_reasoning_cache()
    if provider_type:
        return cache.clear_provider(provider_type)
    else:
        # Clear all
        removed = 0
        for cache_file in cache.cache_dir.glob("*.json"):
            try:
                cache_file.unlink()
                removed += 1
            except Exception:
                pass
        cache._memory_cache.clear()
        return removed


def get_cached_reasoning_summary() -> dict[str, Any]:
    """Get summary of all cached reasoning capabilities."""
    cache = get_reasoning_cache()
    cache._load_all()
    with cache._lock:
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for entry in cache._memory_cache.values():
            if entry.is_expired():
                continue
            provider_entries = by_provider.setdefault(entry.provider_type, [])
            provider_entries.append(
                {
                    "model_name": entry.model_name,
                    "base_url": entry.base_url,
                    "supports_reasoning": entry.capabilities.get("supports_reasoning", False),
                    "supported_efforts": entry.capabilities.get("supported_reasoning_efforts", []),
                    "supports_thinking_toggle": entry.capabilities.get(
                        "supports_thinking_toggle", False
                    ),
                    "source": entry.source,
                    "fetched_at": entry.fetched_at,
                }
            )
        return by_provider
