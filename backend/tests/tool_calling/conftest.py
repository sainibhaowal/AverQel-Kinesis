"""Pytest configuration for tool-calling tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add backend root to path
BACKEND_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Test configuration
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LM_STUDIO_URL", "http://localhost:1234/v1")
os.environ.setdefault("LM_STUDIO_MODEL", "spark-x2.6-1.7b")
os.environ.setdefault("LM_STUDIO_TIMEOUT", "60")

# Pytest plugins
pytest_plugins = [
    "backend.tests.tool_calling.disposable_backend",
]


# Custom markers
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "tool_calling: marks tests as tool-calling integration tests"
    )
    config.addinivalue_line(
        "markers", "recorder: marks tests that record fixtures (require LM Studio)"
    )
    config.addinivalue_line("markers", "replay: marks tests that replay fixtures (CI-safe)")
    config.addinivalue_line("markers", "nightly: marks tests for nightly runs only")
    config.addinivalue_line("markers", "slow: marks tests as slow (>10s)")
    config.addinivalue_line(
        "markers", "requires_lm_studio: marks tests requiring LM Studio running"
    )


# Session-scoped event loop for async fixtures
import pytest_asyncio

pytest_asyncio.loop_scope = "session"
