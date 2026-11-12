from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_lazy_import_cache():
    """Keep cached optional imports from leaking between unit tests."""
    from app.core.lazy_import import clear_lazy_import_cache

    clear_lazy_import_cache()
    yield
    clear_lazy_import_cache()


@pytest.fixture
def require_usable_bubblewrap():
    """Skip OS-cage execution tests when this runner forbids namespaces."""
    from app.deepspace.services.sandbox_backend import BwrapBackend

    if not BwrapBackend().available():
        pytest.skip("this isolated test runner cannot create bubblewrap namespaces")
