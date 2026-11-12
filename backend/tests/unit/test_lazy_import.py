from __future__ import annotations

import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

import pytest

from app.core import lazy_import as lazy_import_module

pytestmark = pytest.mark.unit_no_db


def test_lazy_import_caches_success_and_relative_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lazy_import_module.clear_lazy_import_cache()
    calls: list[tuple[str, str | None]] = []
    module = ModuleType("example")

    def fake_import(name: str, package: str | None = None) -> ModuleType:
        calls.append((name, package))
        return module

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert lazy_import_module.lazy_import(".example", package="app.core") is module
    assert lazy_import_module.lazy_import(".example", package="app.core") is module
    assert calls == [(".example", "app.core")]
    lazy_import_module.clear_lazy_import_cache()


def test_lazy_import_does_not_cache_failed_load(monkeypatch: pytest.MonkeyPatch) -> None:
    lazy_import_module.clear_lazy_import_cache()
    calls = 0
    module = ModuleType("available_later")

    def fail_once(name: str, package: str | None = None) -> ModuleType:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ImportError("not installed yet")
        return module

    monkeypatch.setattr(importlib, "import_module", fail_once)
    with pytest.raises(ImportError):
        lazy_import_module.lazy_import("available_later")
    assert lazy_import_module.lazy_import("available_later") is module
    assert calls == 2
    lazy_import_module.clear_lazy_import_cache()


def test_lazy_import_rejects_empty_names_and_collapses_concurrent_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lazy_import_module.clear_lazy_import_cache()
    with pytest.raises(ValueError, match="non-empty"):
        lazy_import_module.lazy_import(" ")

    calls: list[str] = []
    module = ModuleType("concurrent_optional_module")
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    class RendezvousLock:
        def __init__(self) -> None:
            self.entries = 0

        def __enter__(self) -> None:
            with lock:
                self.entries += 1
                synchronize = self.entries <= 8
            if synchronize:
                barrier.wait(timeout=2)
            lock.acquire()

        def __exit__(self, *_args: object) -> None:
            lock.release()

    monkeypatch.setattr(lazy_import_module, "_lock", RendezvousLock())

    def fake_import(name: str, package: str | None = None) -> ModuleType:
        calls.append(name)
        return module

    monkeypatch.setattr(importlib, "import_module", fake_import)

    def load() -> ModuleType:
        barrier.wait(timeout=2)
        return lazy_import_module.lazy_import("concurrent_optional_module")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: load(), range(8)))
    assert all(result is module for result in results)
    assert calls == ["concurrent_optional_module"]
    lazy_import_module.clear_lazy_import_cache()
