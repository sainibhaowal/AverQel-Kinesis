"""The remote runtime proved against a real SSH server.

The unit checks for the remote runtime use double connections, so they prove
the boundary logic and not the transport.  This module stands up a real SSH
server on the loopback interface, with a generated host key, a generated
client key, and an ``authorized_keys`` file, and points the product's own
remote runtime at it: the same trust store, the same settings, and the same
tools the model calls.

The test-only dependency is the SSH server.  ``asyncssh`` is not installed by
default, and the module skips when it is missing.  The product's remote
runtime runs every command through the OpenSSH client, so the module skips
when that client is not installed either.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.models.ssh_host import DeepSpaceSshHost
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.ssh import SSH_STREAM_TOOLS, SSH_TOOLS
from app.deepspace.services.tools.ssh_fs import SSH_FS_TOOLS
from app.deepspace.services.tools.ssh_terminal import SSH_TERMINAL_TOOLS

try:  # pragma: no cover - the test-only SSH server is not installed by default
    import asyncssh
except ImportError:  # pragma: no cover - the proof skips without the server
    asyncssh = None  # type: ignore[assignment]

pytestmark = pytest.mark.unit_no_db

#: The name the reviewed host is reached by.
HOST_NAME = "build"

#: The user the remote command runs as.
HOST_USER = "tester"

#: The name of the private key the deployment holds for the host.
KEY_NAME = "id_ed25519"

#: The file the read proof finds on the host.
NOTES_NAME = "notes.txt"

#: The content of that file.
NOTES_TEXT = "remote notes\n"

#: The marker the streamed command prints first.
STREAM_FIRST_MARKER = "stream-first"

#: The marker the streamed command prints while it is still running.
STREAM_SECOND_MARKER = "stream-second"

#: The marker the remote terminal prints.
TERMINAL_MARKER = "terminal-ok"


def _asyncssh_missing() -> bool:
    """Report whether the test-only SSH server is not installed.

    Returns:
        True when ``asyncssh`` cannot be imported.
    """
    return asyncssh is None


def _ssh_client_missing() -> bool:
    """Report whether the OpenSSH client is not installed.

    The product's remote runtime runs every command through the ``ssh``
    binary, so a host without that client cannot run the proof.

    Returns:
        True when the ``ssh`` binary is not on the search path.
    """
    return shutil.which("ssh") is None


requires_ssh_server = pytest.mark.skipif(
    _asyncssh_missing(),
    reason=(
        "asyncssh is not installed; the real-host proof needs the test-only SSH server. "
        "Install it with: pip install -r requirements-dev.txt"
    ),
)

requires_ssh_client = pytest.mark.skipif(
    _ssh_client_missing(),
    reason="the OpenSSH client is not installed; the remote runtime runs commands through it.",
)

requires_posix = pytest.mark.skipif(
    os.name != "posix",
    reason="the remote path boundary is resolved by a POSIX shell on the host.",
)


@dataclass(frozen=True, slots=True)
class RealSshHost:
    """One real SSH server the product's remote runtime may reach.

    Attributes:
        port: The port the server listens on.
        key_directory: The directory the deployment holds the private key in.
        workspace: The reviewed remote directory on the host.
        known_hosts_entry: The host key the connection must present.
    """

    port: int
    key_directory: Path
    workspace: Path
    known_hosts_entry: str


async def _pump_input(source: Any, sink: Any) -> None:
    """Copy what the client sends into the remote process.

    Args:
        source: The client's input stream.
        sink: The remote process's input stream.
    """
    try:
        async for chunk in source:
            sink.write(chunk)
            await sink.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            sink.close()
        except (BrokenPipeError, OSError):
            pass


async def _pump_output(source: Any, sink: Any) -> None:
    """Copy what the remote process prints back to the client.

    Args:
        source: The remote process's output stream.
        sink: The client's output stream.
    """
    while True:
        chunk = await source.read(65_536)
        if not chunk:
            return
        sink.write(chunk)


async def _run_remote_command(process: Any) -> None:
    """Run the command the client asked for with the host's own shell.

    The server runs the command the way a host's login shell runs it, so the
    product's remote prelude, its path resolution, and its shell pipeline are
    executed on the host rather than simulated.

    Args:
        process: The server-side session of one command.
    """
    command = str(process.command or os.environ.get("SHELL") or "/bin/sh")
    environment = dict(os.environ)
    environment.update({str(key): str(value) for key, value in (process.env or {}).items()})
    child = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    await asyncio.gather(
        _pump_input(process.stdin, child.stdin),
        _pump_output(child.stdout, process.stdout),
        _pump_output(child.stderr, process.stderr),
    )
    await child.wait()
    process.exit(child.returncode or 0)


@pytest.fixture
async def real_ssh_host(tmp_path: Path) -> AsyncIterator[RealSshHost]:
    """Start a real SSH server on the loopback interface.

    The server holds a generated host key, accepts one generated client key
    through an ``authorized_keys`` file, and runs every command with the
    host's own shell.  Nothing outside the loopback interface is reached.

    Args:
        tmp_path: The test's own directory.

    Yields:
        The running server.
    """
    if asyncssh is None:  # pragma: no cover - the module skips without it
        pytest.skip("asyncssh is not installed.")
    key_directory = tmp_path / "keys"
    key_directory.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    private_key = key_directory / KEY_NAME
    private_key.write_bytes(client_key.export_private_key("openssh"))
    private_key.chmod(0o600)

    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text(
        client_key.export_public_key("openssh").decode("utf-8").strip() + "\n",
        encoding="utf-8",
    )

    server = await asyncssh.create_server(
        None,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=str(authorized_keys),
        process_factory=_run_remote_command,
        encoding=None,
    )
    port = int(server.get_port() or 0)
    known_hosts_entry = (
        f"[127.0.0.1]:{port} {host_key.export_public_key('openssh').decode('utf-8').strip()}"
    )
    try:
        yield RealSshHost(
            port=port,
            key_directory=key_directory,
            workspace=workspace,
            known_hosts_entry=known_hosts_entry,
        )
    finally:
        server.close()
        await server.wait_closed()


class _HostResult:
    """A stand-in for the result of the host lookup.

    Attributes:
        row: The reviewed host the lookup answers with.
    """

    def __init__(self, row: DeepSpaceSshHost) -> None:
        """Create the stand-in.

        Args:
            row: The reviewed host.
        """
        self.row = row

    def scalar_one_or_none(self) -> DeepSpaceSshHost:
        """Return the one host the lookup found.

        Returns:
            The host.
        """
        return self.row

    def scalars(self) -> _HostResult:
        """Return the stand-in itself, the way a SQLAlchemy result does.

        Returns:
            This result.
        """
        return self

    def all(self) -> list[DeepSpaceSshHost]:
        """Return every host the lookup found.

        Returns:
            The hosts.
        """
        return [self.row]


class _HostDb:
    """A stand-in for the request's database session.

    The session only has to answer the one lookup the remote runtime makes:
    the reviewed host the caller named.

    Attributes:
        row: The reviewed host the session holds.
    """

    def __init__(self, row: DeepSpaceSshHost) -> None:
        """Create the stand-in.

        Args:
            row: The reviewed host.
        """
        self.row = row

    def execute(self, statement: Any) -> _HostResult:
        """Answer the reviewed host lookup.

        Args:
            statement: The statement the runtime issued.

        Returns:
            The result the lookup answers with.
        """
        entity = statement.column_descriptions[0]["entity"]
        assert entity is DeepSpaceSshHost
        return _HostResult(self.row)


def _tool(name: str) -> Any:
    """Return one remote runtime tool by its name.

    Args:
        name: The tool name.

    Returns:
        The tool spec.
    """
    for spec in (*SSH_TOOLS, *SSH_STREAM_TOOLS, *SSH_FS_TOOLS, *SSH_TERMINAL_TOOLS):
        if spec.name == name:
            return spec
    raise AssertionError(f"{name} is not a remote runtime tool.")


def _context(host: RealSshHost) -> ToolContext:
    """Return the tool context of a conversation that may reach the host.

    Args:
        host: The running SSH server.

    Returns:
        The tool context.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    row = DeepSpaceSshHost(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        name=HOST_NAME,
        hostname="127.0.0.1",
        port=host.port,
        username=HOST_USER,
        known_hosts_entry=host.known_hosts_entry,
        key_secret_ref=KEY_NAME,
        remote_workspace=str(host.workspace),
        enabled=True,
        timeout_seconds=30,
    )
    settings = get_settings().model_copy(
        update={"deepspace_ssh_key_directory": str(host.key_directory)}
    )
    return ToolContext(
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=frozenset({"admin"}),
            token_id="ssh-real-host-test",
        ),
        conversation_id=uuid.uuid4(),
        settings=settings,
        db=_HostDb(row),
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_the_remote_runtime_reads_a_file_from_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A file on the real host is read through the product's own tool."""
    (real_ssh_host.workspace / NOTES_NAME).write_text(NOTES_TEXT, encoding="utf-8")
    context = _context(real_ssh_host)

    result = await _tool("ssh_fs_read").handler(
        {"host": HOST_NAME, "path": NOTES_NAME},
        context,
    )

    assert result == {
        "status": "ok",
        "path": NOTES_NAME,
        "content": NOTES_TEXT,
        "size_bytes": len(NOTES_TEXT.encode("utf-8")),
        "truncated": False,
    }


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_the_remote_runtime_writes_a_file_on_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A file is written on the real host through the product's own tool."""
    context = _context(real_ssh_host)

    result = await _tool("ssh_fs_write").handler(
        {"host": HOST_NAME, "path": "out/report.txt", "content": "written on the host\n"},
        context,
    )

    assert result["status"] == "ok"
    assert result["path"] == "out/report.txt"
    written = real_ssh_host.workspace / "out" / "report.txt"
    assert written.read_text(encoding="utf-8") == "written on the host\n"


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_the_remote_runtime_lists_a_directory_on_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A directory on the real host is listed through the product's own tool."""
    (real_ssh_host.workspace / NOTES_NAME).write_text(NOTES_TEXT, encoding="utf-8")
    (real_ssh_host.workspace / "nested").mkdir()
    context = _context(real_ssh_host)

    result = await _tool("ssh_fs_list").handler({"host": HOST_NAME, "path": "."}, context)

    assert result["status"] == "ok"
    entries = {entry["name"]: entry for entry in result["entries"]}
    assert entries[NOTES_NAME]["kind"] == "file"
    assert entries[NOTES_NAME]["size_bytes"] == len(NOTES_TEXT.encode("utf-8"))
    assert entries["nested"]["kind"] == "directory"


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_a_path_that_leaves_the_reviewed_directory_is_refused(
    real_ssh_host: RealSshHost,
) -> None:
    """A path out of the reviewed directory is refused, on the host and here.

    A link inside the reviewed directory that points out of it is followed and
    refused on the host, and a path that names a parent directory is refused
    before it reaches the host at all.
    """
    outside = real_ssh_host.workspace.parent / "outside.txt"
    outside.write_text("outside the reviewed directory\n", encoding="utf-8")
    (real_ssh_host.workspace / "escape.txt").symlink_to(outside)
    context = _context(real_ssh_host)

    through_link = await _tool("ssh_fs_read").handler(
        {"host": HOST_NAME, "path": "escape.txt"},
        context,
    )
    through_parent = await _tool("ssh_fs_read").handler(
        {"host": HOST_NAME, "path": "../outside.txt"},
        context,
    )

    assert through_link["status"] == "error"
    assert through_link["error_code"] == "SSH_PATH_REFUSED"
    assert through_parent["status"] == "error"
    assert through_parent["error_code"] == "SSH_PATH_REFUSED"


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_the_remote_runtime_runs_a_command_on_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A command runs on the real host and its output comes back."""
    context = _context(real_ssh_host)

    result = await _tool("ssh_run").handler(
        {"host": HOST_NAME, "command": f"printf '{STREAM_FIRST_MARKER}\\n'"},
        context,
    )

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert STREAM_FIRST_MARKER in result["stdout"]


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_a_streamed_command_reports_its_output_on_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A streamed command keeps running on the real host and reports its output."""
    context = _context(real_ssh_host)

    started = await _tool("ssh_run").handler(
        {
            "host": HOST_NAME,
            "command": (
                f"printf '{STREAM_FIRST_MARKER}\\n'; sleep 2; printf '{STREAM_SECOND_MARKER}\\n'"
            ),
            "stream": True,
            "wait_seconds": 10,
        },
        context,
    )

    assert started["status"] == "ok"
    assert started["session_id"].startswith("sshstream-")
    assert STREAM_FIRST_MARKER in started["stdout"]
    assert started["running"] is True
    try:
        read = await _tool("ssh_stream_read").handler(
            {"session_id": started["session_id"], "wait_seconds": 15},
            context,
        )
        assert read["status"] == "ok"
        assert STREAM_SECOND_MARKER in read["stdout"]
    finally:
        closed = await _tool("ssh_stream_close").handler(
            {"session_id": started["session_id"]},
            context,
        )
    assert closed == {"status": "ok", "closed": True}


@requires_ssh_server
@requires_ssh_client
@requires_posix
@pytest.mark.asyncio
async def test_the_remote_terminal_runs_a_command_on_the_real_host(
    real_ssh_host: RealSshHost,
) -> None:
    """A remote terminal stays open on the real host and answers what it is sent."""
    context = _context(real_ssh_host)

    opened = await _tool("ssh_terminal_open").handler({"host": HOST_NAME}, context)

    assert opened["status"] == "ok"
    session_id = str(opened["session_id"])
    assert session_id.startswith("sshterm-")
    try:
        written = await _tool("ssh_terminal_write").handler(
            {"session_id": session_id, "data": f"printf '{TERMINAL_MARKER}\\n'\n"},
            context,
        )
        assert written["status"] == "ok"

        output = ""
        for _ in range(50):
            read = await _tool("ssh_terminal_read").handler(
                {"session_id": session_id},
                context,
            )
            assert read["status"] == "ok"
            output += str(read["output"])
            if TERMINAL_MARKER in output:
                break
            await asyncio.sleep(0.1)
        assert TERMINAL_MARKER in output
    finally:
        closed = await _tool("ssh_terminal_close").handler({"session_id": session_id}, context)
    assert closed == {"status": "ok", "closed": True}
