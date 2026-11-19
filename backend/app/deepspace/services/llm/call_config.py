"""Utility functions for DeepSpace LLM service."""

from __future__ import annotations

import copy
from typing import Any


def deep_freeze(obj: Any) -> Any:
    """Recursively freeze an object for immutability."""
    if isinstance(obj, dict):
        return tuple(sorted((k, deep_freeze(v)) for k, v in obj.items()))
    elif isinstance(obj, list):
        return tuple(deep_freeze(v) for v in obj)
    elif isinstance(obj, set):
        return tuple(sorted(deep_freeze(v) for v in obj))
    elif hasattr(obj, "__dataclass_fields__"):
        # Handle dataclass objects
        return tuple(sorted((f, deep_freeze(getattr(obj, f))) for f in obj.__dataclass_fields__))
    return obj


def structured_clone(obj: Any) -> Any:
    """Create a deep clone of an object."""
    return copy.deepcopy(obj)


def call_config_equals(a: Any, b: Any) -> bool:
    """Compare two call configs for equality."""
    return bool(deep_freeze(a) == deep_freeze(b))


__all__ = ["deep_freeze", "structured_clone", "call_config_equals"]
