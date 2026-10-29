"""Tests for the remote runtime.

The runtime reaches a host the way the command runner does - the reviewed
host key is checked, the private key never leaves the server, and the
client process receives the reduced environment - and it adds a boundary
the command runner does not have: every path is resolved on the host,
inside the host's reviewed remote directory.

The checks cover that boundary, the trust settings the transfers and the
terminal use, the ownership of a remote session, the idle sweeper, and the
rule that a failed remote call is a result and not an exception.  The
connections are doubles, so the checks never reach a host.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import ssh as ssh_service
from app.deepspace.services.ssh import SshHost
from app.deepspace.services.ssh_runtime import (
    REMOTE_PATH_REFUSED,
    TERMINAL_SESSION_PREFIX,
    RemoteNotFoundError,
    RemotePathRefusedError,
    SshRuntime,
    SshRuntimeError,
    SshStreamRegistry,
    SshTerminalManager,
    SshTerminalNotFoundError,
    build_delete_command,
    build_list_command,
    build_read_command,
    build_terminal_command,
    build_write_command,
    normalize_remote_path,
)
from app.deepspace.services.tools import ssh_fs as ssh_fs_tools
from app.deepspace.services.tools import ssh_terminal as ssh_terminal_tools
from app.deepspace.services.tools._base import ToolContext, ToolHandler

pytestmark = pytest.mark.unit_no_db

#: A reviewed host key entry, in known_hosts form.
HOST_KEY = "build.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial"


def _host(**overrides: object) -> SshHost:
    """Build one reviewed host for a test.

    Args:
        overrides: The fields to override.

    Returns:
        The host.
    """
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "build",
        "hostname": "build.example.com",
        "port": 22,
        "username": "deploy",
        "known_hosts_entry": HOST_KEY,
        "key_secret_ref": None,
        "remote_workspace": "/srv/app",
        "enabled": True,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return SshHost(**values)  # type: ignore[arg-type]


class _FakeProcess:
    """A subprocess double that answers with fixed bytes."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        """Create the double.

        Args:
            stdout: The bytes the command wrote to its standard output.
            stderr: The bytes the command wrote to its standard error.
            returncode: The code the command exited with.
        """
        self.stdout_bytes = stdout
        self.stderr_bytes = stderr
        self.returncode = returncode
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.terminated = False
        self.input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Return the fixed output and record what the command read.

        Args:
            input: The bytes the command read from its standard input.

        Returns:
            The standard output and the standard error.
        """
        self.input = input
        return self.stdout_bytes, self.stderr_bytes

    def terminate(self) -> None:
        """Record that the connection was ended."""
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        """Record that the connection was killed."""
        self.returncode = -9

    async def wait(self) -> int:
        """Return the code the command exited with.

        Returns:
            The exit code.
        """
        return self.returncode


@dataclass
class _Call:
    """One connection the runtime opened."""

    argv: list[str]
    env: dict[str, str] | None
    known_hosts: str


def _patch_exec(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
) -> list[_Call]:
    """Replace the process launcher and record the connections it opens.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        process: The process the launcher hands back.

    Returns:
        The recorded connections.
    """
    recorded: list[_Call] = []

    async def fake_create(*argv: str, **kwargs: Any) -> _FakeProcess:
        known_hosts = ""
        for part in argv:
            if part.startswith("UserKnownHostsFile="):
                path = Path(part.split("=", 1)[1])
                if path.exists():
                    known_hosts = path.read_text(encoding="utf-8")
        recorded.append(
            _Call(
                argv=list(argv),
                env=kwargs.get("env"),
                known_hosts=known_hosts,
            )
        )
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return recorded


def _context(**overrides: Any) -> ToolContext:
    """Build a tool context double for the remote runtime tools.

    Args:
        overrides: The context fields to replace.

    Returns:
        The tool context.
    """
    values: dict[str, Any] = {
        "auth": SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        "conversation_id": uuid.uuid4(),
        "settings": SimpleNamespace(
            deepspace_ssh_key_directory="",
            deepspace_ssh_runtime_enabled=True,
            deepspace_terminal_idle_timeout_seconds=900.0,
        ),
        "db": None,
        "task_store": SimpleNamespace(),
        "tool_result_store": None,
        "provider_registry": SimpleNamespace(),
    }
    values.update(overrides)
    return ToolContext(**values)


