"""The Playwright browser engine must run under shared process containment."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import browser_runtime
from app.deepspace.services.tools import browser_automation

pytestmark = pytest.mark.unit_no_db


@pytest.mark.asyncio
async def test_managed_playwright_server_reports_hosted_browser_startup_errors(
    tmp_path: Path,
) -> None:
    """The actual shared-launcher Node host returns errors and is reaped cleanly."""
    with pytest.raises(browser_runtime.BrowserUnavailableError, match="browser could not start"):
        await browser_runtime.ManagedPlaywrightServer.start(
            browser_type="not-a-browser",
            launch_options={},
            environment={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_managed_playwright_server_uses_shared_launcher_and_private_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser server is launched through the helper and binds only to loopback."""
    output = asyncio.StreamReader()
    launch_calls: list[dict[str, Any]] = []

    class FakeProcess:
        returncode: int | None = None

    class FakeWriter:
        closed = False

        def write(self, payload: bytes) -> None:
            request = json.loads(payload)
            launch_calls.append(request)
            options = request["options"]
            endpoint = f"ws://127.0.0.1:43210/{options['wsPath']}"
            output.feed_data(json.dumps({"endpoint": endpoint}).encode() + b"\n")

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.closed = True

    writer = FakeWriter()
    process = FakeProcess()

    class FakeHandle:
        stdin = writer
        stdout = output

        def __init__(self) -> None:
            self.process = process

        async def wait(self):
            process.returncode = 0
            return SimpleNamespace(exit_code=0)

        async def terminate_and_wait(self):
            process.returncode = -9
            return SimpleNamespace(exit_code=-9)

    handle = FakeHandle()

    async def fake_spawn(argv, **kwargs):
        launch_calls.append({"argv": argv, **kwargs})
        return handle

    monkeypatch.setattr("app.deepspace.services.subprocess_stdio.spawn_with_stdio", fake_spawn)
    server = await browser_runtime.ManagedPlaywrightServer.start(
        browser_type="firefox",
        launch_options={"headless": False, "args": ["--private"]},
        environment={"PATH": "/safe/path", "HOME": "/private/profile"},
    )

    spawn = launch_calls[0]
    request = launch_calls[1]
    assert spawn["argv"][1] == "-e"
    assert spawn["argv"][3].endswith("/playwright/driver/package")
    assert spawn["parent_environment"] == {"PATH": "/safe/path", "HOME": "/private/profile"}
    assert spawn["stdio"].stdin == "pipe"
    assert spawn["stdio"].stdout == "pipe"
    assert request["browser_type"] == "firefox"
    assert request["options"]["headless"] is False
    assert request["options"]["args"] == ["--private"]
    assert request["options"]["host"] == "127.0.0.1"
    assert request["options"]["port"] == 0
    assert server.endpoint == f"ws://127.0.0.1:43210/{request['options']['wsPath']}"
    await server.close()
    assert writer.closed is True
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_browser_automation_connects_to_managed_server_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browser automation connects to the contained server rather than launching directly."""
    profile = tmp_path / "profile"
    profile.mkdir()
    captured: dict[str, Any] = {}

    class FakePage:
        async def close(self) -> None:
            return None

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        def set_default_timeout(self, _timeout: int) -> None:
            return None

        def set_default_navigation_timeout(self, _timeout: int) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeBrowser:
        async def new_context(self, **kwargs):
            captured["context"] = kwargs
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakeBrowserType:
        async def connect(self, endpoint: str, *, timeout: int):
            captured["endpoint"] = endpoint
            captured["timeout"] = timeout
            return FakeBrowser()

        async def launch(self, **_kwargs):
            raise AssertionError("Playwright must not launch the browser outside containment")

    class FakePlaywright:
        chromium = FakeBrowserType()

        async def stop(self) -> None:
            captured["playwright_stopped"] = True

    class FakePlaywrightContext:
        async def start(self):
            return FakePlaywright()

    class FakeManagedServer:
        endpoint = "ws://127.0.0.1:43210/private-token"

        async def close(self) -> None:
            captured["server_closed"] = True

    async def fake_server_start(cls, **kwargs):
        captured["launch_options"] = kwargs["launch_options"]
        captured["environment"] = kwargs["environment"]
        return FakeManagedServer()

    monkeypatch.setattr(browser_automation, "_playwright_factory", FakePlaywrightContext)
    monkeypatch.setenv("DISPLAY", ":9")
    monkeypatch.setenv("XAUTHORITY", str(tmp_path / "xauthority"))
    monkeypatch.setattr(browser_runtime, "_browser_profile_dir", lambda: profile)
    monkeypatch.setattr(
        browser_runtime,
        "build_browser_environment",
        lambda path, mode: {"HOME": str(path), "DEEPSPACE_SANDBOX_MODE": mode},
    )
    monkeypatch.setattr(browser_runtime, "find_browser_executable", lambda: "/opt/chromium")
    monkeypatch.setattr(
        browser_runtime.ManagedPlaywrightServer,
        "start",
        classmethod(fake_server_start),
    )

    runtime = browser_automation.BrowserAutomationRuntime(
        browser_automation.BrowserAutomationConfig(
            browser_type="chromium",
            headless=False,
            extra_args=("--disable-gpu",),
            proxy={"server": "http://proxy.internal:8080"},
        ),
        SimpleNamespace(deepspace_sandbox_policy="workspace-write"),
    )
    session = await runtime.start(uuid.uuid4())

    assert session.browser is not None
    assert captured["endpoint"] == FakeManagedServer.endpoint
    assert captured["timeout"] == 30_000
    assert captured["launch_options"] == {
        "headless": False,
        "args": ["--disable-gpu"],
        "proxy": {"server": "http://proxy.internal:8080"},
        "executablePath": "/opt/chromium",
    }
    assert captured["environment"]["HOME"] == str(profile)
    assert captured["environment"]["DISPLAY"] == ":9"
    assert captured["environment"]["XAUTHORITY"] == str(tmp_path / "xauthority")
    await runtime.stop()
    assert captured["server_closed"] is True
    assert captured["playwright_stopped"] is True
    assert profile.exists() is False
