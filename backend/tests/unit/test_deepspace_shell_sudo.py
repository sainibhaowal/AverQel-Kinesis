from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.tools import shell as shell_module


@pytest.mark.unit_no_db
def test_prepare_linux_privilege_env_wires_native_askpass_in_full_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("SUDO_ASKPASS", raising=False)
    monkeypatch.setattr(
        shell_module.shutil,
        "which",
        lambda name: "/usr/bin/ssh-askpass" if name == "ssh-askpass" else None,
    )

    cmd, env, cleanup = shell_module._prepare_linux_privilege_env(
        kind="bash",
        command="sudo apt-get update",
        policy="danger-full-access",
        env={"PATH": "/usr/bin"},
    )

    assert cleanup is None
    assert env["DISPLAY"] == ":0"
    assert env["SUDO_ASKPASS"] == "/usr/bin/ssh-askpass"
    assert "command sudo -A" in cmd
    assert "sudo apt-get update" in cmd


@pytest.mark.unit_no_db
def test_prepare_linux_privilege_env_creates_gui_askpass_script_when_zenity_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SUDO_ASKPASS", raising=False)
    monkeypatch.setattr(
        shell_module.shutil,
        "which",
        lambda name: "/usr/bin/zenity" if name == "zenity" else None,
    )

    cmd, env, cleanup = shell_module._prepare_linux_privilege_env(
        kind="bash",
        command="sudo systemctl status nginx",
        policy="danger-full-access",
        env={"PATH": "/usr/bin"},
    )

    try:
        assert cleanup is not None
        assert cleanup.exists()
        assert env["SUDO_ASKPASS"] == str(cleanup)
        assert env["WAYLAND_DISPLAY"] == "wayland-0"
        assert "zenity" in cleanup.read_text(encoding="utf-8")
        assert "command sudo -A" in cmd
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)


@pytest.mark.unit_no_db
def test_prepare_linux_privilege_env_never_wires_askpass_outside_full_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    cmd, env, cleanup = shell_module._prepare_linux_privilege_env(
        kind="bash",
        command="sudo id",
        policy="workspace-write",
        env={"PATH": "/usr/bin"},
    )

    assert cleanup is None
    assert cmd == "sudo id"
    assert "DISPLAY" not in env
    assert "SUDO_ASKPASS" not in env


@pytest.mark.asyncio
@pytest.mark.unit_no_db
async def test_run_command_reports_clear_guidance_on_headless_sudo_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("SUDO_ASKPASS", raising=False)
    monkeypatch.setattr(shell_module, "_shell_program", lambda kind: "/bin/bash")

    class _FakeStream:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._sent = False

        async def read(self, _n: int) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return self._data

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdout = _FakeStream(b"")
            self.stderr = _FakeStream(b"sudo: a terminal is required to read the password\n")

        async def wait(self) -> int:
            return 1

    class _FakeHandle:
        def __init__(self) -> None:
            self.process = _FakeProcess()

        async def wait(self) -> int:
            return 1

        async def terminate_and_wait(self) -> None:
            return None

    async def fake_spawn(*args, **kwargs):
        return _FakeHandle()

    monkeypatch.setattr(shell_module, "spawn_with_stdio", fake_spawn)

    result = await shell_module._run_command(
        kind="bash",
        command="sudo apt-get update",
        workspace=tmp_path,
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=10_000,
        settings=SimpleNamespace(),
        permission_mode="danger-full-access",
    )

    assert result["status"] == "failed"
    assert result["sudo_requires_terminal"] is True
    assert "never paste your password into chat" in result["message"].lower()
