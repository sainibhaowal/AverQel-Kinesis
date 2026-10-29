"""The cage, proved on whichever host runs it.

The Linux proof lives in ``test_deepspace_linux_cage_proof.py``. This file
is the same proof for every platform: it asks the product for the cage the
host offers, runs a command inside it, and checks what the command could
and could not do.

A host whose cage cannot be built skips the checks with the reason, so
running this file on macOS or on Windows is what produces the proof for
those platforms.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deepspace.services.sandbox_backend import build_sandbox_command, sandbox_status

pytestmark = pytest.mark.unit_no_db


def _cage_or_skip() -> str:
    """Return the name of the cage this host offers.

    Returns:
        The backend name.

    Raises:
        pytest.skip: When the host offers no cage.
    """
    status = sandbox_status(get_settings())
    backend = str(status.get("selected") or "none")
    if backend in {"none", ""}:
        pytest.skip(f"This host offers no cage ({status.get('platform')}).")
    return backend


def _run(
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
    command = build_sandbox_command(
        get_settings(),
        argv,
        workspace=workspace,
        cwd=cwd,
        network=network,
        read_only=read_only,
    )
    if not command.sandboxed:
        pytest.skip("This host offers no cage.")
    return subprocess.run(  # noqa: S603 - the argv is the product's own
        command.argv,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


def _is_windows() -> bool:
    """Report whether the host is Windows.

    Returns:
        True on Windows.
    """
    return sys.platform == "win32"


def test_the_host_offers_a_cage() -> None:
    """The host must offer a cage for the rest of the proof to mean anything."""
    backend = _cage_or_skip()
    assert backend


def test_a_command_writes_inside_the_workspace(tmp_path: Path) -> None:
    """A command may write in the workspace it was given."""
    _cage_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "written.txt"

    result = _run(
        ["/bin/sh", "-c", f"printf hello > {target}"],
        workspace=workspace,
        cwd=workspace,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "hello"


def test_a_command_cannot_write_outside_the_workspace(tmp_path: Path) -> None:
    """A command may not write outside the workspace it was given.

    The cage gives the command its own temporary filesystem, so the write
    lands inside the cage and never reaches the host.
    """
    _cage_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"

    _run(
        ["/bin/sh", "-c", f"printf hello > {outside}"],
        workspace=workspace,
        cwd=workspace,
    )

    assert not outside.exists()


def test_a_read_only_workspace_cannot_be_written(tmp_path: Path) -> None:
    """A read-only workspace refuses a write even inside the workspace."""
    _cage_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "written.txt"

    result = _run(
        ["/bin/sh", "-c", f"printf hello > {target}"],
        workspace=workspace,
        cwd=workspace,
        read_only=True,
    )

    assert result.returncode != 0
    assert not target.exists()


@pytest.mark.skipif(_is_windows(), reason="The Windows boundary confines writes, not reads.")
def test_a_command_cannot_see_the_users_home(tmp_path: Path) -> None:
    """The user's home directory is not the command's to read."""
    _cage_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.txt").write_text("private", encoding="utf-8")
    previous = os.environ.get("HOME")
    os.environ["HOME"] = str(home)

    try:
        result = _run(
            ["/bin/sh", "-c", "cat $HOME/secret.txt 2>/dev/null; echo done"],
            workspace=workspace,
            cwd=workspace,
        )
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous

    assert "private" not in result.stdout


@pytest.mark.skipif(_is_windows(), reason="The Windows boundary does not confine the network.")
def test_a_command_cannot_reach_the_network_when_it_is_forbidden(tmp_path: Path) -> None:
    """Without the network, the command has no route out."""
    _cage_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run(
        ["/bin/sh", "-c", "timeout 5 getent hosts example.com >/dev/null 2>&1; echo exit=$?"],
        workspace=workspace,
        cwd=workspace,
        network=False,
    )

    assert "exit=0" not in result.stdout


def test_the_cage_is_the_one_the_host_offers() -> None:
    """The proof must run against the cage the deployment would use."""
    backend = _cage_or_skip()
    settings = get_settings()
    command = build_sandbox_command(
        settings,
        ["/bin/sh", "-c", "true"],
        workspace=Path(os.environ.get("TMPDIR", "/tmp")),
        cwd=Path(os.environ.get("TMPDIR", "/tmp")),
    )

    assert command.sandboxed
    assert command.backend == backend
    assert shutil.which("bwrap") is not None or backend != "bwrap"
