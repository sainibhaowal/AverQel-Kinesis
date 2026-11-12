"""Small success-caching lazy importer for optional runtime dependencies."""

from __future__ import annotations

import importlib
import threading
from types import ModuleType

_cache: dict[tuple[str, str | None], ModuleType] = {}
_lock = threading.RLock()


def lazy_import(module_name: str, *, package: str | None = None) -> ModuleType:
    """Import a module on demand, cache success, and leave failures retryable.

    Relative names follow Python's package-relative import rules. The cache is
    populated only after ``import_module`` succeeds, so an optional dependency
    installed or repaired later in the process can be retried.
    """
    if not isinstance(module_name, str) or not module_name.strip():
        raise ValueError("module_name must be a non-empty string")
    key = (module_name, package)
    module = _cache.get(key)
    if module is not None:
        return module
    with _lock:
        module = _cache.get(key)
        if module is None:
            module = importlib.import_module(module_name, package=package)
            _cache[key] = module
        return module


def clear_lazy_import_cache() -> None:
    """Clear this helper's references, primarily for isolated tests."""
    with _lock:
        _cache.clear()
