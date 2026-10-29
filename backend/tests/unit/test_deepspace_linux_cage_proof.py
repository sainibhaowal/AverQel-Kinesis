"""The Linux cage, proved on the host that runs it.

The sandbox tests elsewhere check the argument vector and how a failure is
classified.  These check the thing that matters: that a command inside the
cage can write in the workspace and **cannot** write anywhere else, cannot
see the user's home, and cannot reach the network when the deployment
forbids it.

The proof needs a host that can create the namespaces.  A host that cannot
is not a failure of the product, so those checks skip with the reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deepspace.services.sandbox_backend import build_sandbox_command

pytestmark = pytest.mark.unit_no_db


def _cage(
    argv: list[str],
    *,
    workspace: Path,
    cwd: Path,
    network: bool = False,
    read_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one command inside the product's own cage.

    Args:
        argv: The command to run.
        workspace: The workspace the cage grants.
        cwd: The directory the command starts in.
        network: Whether the cage may reach the network.
        read_only: Whether the workspace is read-only.

    Returns:
        The command's outcome.
    """
    settings = get_settings()
    command = build_sandbox_command(
        settings,
        argv,
        workspace=workspace,
        cwd=cwd,
        network=network,
        read_only=read_only,
    )
    if not command.sandboxed:
        pytest.skip(f"This host offers no cage ({command.backend}).")
    return subprocess.run(  # noqa: S603 - the argv is the product's own
        command.argv,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


def _bwrap_available() -> bool:
    """Report whether this host can run the cage at all.

    Returns:
        True when the cage can run.
    """
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_command_writes_inside_the_workspace(tmp_path: Path) -> None:
    """A command may write in the workspace it was given."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "written.txt"

    result = _cage(
        ["/bin/sh", "-c", f"printf hello > {target}"],
        workspace=workspace,
        cwd=workspace,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_command_cannot_write_outside_the_workspace(tmp_path: Path) -> None:
    """A command may not write outside the workspace it was given.

    The cage gives the command its own temporary filesystem, so a write
    outside the workspace lands inside the cage and never reaches the host.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"

    _cage(
        ["/bin/sh", "-c", f"printf hello > {outside}"],
        workspace=workspace,
        cwd=workspace,
    )

    # The command may succeed inside the cage, but the host must not have
    # the file.
    assert not outside.exists()


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_read_only_workspace_cannot_be_written(tmp_path: Path) -> None:
    """A read-only workspace refuses a write even inside the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "written.txt"

    result = _cage(
        ["/bin/sh", "-c", f"printf hello > {target}"],
        workspace=workspace,
        cwd=workspace,
        read_only=True,
    )

    assert result.returncode != 0
    assert not target.exists()


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_command_cannot_see_the_users_home(tmp_path: Path) -> None:
    """The user's home directory is not the command's to read."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.txt").write_text("private", encoding="utf-8")
    os.environ["HOME"] = str(home)

    try:
        result = _cage(
            ["/bin/sh", "-c", "cat $HOME/secret.txt 2>/dev/null; echo done"],
            workspace=workspace,
            cwd=workspace,
        )
    finally:
        os.environ.pop("HOME", None)

    assert "private" not in result.stdout


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_command_cannot_reach_the_network_when_the_deployment_forbids_it(
    tmp_path: Path,
) -> None:
    """Without the network, the command has no route out."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _cage(
        [
            "/bin/sh",
            "-c",
            "timeout 5 getent hosts example.com >/dev/null 2>&1; echo exit=$?",
        ],
        workspace=workspace,
        cwd=workspace,
        network=False,
    )

    # The lookup must fail: there is no resolver and no route.
    assert "exit=0" not in result.stdout


@pytest.mark.skipif(not _bwrap_available(), reason="This host cannot create the namespaces.")
def test_a_command_sees_only_its_own_workspace(tmp_path: Path) -> None:
    """The workspace is the only writable place the command knows about."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    result = _cage(
        ["/bin/sh", "-c", "printf x > /tmp/anywhere.txt 2>/dev/null; echo done"],
        workspace=workspace,
        cwd=workspace,
    )

    assert result.returncode == 0
    assert not (Path("/tmp") / "anywhere.txt").exists()
    assert not (outside_dir / "anywhere.txt").exists()
