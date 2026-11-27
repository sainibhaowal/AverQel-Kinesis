from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.deepspace.services.tools import browser_automation as ba_module


@pytest.mark.unit_no_db
def test_browser_automation_runtime_is_isolated_per_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ba_module, "browser_automation_unavailable_reason", lambda _s: None)

    started_loops: list[asyncio.AbstractEventLoop] = []

    class _FakeRuntime:
        def __init__(self, config, settings) -> None:
            self.config = config
            self.settings = settings
            self.loop: asyncio.AbstractEventLoop | None = None

        async def ensure_started(self, conversation_id) -> None:
            self.loop = asyncio.get_running_loop()
            started_loops.append(self.loop)

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(ba_module, "BrowserAutomationRuntime", _FakeRuntime)

    conv_id = uuid4()
    ctx = SimpleNamespace(
        conversation_id=conv_id,
        settings=SimpleNamespace(deepspace_browser_automation_enabled=True),
    )

    rt1 = asyncio.run(ba_module._get_runtime(ctx))
    rt2 = asyncio.run(ba_module._get_runtime(ctx))

    assert rt1 is not rt2
    assert len(started_loops) == 2
    assert started_loops[0] is not started_loops[1]
