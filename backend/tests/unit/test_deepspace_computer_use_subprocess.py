from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.tools import computer_use

pytestmark = pytest.mark.unit_no_db


@pytest.mark.asyncio
async def test_computer_use_command_uses_shared_launcher_and_display_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":93")
    monkeypatch.setenv("XAUTHORITY", "/tmp/xauth")
    monkeypatch.setenv("API_KEY", "must-not-be-forwarded")
    calls: list[dict[str, Any]] = []

    class Handle:
        async def wait(self):
            return SimpleNamespace(exit_code=7)

        def collect_final(self, label: str):
            return {"text": f"{label} output", "truncated": False}

    async def spawn(argv: list[str], **kwargs: Any) -> Handle:
        calls.append({"argv": argv, **kwargs})
        return Handle()

    monkeypatch.setattr(computer_use, "spawn_with_stdio", spawn)

    result = await computer_use._run(["xdotool", "click", "1"], timeout=5)

    assert result.returncode == 7
    assert result.stdout == "stdout output"
    assert result.stderr == "stderr output"
    assert calls[0]["argv"] == ["xdotool", "click", "1"]
    assert calls[0]["parent_environment"]["DISPLAY"] == ":93"
    assert calls[0]["parent_environment"]["XAUTHORITY"] == "/tmp/xauth"
    assert "API_KEY" not in calls[0]["parent_environment"]
    assert "capture_output" not in calls[0]
    assert calls[0]["stdio"].stdout.max_bytes == 1_048_576


@pytest.mark.asyncio
async def test_computer_use_timeout_terminates_the_contained_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated = False

    class Handle:
        async def wait(self):
            await asyncio.sleep(1)

        async def terminate_and_wait(self):
            nonlocal terminated
            terminated = True
            return SimpleNamespace(exit_code=-9)

        def collect_final(self, label: str):
            return {"text": f"partial {label}", "truncated": False}

    async def spawn(_argv: list[str], **_kwargs: Any) -> Handle:
        return Handle()

    monkeypatch.setattr(computer_use, "spawn_with_stdio", spawn)

    result = await computer_use._run(["identify", "image.png"], timeout=0.01)

    assert terminated is True
    assert result.returncode == -9
    assert result.stdout == "partial stdout"


@pytest.mark.asyncio
async def test_image_dimension_probe_uses_shared_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(computer_use, "PIL_AVAILABLE", False)
    monkeypatch.setattr(computer_use, "_tool_path", lambda _name: "/usr/bin/identify")

    async def run(argv: list[str], timeout: float = 30.0):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="640 480", stderr="")

    monkeypatch.setattr(computer_use, "_run", run)

    assert await computer_use._image_dimensions(computer_use.Path("shot.png")) == (640, 480)
    assert calls == [["/usr/bin/identify", "-format", "%w %h", "shot.png"]]
