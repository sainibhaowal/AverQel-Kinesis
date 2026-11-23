"""Tests for the universal reasoning capabilities cache."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from app.providers.services.reasoning_cache import (
    ReasoningCache,
    ReasoningCacheEntry,
    get_reasoning_cache,
)


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cache(temp_cache_dir):
    """Create a reasoning cache instance with temp directory."""
    return ReasoningCache(temp_cache_dir)


def test_cache_set_and_get(cache):
    """Test basic cache set and get operations."""
    entry = ReasoningCacheEntry(
        provider_type="lmstudio",
        model_name="qwen2.5-7b",
        base_url="http://localhost:1234/v1",
        capabilities={
            "supports_reasoning": True,
            "supported_reasoning_efforts": ["low", "medium", "high"],
            "supports_thinking_toggle": True,
        },
        source="discovery",
    )

    cache.set(entry)
    retrieved = cache.get("lmstudio", "qwen2.5-7b", "http://localhost:1234/v1")

    assert retrieved is not None
    assert retrieved.provider_type == "lmstudio"
    assert retrieved.model_name == "qwen2.5-7b"
    assert retrieved.capabilities["supports_reasoning"] is True
    assert retrieved.capabilities["supported_reasoning_efforts"] == ["low", "medium", "high"]
    assert retrieved.source == "discovery"


def test_cache_key_generation(cache):
    """Test cache key generation handles special characters."""
    key1 = cache._make_key("lmstudio", "model/name", "http://localhost:1234/v1")
    key2 = cache._make_key("lmstudio", "model/name", "http://localhost:1234/v1")
    key3 = cache._make_key("lmstudio", "model/name", "http://different:5678/v1")

    assert key1 == key2
    assert key1 != key3
    # Key should NOT include .json extension (added when creating file)
    assert not key1.endswith(".json")
    # But file path should include .json
    cache_file = cache.cache_dir / f"{key1}.json"
    assert cache_file.name.endswith(".json")


def test_cache_expiration(cache):
    """Test cache expiration logic."""
    entry = ReasoningCacheEntry(
        provider_type="ollama",
        model_name="llama3",
        base_url="http://localhost:11434",
        capabilities={"supports_reasoning": False},
        source="static",
    )

    # Manually set fetched_at to old time (8 days ago > 7 day TTL)
    entry.fetched_at = time.time() - (8 * 24 * 60 * 60 + 100)

    cache.set(entry)
    retrieved = cache.get("ollama", "llama3", "http://localhost:11434")

    # Should be None because expired
    assert retrieved is None


def test_cache_persistence(temp_cache_dir):
    """Test cache persists across instances."""
    cache1 = ReasoningCache(temp_cache_dir)

    entry = ReasoningCacheEntry(
        provider_type="openai",
        model_name="gpt-4o",
        base_url="https://api.openai.com/v1",
        capabilities={
            "supports_reasoning": True,
            "supported_reasoning_efforts": ["low", "medium", "high"],
        },
        source="discovery",
    )

    cache1.set(entry)

    # Create new cache instance with same directory
    cache2 = ReasoningCache(temp_cache_dir)
    retrieved = cache2.get("openai", "gpt-4o", "https://api.openai.com/v1")

    assert retrieved is not None
    assert retrieved.provider_type == "openai"
    assert retrieved.model_name == "gpt-4o"
    assert retrieved.capabilities["supports_reasoning"] is True


def test_get_all_for_provider(cache):
    """Test getting all entries for a provider."""
    cache.set(
        ReasoningCacheEntry(
            provider_type="lmstudio",
            model_name="model1",
            base_url="http://localhost:1234",
            capabilities={},
            source="discovery",
        )
    )
    cache.set(
        ReasoningCacheEntry(
            provider_type="lmstudio",
            model_name="model2",
            base_url="http://localhost:1234",
            capabilities={},
            source="static",
        )
    )
    cache.set(
        ReasoningCacheEntry(
            provider_type="ollama",
            model_name="model3",
            base_url="http://localhost:11434",
            capabilities={},
            source="discovery",
        )
    )

    lmstudio_entries = cache.get_all_for_provider("lmstudio")
    ollama_entries = cache.get_all_for_provider("ollama")

    assert len(lmstudio_entries) == 2
    assert len(ollama_entries) == 1
    assert all(e.provider_type == "lmstudio" for e in lmstudio_entries)
    assert all(e.provider_type == "ollama" for e in ollama_entries)


def test_clear_provider(cache):
    """Test clearing cache for a specific provider."""
    cache.set(
        ReasoningCacheEntry(
            provider_type="lmstudio",
            model_name="model1",
            base_url="http://localhost:1234",
            capabilities={},
            source="discovery",
        )
    )
    cache.set(
        ReasoningCacheEntry(
            provider_type="ollama",
            model_name="model2",
            base_url="http://localhost:11434",
            capabilities={},
            source="discovery",
        )
    )

    removed = cache.clear_provider("lmstudio")
    assert removed == 1

    lmstudio_entries = cache.get_all_for_provider("lmstudio")
    ollama_entries = cache.get_all_for_provider("ollama")

    assert len(lmstudio_entries) == 0
    assert len(ollama_entries) == 1


def test_clear_expired(cache):
    """Test clearing expired entries."""
    # Add fresh entry
    cache.set(
        ReasoningCacheEntry(
            provider_type="openai",
            model_name="gpt-4o",
            base_url="https://api.openai.com/v1",
            capabilities={},
            source="discovery",
        )
    )

    # Add expired entry (8 days ago > 7 day TTL)
    expired_entry = ReasoningCacheEntry(
        provider_type="openai",
        model_name="gpt-3.5-turbo",
        base_url="https://api.openai.com/v1",
        capabilities={},
        source="static",
    )
    expired_entry.fetched_at = time.time() - (8 * 24 * 60 * 60 + 100)
    cache.set(expired_entry)

    removed = cache.clear_expired()
    assert removed == 1

    # Fresh entry should still exist
    fresh = cache.get("openai", "gpt-4o", "https://api.openai.com/v1")
    assert fresh is not None


def test_global_cache_singleton():
    """Test global cache singleton."""
    cache1 = get_reasoning_cache()
    cache2 = get_reasoning_cache()
    assert cache1 is cache2


def test_reasoning_cache_entry_serialization():
    """Test cache entry serialization round-trip."""
    original = ReasoningCacheEntry(
        provider_type="custom",
        model_name="my-model",
        base_url="https://custom.example.com/v1",
        capabilities={
            "supports_reasoning": True,
            "supported_reasoning_efforts": ["low", "high"],
            "reasoning_effort_values": {"low": "low", "high": "high"},
        },
        source="user",
        fetched_at=1234567890.0,
    )

    # Serialize and deserialize
    data = original.to_dict()
    restored = ReasoningCacheEntry.from_dict(data)

    assert restored.provider_type == original.provider_type
    assert restored.model_name == original.model_name
    assert restored.base_url == original.base_url
    assert restored.capabilities == original.capabilities
    assert restored.source == original.source
    assert restored.fetched_at == original.fetched_at


def test_cache_with_special_characters_in_names(cache):
    """Test cache handles special characters in model/provider names."""
    # Model names with slashes, colons, etc.
    cache.set(
        ReasoningCacheEntry(
            provider_type="openai-compatible",
            model_name="organization/model-name:tag",
            base_url="https://api.example.com/v1",
            capabilities={"supports_reasoning": True},
            source="discovery",
        )
    )

    retrieved = cache.get(
        "openai-compatible", "organization/model-name:tag", "https://api.example.com/v1"
    )
    assert retrieved is not None
    assert retrieved.model_name == "organization/model-name:tag"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
