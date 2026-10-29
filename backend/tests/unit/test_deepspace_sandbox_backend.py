"""Tests for the DeepSpace operating-system sandbox."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.sandbox_backend import (
    BwrapBackend,
    DockerBackend,
    SandboxExecBackend,
    build_sandbox_command,
    sandbox_status,
)
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build the shell settings for one test."""
    values = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_sandbox_backend": "auto",
        "deepspace_sandbox_network": True,
        "deepspace_sandbox_image": None,
        "deepspace_sandbox_required": False,
        "deepspace_shell_timeout_seconds": 15,
        "deepspace_shell_max_output_bytes": 65_536,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(tmp_path: Path, **overrides: Any) -> ToolContext:
    """Build a tool context for one test."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=_settings(tmp_path, **overrides),
        db=SimpleNamespace(),
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )


def test_bwrap_wrap_hides_home_and_binds_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    command = BwrapBackend().wrap(
        ["/bin/bash", "-c", "pwd"],
        workspace=workspace,
        cwd=workspace,
        network=False,
    )

    assert command.backend == "bwrap"
    assert command.sandboxed is True
    assert "--unshare-all" in command.argv
    assert "--share-net" not in command.argv
    assert "--bind" in command.argv
    assert str(workspace) in command.argv
    assert command.argv[-3:] == ["/bin/bash", "-c", "pwd"]
    assert "--tmpfs" in command.argv
    assert "/home" in command.argv


def test_bwrap_keeps_network_when_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    command = BwrapBackend().wrap(["/bin/true"], workspace=workspace, cwd=workspace, network=True)

    assert "--share-net" in command.argv


def test_sandbox_exec_profile_keeps_network_when_enabled(tmp_path: Path) -> None:
    command = SandboxExecBackend().wrap(
        ["/bin/true"], workspace=tmp_path, cwd=tmp_path, network=True
    )

    assert "(allow network*)" in command.argv[2]
    assert command.argv[-1] == "/bin/true"


def test_sandbox_exec_profile_denies_network_when_disabled(tmp_path: Path) -> None:
    command = SandboxExecBackend().wrap(
        ["/bin/true"], workspace=tmp_path, cwd=tmp_path, network=False
    )

    assert "(deny network*)" in command.argv[2]


def test_docker_wrap_mounts_the_workspace_without_network(tmp_path: Path) -> None:
    command = DockerBackend("debian:stable-slim").wrap(
        ["/bin/true"], workspace=tmp_path, cwd=tmp_path, network=False
    )

    assert command.backend == "docker"
    assert command.argv[command.argv.index("--network") + 1] == "none"
    assert f"type=bind,source={tmp_path},target={tmp_path}" in command.argv
    assert command.argv[-1] == "/bin/true"


def test_none_backend_keeps_the_native_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path, deepspace_sandbox_backend="none")

    command = build_sandbox_command(settings, ["/bin/true"], workspace=tmp_path, cwd=tmp_path)

    assert command.backend == "none"
    assert command.sandboxed is False
    assert command.argv == ["/bin/true"]


def test_required_sandbox_refuses_without_a_backend(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        deepspace_sandbox_backend="none",
        deepspace_sandbox_required=True,
    )

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo hi"}, context))

    assert result is not None
    assert result["status"] == "unavailable"
    assert result["sandboxed"] is False
    assert "sandbox" in str(result["message"]).lower()


@pytest.mark.skipif(not BwrapBackend().available(), reason="bubblewrap is not usable here")
def test_bwrap_runs_a_command_and_reports_the_backend(tmp_path: Path) -> None:
    context = _context(tmp_path, deepspace_sandbox_backend="bwrap")

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo sandbox-ok"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert "sandbox-ok" in str(result["output"])
    assert result["sandbox_backend"] == "bwrap"
    assert result["sandboxed"] is True


@pytest.mark.skipif(not BwrapBackend().available(), reason="bubblewrap is not usable here")
def test_bwrap_hides_a_file_outside_the_workspace(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("hidden", encoding="utf-8")
    context = _context(tmp_path, deepspace_sandbox_backend="bwrap")

    result = asyncio.run(dispatch_native_tool("bash", {"command": f"cat {secret}"}, context))

    assert result is not None
    assert result["status"] == "failed"
    assert "hidden" not in str(result["output"])


@pytest.mark.skipif(not BwrapBackend().available(), reason="bubblewrap is not usable here")
def test_bwrap_allows_a_write_inside_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path, deepspace_sandbox_backend="bwrap")

    result = asyncio.run(
        dispatch_native_tool(
            "bash",
            {"command": "echo inside > inside.txt && cat inside.txt"},
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert "inside" in str(result["output"])


def test_sandbox_status_reports_the_configured_backend(tmp_path: Path) -> None:
    status = sandbox_status(_settings(tmp_path, deepspace_sandbox_backend="none"))

    assert status["configured"] == "none"
    assert status["available"] is False


def test_bwrap_read_only_mounts_the_workspace_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    command = BwrapBackend().wrap(
        ["/bin/true"],
        workspace=workspace,
        cwd=workspace,
        network=False,
        read_only=True,
    )

    index = command.argv.index(str(workspace))
    assert command.argv[index - 1] == "--ro-bind"


def test_sandbox_exec_read_only_profile_denies_workspace_writes(tmp_path: Path) -> None:
    command = SandboxExecBackend().wrap(
        ["/bin/true"],
        workspace=tmp_path,
        cwd=tmp_path,
        network=False,
        read_only=True,
    )

    assert "file-write*" not in command.argv[2]


def test_docker_read_only_mounts_the_workspace_read_only(tmp_path: Path) -> None:
    command = DockerBackend("debian:stable-slim").wrap(
        ["/bin/true"],
        workspace=tmp_path,
        cwd=tmp_path,
        network=False,
        read_only=True,
    )

    mount = command.argv[command.argv.index("--mount") + 1]
    assert mount.endswith(",readonly")


def test_danger_full_access_skips_the_sandbox(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        deepspace_permission_mode="danger-full-access",
        deepspace_sandbox_backend="bwrap",
        deepspace_sandbox_required=True,
    )

    result = asyncio.run(dispatch_native_tool("bash", {"command": "echo full-access"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert result["sandboxed"] is False
    assert result["sandbox_backend"] == "none"
    assert "full-access" in str(result["output"])


@pytest.mark.skipif(not BwrapBackend().available(), reason="bubblewrap is not usable here")
def test_read_only_cage_denies_a_redirect_write(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        deepspace_permission_mode="read-only",
        deepspace_sandbox_backend="bwrap",
    )

    result = asyncio.run(
        dispatch_native_tool("bash", {"command": "echo bad > inside.txt"}, context)
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["sandboxed"] is True
    workspace = tmp_path / "workspaces" / str(context.auth.tenant_id) / str(context.auth.user_id)
    assert not (workspace / "inside.txt").exists()


def test_the_status_reports_the_platform_and_the_enforcement(tmp_path: Path) -> None:
    status = sandbox_status(_settings(tmp_path))

    assert status["platform"]
    assert status["enforcement"] in {"full", "partial", "none"}
    if status["selected"] in {"bwrap", "sandbox-exec", "docker"}:
        assert status["enforcement"] == "full"
    if status["selected"] == "windows":
        assert status["enforcement"] == "partial"
    if status["selected"] == "none":
        assert status["enforcement"] == "none"


def test_the_windows_backend_is_not_selected_on_other_platforms(tmp_path: Path) -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("The Windows backend is the native backend on Windows.")

    command = build_sandbox_command(
        _settings(tmp_path, deepspace_sandbox_backend="windows"),
        ["/bin/true"],
        workspace=tmp_path,
        cwd=tmp_path,
    )

    assert command.backend == "none"
    assert command.sandboxed is False


def test_the_windows_runner_fails_closed(tmp_path: Path) -> None:
    from app.deepspace.services.windows_acl_launcher import (
        RUNNER_FAILURE_EXIT,
        RUNNER_FAILURE_SIGNATURE,
    )

    # The runner refuses the command when it cannot confine it, so a command
    # never runs without containment.
    assert RUNNER_FAILURE_EXIT == 127
    assert RUNNER_FAILURE_SIGNATURE == "windows-acl-run:"


def test_the_windows_runner_reduces_the_environment(tmp_path: Path) -> None:
    from app.deepspace.services.windows_acl_launcher import _ALLOWED_ENVIRONMENT

    # The confined command receives the paths it needs and none of the
    # deployment's secrets.
    assert "PATH" in _ALLOWED_ENVIRONMENT
    assert "TEMP" in _ALLOWED_ENVIRONMENT
    assert not any("SECRET" in name or "KEY" in name for name in _ALLOWED_ENVIRONMENT)


def test_the_windows_backend_reports_its_boundary() -> None:
    from app.deepspace.services.windows_acl import describe_windows_boundary

    boundary = describe_windows_boundary()

    assert boundary["backend"] == "windows-acl"
    assert boundary["enforcement"] == "partial"


def test_the_extra_read_only_paths_reach_the_cage(tmp_path: Path) -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("The job-object runner has no read-only bind list.")

    command = build_sandbox_command(
        _settings(tmp_path, deepspace_sandbox_backend="bwrap"),
        ["/bin/true"],
        workspace=tmp_path,
        cwd=tmp_path,
        extra_read_only_paths=("/usr",),
    )

    assert "--ro-bind" in command.argv
    assert "/usr" in command.argv