def _handler(module: Any, name: str) -> ToolHandler:
    """Return the handler one tool declares.

    Args:
        module: The tool module that declares the tool.
        name: The tool's name.

    Returns:
        The tool's handler.
    """
    for attribute in ("SSH_FS_TOOLS", "SSH_TERMINAL_TOOLS", "SSH_TOOLS", "SSH_STREAM_TOOLS"):
        for spec in getattr(module, attribute, []):
            if spec.name == name:
                return spec.handler
    raise AssertionError(f"no tool named {name!r}")


def _key(tmp_path: Path) -> Path:
    """Write a private key the deployment holds.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The key's path.
    """
    key = tmp_path / "deploy"
    key.write_text("PRIVATE KEY", encoding="utf-8")
    key.chmod(0o600)
    return key


# --- The path boundary -------------------------------------------------


def _run_on_the_host(command: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Run one remote command in a local shell.

    The command the runtime builds is shell code that runs on the host, so
    the checks run it in a shell to see what it does to a real directory
    tree.  No host is reached.

    Args:
        command: The remote command line.
        stdin: The bytes the command reads from its standard input.

    Returns:
        The command's outcome.
    """
    return subprocess.run(
        ["sh", "-c", command],
        input=stdin,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "posix", reason="the resolver runs in a POSIX shell")
def test_the_resolver_refuses_a_path_that_leaves_the_reviewed_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("inner", encoding="utf-8")
    (outside / "c.txt").write_text("secret", encoding="utf-8")
    (root / "esc").symlink_to(outside)
    (root / "inside").symlink_to(root / "sub")
    host = _host(remote_workspace=str(root))

    inside = _run_on_the_host(build_read_command(host, "a.txt"))
    assert inside.returncode == 0
    assert inside.stdout == b"hello"

    # A link that stays inside the reviewed directory is followed.
    linked = _run_on_the_host(build_read_command(host, "inside/b.txt"))
    assert linked.returncode == 0
    assert linked.stdout == b"inner"

    # A parent directory leaves the reviewed directory.
    assert _run_on_the_host(build_read_command(host, "../outside/c.txt")).returncode == (
        REMOTE_PATH_REFUSED
    )

    # A link that leaves the reviewed directory is refused as well.
    assert _run_on_the_host(build_read_command(host, "esc/c.txt")).returncode == (
        REMOTE_PATH_REFUSED
    )


@pytest.mark.skipif(os.name != "posix", reason="the resolver runs in a POSIX shell")
def test_the_resolver_writes_through_a_temporary_name(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = _host(remote_workspace=str(root))

    written = _run_on_the_host(build_write_command(host, "new/deep/c.txt"), stdin=b"payload")

    assert written.returncode == 0
    assert (root / "new" / "deep" / "c.txt").read_text(encoding="utf-8") == "payload"
    assert list(root.rglob(".deepspace-tmp*")) == []


@pytest.mark.skipif(os.name != "posix", reason="the resolver runs in a POSIX shell")
def test_the_resolver_lists_the_reviewed_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    host = _host(remote_workspace=str(root))

    listed = _run_on_the_host(build_list_command(host, ""))

    assert listed.returncode == 0
    assert b"file\t5\ta.txt" in listed.stdout
    assert b"directory\t0\tsub" in listed.stdout


def test_the_remote_command_checks_the_path_against_the_reviewed_directory() -> None:
    command = build_read_command(_host(), "notes.txt")

    # The reviewed directory is bound on the host.
    assert "DS_ROOT=/srv/app" in command
    # The path is resolved physically, so a link cannot hide an escape.
    assert "ds_phys()" in command
    assert "cd -P" in command
    # The resolved path is accepted only when it lies under the directory.
    assert 'case "$_ds_cur/" in' in command
    assert '"$_ds_root"/*' in command
    # A path outside the directory is refused with the refusal code.
    assert "return 4" in command
    assert "exit $?" in command


def test_every_remote_file_command_carries_the_check() -> None:
    host = _host()

    for command in (
        build_read_command(host, "a.txt"),
        build_write_command(host, "a.txt"),
        build_list_command(host, "a.txt"),
        build_delete_command(host, "a.txt"),
    ):
        assert "DS_ROOT=/srv/app" in command
        assert "ds_resolve" in command
        assert '"$_ds_root"/*' in command


def test_a_write_goes_through_a_temporary_name_and_a_rename() -> None:
    command = build_write_command(_host(), "notes.txt")

    assert 'cat > "$_tmp"' in command
    assert 'mv -f "$_tmp" "$_target"' in command


def test_a_path_that_leaves_the_reviewed_directory_is_refused() -> None:
    with pytest.raises(RemotePathRefusedError):
        normalize_remote_path("../etc/passwd")

    with pytest.raises(RemotePathRefusedError):
        normalize_remote_path("/etc/passwd")

    with pytest.raises(RemotePathRefusedError):
        normalize_remote_path("notes/../../etc/passwd")


def test_a_host_without_a_reviewed_directory_has_no_reachable_files() -> None:
    host = _host(remote_workspace=None)

    with pytest.raises(RemotePathRefusedError):
        build_read_command(host, "a.txt")


def test_the_remote_terminal_runs_in_the_reviewed_directory() -> None:
    command = build_terminal_command(_host())

    assert "cd /srv/app" in command
    assert "exec" in command


# --- The trust rules ---------------------------------------------------


async def test_the_remote_file_command_uses_the_reviewed_key_and_the_reduced_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _key(tmp_path)
    host = _host(key_secret_ref="deploy")
    process = _FakeProcess(
        stdout=b"hello",
        stderr=b"DEEPSPACE_META size=5 path=/srv/app/a.txt\n",
    )
    recorded = _patch_exec(monkeypatch, process)

    result = await SshRuntime(key_directory=tmp_path).read_file(host, "a.txt")

    assert result.content == "hello"
    assert result.size_bytes == 5
    argv = recorded[0].argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "BatchMode=yes" in argv
    assert str(key) in argv
    assert recorded[0].known_hosts.strip() == HOST_KEY
    environment = recorded[0].env
    assert environment is not None
    assert environment["DEEPSPACE_SANDBOX_BACKEND"] == "ssh"
    assert "AKS_JWT_SECRET" not in environment


async def test_the_remote_terminal_uses_the_reviewed_key_and_a_pseudo_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _key(tmp_path)
    host = _host(key_secret_ref="deploy")
    recorded = _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()

    session = await manager.open(
        tenant_id=host.tenant_id,
        user_id=host.user_id,
        conversation_id=uuid.uuid4(),
        host=host,
        key_directory=tmp_path,
    )
    await manager.close_all()

    argv = recorded[0].argv
    assert "-tt" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert str(key) in argv
    assert recorded[0].known_hosts.strip() == HOST_KEY
    assert session.id.startswith(TERMINAL_SESSION_PREFIX)
    environment = recorded[0].env
    assert environment is not None
    assert environment["DEEPSPACE_SANDBOX_BACKEND"] == "ssh"


async def test_a_remote_write_sends_the_content_to_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stderr=b"DEEPSPACE_META size=5 path=/srv/app/a.txt\n")
    _patch_exec(monkeypatch, process)

    await SshRuntime(key_directory=tmp_path).write_file(_host(), "a.txt", "hello")

    assert process.input == b"hello"


# --- The remote session ------------------------------------------------


async def test_a_remote_session_belongs_to_the_conversation_that_opened_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await manager.open(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        host=_host(),
        key_directory=tmp_path,
    )

    assert (
        manager.get(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        is session
    )
    with pytest.raises(SshTerminalNotFoundError):
        manager.get(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.uuid4(),
        )
    await manager.close_all()


async def test_the_idle_sweeper_closes_a_remote_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await manager.open(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        host=_host(),
        key_directory=tmp_path,
    )
    session.idle_deadline = time.monotonic() - 1.0

    closed = await manager.sweep()

    assert closed == [session.id]
    with pytest.raises(SshTerminalNotFoundError):
        manager.get(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


async def test_a_remote_session_id_does_not_name_a_local_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()
    session = await manager.open(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        host=_host(),
        key_directory=tmp_path,
    )

    assert session.id.startswith(TERMINAL_SESSION_PREFIX)
    # A local terminal session id is a bare hexadecimal digest.
    assert not session.id.removeprefix(TERMINAL_SESSION_PREFIX).startswith("ssh")
    await manager.close_all()


# --- The tool results --------------------------------------------------


async def test_a_failed_remote_command_is_a_result_and_not_an_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess(returncode=5))
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_read")(
        {"host": "build", "path": "a.txt"},
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "SSH_NOT_FOUND"
    assert isinstance(result["message"], str)


async def test_a_refused_path_is_a_result_and_not_an_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess(returncode=4))
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_read")(
        {"host": "build", "path": "../etc/passwd"},
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "SSH_PATH_REFUSED"


async def test_a_host_that_does_not_exist_is_refused_with_the_known_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.ssh import SshHostNotFoundError

    def refuse(context: Any, name: str) -> SshHost:
        raise SshHostNotFoundError("No such SSH host.")

    monkeypatch.setattr(ssh_fs_tools, "resolve_host", refuse)
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_list")(
        {"host": "build", "path": "."},
        context,
    )

    assert result == {"status": "error", "message": "No such SSH host."}


async def test_the_read_tool_returns_the_content_and_the_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(
        monkeypatch,
        _FakeProcess(
            stdout=b"hello",
            stderr=b"DEEPSPACE_META size=5 path=/srv/app/a.txt\n",
        ),
    )
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_read")(
        {"host": "build", "path": "a.txt"},
        context,
    )

    assert result["status"] == "ok"
    assert result["path"] == "a.txt"
    assert result["content"] == "hello"
    assert result["size_bytes"] == 5


async def test_the_list_tool_returns_the_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(
        monkeypatch,
        _FakeProcess(
            stdout=b"file\t12\ta.txt\ndirectory\t0\tsub\n",
            stderr=b"DEEPSPACE_META path=/srv/app\n",
        ),
    )
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_list")(
        {"host": "build", "path": "."},
        context,
    )

    assert result["status"] == "ok"
    assert result["path"] == "."
    assert result["entries"] == [
        {"name": "a.txt", "kind": "file", "size_bytes": 12},
        {"name": "sub", "kind": "directory", "size_bytes": 0},
    ]


async def test_the_write_tool_returns_the_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(
        monkeypatch,
        _FakeProcess(stderr=b"DEEPSPACE_META size=5 path=/srv/app/a.txt\n"),
    )
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_write")(
        {"host": "build", "path": "a.txt", "content": "hello"},
        context,
    )

    assert result == {"status": "ok", "path": "a.txt", "size_bytes": 5}


async def test_the_delete_tool_reports_the_removed_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess(stderr=b"DEEPSPACE_META path=/srv/app/a.txt\n"))
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_fs_tools, "ssh_fs_delete")(
        {"host": "build", "path": "a.txt"},
        context,
    )

    assert result == {"status": "ok", "path": "a.txt", "deleted": True}


async def test_the_remote_runtime_can_be_switched_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_fs_tools, "resolve_host", lambda context, name: _host())
    settings = SimpleNamespace(
        deepspace_ssh_key_directory="",
        deepspace_ssh_runtime_enabled=False,
    )
    context = _context(settings=settings)

    result = await _handler(ssh_fs_tools, "ssh_fs_read")(
        {"host": "build", "path": "a.txt"},
        context,
    )

    assert result == {
        "status": "error",
        "message": ssh_fs_tools.RUNTIME_DISABLED_MESSAGE,
    }


# --- The remote terminal tools -----------------------------------------


async def test_a_session_of_another_conversation_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()
    monkeypatch.setattr(ssh_terminal_tools, "ssh_terminal_manager", lambda: manager)
    conversation_id = uuid.uuid4()
    session = await manager.open(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=conversation_id,
        host=_host(),
        key_directory=tmp_path,
    )
    other = _context(conversation_id=uuid.uuid4())

    result = await _handler(ssh_terminal_tools, "ssh_terminal_read")(
        {"session_id": session.id},
        other,
    )

    assert result == {
        "status": "error",
        "message": ssh_terminal_tools.NO_SESSION_MESSAGE,
    }
    await manager.close_all()


async def test_the_open_tool_returns_the_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_exec(monkeypatch, _FakeProcess())
    manager = SshTerminalManager()
    monkeypatch.setattr(ssh_terminal_tools, "ssh_terminal_manager", lambda: manager)
    monkeypatch.setattr(ssh_terminal_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_terminal_tools, "ssh_terminal_open")(
        {"host": "build"},
        context,
    )

    assert result["status"] == "ok"
    assert result["session_id"].startswith(TERMINAL_SESSION_PREFIX)
    await manager.close_all()


async def test_the_terminal_tools_declare_their_names_and_modes() -> None:
    modes = {spec.name: spec.mode for spec in ssh_terminal_tools.SSH_TERMINAL_TOOLS}
    fs_modes = {spec.name: spec.mode for spec in ssh_fs_tools.SSH_FS_TOOLS}

    assert modes == {
        "ssh_terminal_open": "write",
        "ssh_terminal_read": "read",
        "ssh_terminal_write": "write",
        "ssh_terminal_signal": "write",
        "ssh_terminal_close": "write",
    }
    assert fs_modes == {
        "ssh_fs_read": "read",
        "ssh_fs_write": "write",
        "ssh_fs_list": "read",
        "ssh_fs_delete": "write",
    }


# --- The command runner ------------------------------------------------


async def test_a_streamed_command_returns_the_output_it_produced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import ssh as ssh_tools

    process = _FakeProcess()
    process.returncode = None
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_data(b"first line\n")
    process.stdout.feed_eof()
    _patch_exec(monkeypatch, process)
    registry = SshStreamRegistry()
    monkeypatch.setattr(ssh_tools, "ssh_stream_registry", lambda: registry)
    monkeypatch.setattr(ssh_tools, "resolve_host", lambda context, name: _host())
    context = _context()

    result = await _handler(ssh_tools, "ssh_run")(
        {"host": "build", "command": "ls", "stream": True},
        context,
    )

    assert result["status"] == "ok"
    assert result["stream"] is True
    assert result["stdout"] == "first line\n"
    assert result["session_id"].startswith("sshstream-")
    await registry.close_all()


async def test_a_streamed_command_of_another_conversation_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import ssh as ssh_tools

    process = _FakeProcess()
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_eof()
    _patch_exec(monkeypatch, process)
    registry = SshStreamRegistry()
    monkeypatch.setattr(ssh_tools, "ssh_stream_registry", lambda: registry)
    monkeypatch.setattr(ssh_tools, "resolve_host", lambda context, name: _host())
    owner = _context()
    session = await registry.open(
        tenant_id=owner.auth.tenant_id,
        user_id=owner.auth.user_id,
        conversation_id=owner.conversation_id,
        host=_host(),
        command="ls",
        key_directory=tmp_path,
    )

    other = _context()
    result = await _handler(ssh_tools, "ssh_stream_read")(
        {"session_id": session.id},
        other,
    )

    assert result == {
        "status": "error",
        "message": ssh_tools.NO_SESSION_MESSAGE,
    }
    await registry.close_all()


def test_the_command_runner_keeps_its_result_shape() -> None:
    from app.deepspace.services.tools import ssh as ssh_tools

    modes = {spec.name: spec.mode for spec in ssh_tools.SSH_TOOLS}
    stream_modes = {spec.name: spec.mode for spec in ssh_tools.SSH_STREAM_TOOLS}

    assert modes == {"ssh_run": "write", "ssh_host_list": "read"}
    assert stream_modes == {"ssh_stream_read": "read", "ssh_stream_close": "write"}


def test_the_executor_still_runs_a_command_in_the_reviewed_directory() -> None:
    from app.deepspace.services.ssh import build_remote_command

    assert build_remote_command(_host(), "ls -la") == "cd /srv/app && ls -la"


def test_the_executor_raises_the_failure_a_host_reports() -> None:
    from app.deepspace.services.ssh import SshError, SshTrustError

    with pytest.raises(SshError):
        ssh_service.build_remote_command(_host(), "  ")
    with pytest.raises(SshTrustError):
        ssh_service.prepare_trust(
            _host(known_hosts_entry=""),
            key_directory=Path("/tmp"),
        )


def test_the_runtime_reports_a_missing_path() -> None:
    assert issubclass(RemoteNotFoundError, SshRuntimeError)
    assert issubclass(RemotePathRefusedError, SshRuntimeError)
