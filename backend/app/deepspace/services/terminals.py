"""Persistent terminal sessions for DeepSpace.

A terminal session is a long-lived child process the agent can write to and
read from across several tool calls, so a build that takes a minute, a REPL,
or an interactive prompt survives between turns.  This module owns the
process layer: it starts a child inside the conversation's workspace, keeps a
bounded buffer of what the child printed, and ends the child when the session
is closed or goes idle.

The child runs behind a pseudo-terminal on POSIX, so a program that checks
whether it has a terminal - a shell, a REPL, a pager - behaves the way it
does for a person.  The standard library has no pseudo-terminal on Windows,
so a session there runs with ordinary pipes and reports ``pty`` as False.

A session never inherits the deployment's environment.  The child receives a
reduced environment built from a fixed list of variables, so a credential held
in the server's environment is not readable from inside a session.

A session belongs to the tenant, user, and conversation that opened it.  The
manager answers a request for a session that belongs to somebody else exactly
as it answers a request for a session that does not exist, so a caller cannot
learn that another tenant's session exists.

The manager also reports what a session did with the input it received.  A
write settles with one reason: the session asked for more input, the session
went quiet, the wait ran out, or the session's process is gone.  A caller that
waits for a reason learns whether the command it sent is still running, and a
caller that sends a signal to the session's process group can interrupt a
command that hangs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.deepspace.services.workspace_root import (
    resolve_within_root,
    workspace_root_for,
)

#: The shell kinds a terminal session can run.
ShellKind = Literal["bash", "pwsh", "cmd"]

#: Every shell kind, in the order the resolver reports them.
SHELL_KINDS: tuple[ShellKind, ...] = ("bash", "pwsh", "cmd")

#: The largest output one session keeps, in bytes.
DEFAULT_MAX_BUFFER_BYTES = 262_144

#: The largest output one read returns by default, in bytes.
DEFAULT_READ_BYTES = 16_384

#: The largest output one read may return, in bytes.
MAX_READ_BYTES = 262_144

#: The number of live sessions one conversation may hold.
DEFAULT_MAX_SESSIONS_PER_CONVERSATION = 4

#: The number of live sessions one deployment may hold.
DEFAULT_MAX_SESSIONS = 64

#: The idle time after which a session is swept, in seconds.
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0

#: The directory inside the workspace a session writes temporary files to.
TEMP_DIR_NAME = ".deepspace-tmp"

#: The program search path a session falls back to.
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

#: The locale a session falls back to.
DEFAULT_LANG = "C.UTF-8"

#: The longest wait one write may request, in seconds.
MAX_WAIT_SECONDS = 300

#: The silence after which a write reports that the session is probably finished.
DEFAULT_IDLE_WINDOW_SECONDS = 1.0

#: The time between two observations of a session's foreground state, in seconds.
WAIT_POLL_INTERVAL_SECONDS = 0.05

#: The time a write waits before it trusts an observation of a waiting session.
WAIT_SETTLE_SECONDS = 0.15

#: The number of observations in a row that confirm a session waits for input.
WAIT_CONFIRMATIONS = 2

#: The signals a caller may send to a session's foreground process group.
SUPPORTED_SIGNALS: tuple[str, ...] = (
    "SIGINT",
    "SIGTERM",
    "SIGHUP",
    "SIGQUIT",
    "SIGKILL",
    "SIGTSTP",
    "SIGCONT",
)

#: The system call numbers of the input waits, per architecture.
#:
#: The kernel publishes the system call a process is blocked in with the
#: numbers of the architecture it runs on, so a host reports the input wait
#: only when its numbers are known.  Every known table is consulted, because
#: a host may run a program built for another architecture.
SYSCALL_TABLES: dict[str, dict[str, int]] = {
    "x86_64": {
        "read": 0,
        "select": 23,
        "pselect": 270,
        "poll": 7,
        "ppoll": 271,
        "epoll_wait": 232,
        "epoll_pwait": 281,
    },
    "aarch64": {
        "read": 63,
        "pselect": 72,
        "ppoll": 73,
        "epoll_pwait": 22,
    },
}

#: The reasons a write to a terminal session may settle with.
type WaitReason = Literal["sent", "stdin_read", "inferred_idle", "timeout", "session_exit"]


class TerminalError(RuntimeError):
    """A terminal session could not be opened, used, or closed."""


class ShellUnavailableError(TerminalError):
    """The requested shell is not available on this host."""


class TerminalNotFoundError(TerminalError):
    """No live session with that id belongs to the caller."""


class TerminalLimitError(TerminalError):
    """The deployment's terminal session limit is reached."""


def resolve_shell(kind: ShellKind) -> list[str]:
    """Resolve the argument vector prefix that starts one shell.

    Args:
        kind: The shell to start.

    Returns:
        The argument vector prefix, for example ``["/bin/bash"]``.

    Raises:
        ShellUnavailableError: When the host has no such shell.
    """
    if kind == "bash":
        for candidate in ("/bin/bash", "/usr/bin/bash"):
            if os.path.exists(candidate):
                return [candidate]
        found = shutil.which("bash")
        if found is not None:
            return [found]
        raise ShellUnavailableError("No bash shell is available on this host.")
    if kind == "pwsh":
        for candidate in ("pwsh", "powershell"):
            found = shutil.which(candidate)
            if found is not None:
                return [found]
        raise ShellUnavailableError("No PowerShell shell is available on this host.")
    if kind == "cmd":
        if os.name != "nt":
            raise ShellUnavailableError("cmd.exe is only available on Windows.")
        found = shutil.which("cmd")
        return [found or "cmd", "/Q"]
    raise ShellUnavailableError(f"Unknown shell kind: {kind!r}.")


def available_shells() -> tuple[ShellKind, ...]:
    """Return the shell kinds this host can actually start.

    Returns:
        The kinds that resolve without an error.
    """
    kinds: list[ShellKind] = []
    for kind in SHELL_KINDS:
        try:
            resolve_shell(kind)
        except ShellUnavailableError:
            continue
        kinds.append(kind)
    return tuple(kinds)


def default_shell() -> ShellKind:
    """Return the best interactive shell available on this host.

    Returns:
        ``bash`` on POSIX hosts when it is installed, or the first usable
        shell for the host.  Windows prefers PowerShell and then ``cmd``.

    Raises:
        ShellUnavailableError: When no supported shell is installed.
    """
    available = available_shells()
    preferences: tuple[ShellKind, ...]
    if os.name == "nt":
        preferences = ("pwsh", "cmd", "bash")
    else:
        preferences = ("bash", "pwsh", "cmd")
    for kind in preferences:
        if kind in available:
            return kind
    raise ShellUnavailableError("No supported interactive shell is available on this host.")


def signal_number(name: str) -> int | None:
    """Return the number of one supported signal.

    Args:
        name: The signal's name, such as ``SIGINT``.  A name without the
            ``SIG`` prefix is accepted as well.

    Returns:
        The signal's number, or None when the host has no such signal.
    """
    normalized = str(name or "").strip().upper()
    if not normalized.startswith("SIG"):
        normalized = f"SIG{normalized}"
    if normalized not in SUPPORTED_SIGNALS:
        return None
    number = getattr(signal, normalized, None)
    if number is None:
        return None
    return int(number)


def _build_argv(kind: ShellKind, command: str | None) -> list[str]:
    """Build the argument vector that starts one shell.

    Args:
        kind: The shell to start.
        command: The command to run at startup, or None for an interactive
            shell.

    Returns:
        The complete argument vector.
    """
    prefix = resolve_shell(kind)
    if not command:
        # No startup command: the shell is the session, and the agent talks
        # to it for as long as it lives.
        if kind == "bash":
            # The host's interactive bashrc replaces PS1 with a prompt that
            # abbreviates the workspace as `~`. Keep this session's prompt
            # controlled by its workspace environment while retaining Bash's
            # real PTY, readline, job-control, and shell semantics.
            return [*prefix, "--noprofile", "--norc"]
        if kind == "pwsh":
            return [*prefix, "-NoLogo", "-NoProfile"]
        return prefix
    # A startup command is the session's process. It ends with the command.
    if kind == "bash":
        return [*prefix, "-c", command]
    if kind == "pwsh":
        return [*prefix, "-NoLogo", "-NoProfile", "-Command", command]
    return [*prefix, "/C", command]


def _sandbox_mode(settings: Any) -> str:
    """Return the containment mode the deployment runs in.

    Args:
        settings: The deployment settings in force.

    Returns:
        ``read-only``, ``workspace-write``, or ``danger-full-access``.
    """
    return str(
        getattr(settings, "deepspace_sandbox_policy", None)
        or getattr(settings, "deepspace_permission_mode", "workspace-write")
    )


def _setting_int(settings: Any, name: str, default: int) -> int:
    """Read one positive integer setting, falling back to a default.

    Args:
        settings: The deployment settings in force.
        name: The setting's name.
        default: The value to use when the setting is absent or unusable.

    Returns:
        The setting's value.
    """
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _setting_float(settings: Any, name: str, default: float) -> float:
    """Read one number setting, falling back to a default.

    Args:
        settings: The deployment settings in force.
        name: The setting's name.
        default: The value to use when the setting is absent or unusable.

    Returns:
        The setting's value.
    """
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def build_session_environment(
    *,
    workspace: Path,
    shell: ShellKind,
    mode: str = "workspace-write",
) -> dict[str, str]:
    """Build the reduced environment a session's child receives.

    The child must not inherit the server's environment, because that
    environment holds the deployment's credentials.  The session therefore
    receives a fixed list of variables: the program search path, the locale,
    the terminal type, the workspace as the home directory, a temporary
    directory inside the workspace, and the markers that tell a program it
    runs under DeepSpace.

    Args:
        workspace: The workspace root the session is confined to.
        shell: The shell kind the session runs.
        mode: The deployment's containment mode, reported to the child.

    Returns:
        The environment for the child process.
    """
    temp_dir = workspace / TEMP_DIR_NAME
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        temp_dir = workspace
    environment = {
        "PATH": os.environ.get("PATH") or DEFAULT_PATH,
        "HOME": str(workspace),
        "TMPDIR": str(temp_dir),
        "LANG": os.environ.get("LANG") or DEFAULT_LANG,
        "TERM": "xterm-256color",
        "DEEPSPACE_SHELL": "1",
        "DEEPSPACE_SANDBOX": "1",
        "DEEPSPACE_SANDBOX_MODE": mode,
        "DEEPSPACE_SHELL_KIND": str(shell),
        "DEEPSPACE_WORKSPACE": str(workspace),
    }
    if shell == "bash":
        # HOME intentionally points at the workspace for the reduced shell
        # environment. Bash would render that directory as `~`, so keep the
        # prompt honest by showing the actual current directory.
        environment["PS1"] = r"\u@\h:$(pwd)\$ "
    return environment


@dataclass(slots=True)
class OutputBuffer:
    """A bounded buffer that keeps the tail of a session's output.

    The buffer drops the oldest bytes once it reaches its cap, so a session
    that prints more than the cap keeps the most recent output and reports
    how many bytes were lost.
    """

    #: The largest number of bytes the buffer keeps.
    max_bytes: int = DEFAULT_MAX_BUFFER_BYTES
    #: The number of bytes dropped because the buffer reached its cap.
    dropped_bytes: int = 0
    _chunks: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _size: int = field(default=0, init=False, repr=False)

    def append(self, data: bytes) -> None:
        """Add output to the buffer.

        Args:
            data: The bytes the child produced.
        """
        if not data:
            return
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self.max_bytes and self._chunks:
            oldest = self._chunks[0]
            excess = self._size - self.max_bytes
            if len(oldest) <= excess:
                self._chunks.popleft()
                self._size -= len(oldest)
                self.dropped_bytes += len(oldest)
            else:
                self._chunks[0] = oldest[excess:]
                self._size -= excess
                self.dropped_bytes += excess

    def read(self, max_bytes: int) -> str:
        """Return and remove the oldest buffered output.

        Args:
            max_bytes: The largest number of bytes to return.

        Returns:
            The decoded output.
        """
        if max_bytes <= 0:
            return ""
        taken = bytearray()
        while self._chunks and len(taken) < max_bytes:
            chunk = self._chunks[0]
            room = max_bytes - len(taken)
            if len(chunk) <= room:
                taken.extend(chunk)
                self._chunks.popleft()
                self._size -= len(chunk)
            else:
                taken.extend(chunk[:room])
                self._chunks[0] = chunk[room:]
                self._size -= room
        return taken.decode("utf-8", errors="replace")

    def peek(self, max_bytes: int) -> str:
        """Return the oldest buffered output without removing it.

        Args:
            max_bytes: The largest number of bytes to return.

        Returns:
            The decoded output.
        """
        if max_bytes <= 0:
            return ""
        data = bytearray()
        for chunk in self._chunks:
            if len(data) >= max_bytes:
                break
            data.extend(chunk[: max_bytes - len(data)])
        return data.decode("utf-8", errors="replace")

    @property
    def pending_bytes(self) -> int:
        """Return the number of buffered bytes that are not read yet."""
        return self._size


@dataclass(slots=True)
class ManagedProcess:
    """One child process a terminal session drives.

    The process owns a bounded buffer of what the child printed and a reader
    task that keeps that buffer current.  A read never blocks the event loop:
    the reader task awaits the pipe and fills the buffer, and a read only
    takes bytes the buffer already holds.
    """

    #: The argument vector the child was started with.
    argv: list[str]
    #: The shell kind the child runs.
    shell: ShellKind
    #: The directory the child starts in.
    cwd: Path
    #: The running child process.
    process: asyncio.subprocess.Process
    #: Whether the child runs behind a pseudo-terminal.
    pty: bool
    #: The bounded buffer of the child's output.
    buffer: OutputBuffer = field(default_factory=OutputBuffer)
    #: The idle time after which the session is swept.
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    #: The monotonic deadline after which the session is swept.
    idle_deadline: float = field(default_factory=time.monotonic)
    #: The wall-clock time of the session's last use.
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The number of bytes the child has produced since it started.
    produced_bytes: int = 0
    #: The monotonic time of the child's most recent output.
    last_output_at: float = field(default_factory=time.monotonic)
    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _read_transport: asyncio.ReadTransport | None = field(default=None, init=False, repr=False)
    _master_fd: int | None = field(default=None, init=False, repr=False)
    _pending: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _writer_registered: bool = field(default=False, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _subscribers: set[asyncio.Queue[bytes | None]] = field(
        default_factory=set, init=False, repr=False
    )

    @classmethod
    async def start(
        cls,
        *,
        argv: Sequence[str],
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> ManagedProcess:
        """Start one child process and begin collecting its output.

        On POSIX the child runs behind a pseudo-terminal, so it sees a real
        terminal.  On Windows it runs with ordinary pipes and its standard
        error is merged into its standard output.

        Args:
            argv: The argument vector to start.
            shell: The shell kind the child runs.
            cwd: The directory the child starts in.
            env: The environment the child receives.
            max_buffer_bytes: The largest output the session keeps.
            idle_timeout_seconds: The idle time after which the session is
                swept.

        Returns:
            The running process with its reader task started.
        """
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=dict(env),
            )
            managed = cls(
                argv=list(argv),
                shell=shell,
                cwd=cwd,
                process=process,
                pty=False,
                buffer=OutputBuffer(max_bytes=max_buffer_bytes),
                idle_timeout_seconds=idle_timeout_seconds,
            )
        else:
            managed = await cls._start_pty(
                argv=argv,
                shell=shell,
                cwd=cwd,
                env=env,
                max_buffer_bytes=max_buffer_bytes,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        managed._loop = asyncio.get_running_loop()
        managed.touch()
        managed._task = asyncio.create_task(managed._drain())
        return managed

    @classmethod
    async def _start_pty(
        cls,
        *,
        argv: Sequence[str],
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        max_buffer_bytes: int,
        idle_timeout_seconds: float,
    ) -> ManagedProcess:
        """Start one child behind a pseudo-terminal.

        Args:
            argv: The argument vector to start.
            shell: The shell kind the child runs.
            cwd: The directory the child starts in.
            env: The environment the child receives.
            max_buffer_bytes: The largest output the session keeps.
            idle_timeout_seconds: The idle time after which the session is
                swept.

        Returns:
            The running process with its reader connected.
        """
        master_fd, slave_fd = _open_pty()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(cwd),
                env=dict(env),
                start_new_session=True,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        reader, transport = await _connect_reader(master_fd)
        managed = cls(
            argv=list(argv),
            shell=shell,
            cwd=cwd,
            process=process,
            pty=True,
            buffer=OutputBuffer(max_bytes=max_buffer_bytes),
            idle_timeout_seconds=idle_timeout_seconds,
        )
        managed._reader = reader
        managed._read_transport = transport
        managed._master_fd = master_fd
        return managed

    @property
    def is_alive(self) -> bool:
        """Report whether the child is still running."""
        return not self._closed and self.process.returncode is None

    @property
    def is_running(self) -> bool:
        """Report whether the child's process is still running.

        Unlike :attr:`is_alive`, this stays true while the session is being
        closed, so the teardown can still signal a child that is on its way
        out.
        """
        return self.process.returncode is None

    @property
    def pending_bytes(self) -> int:
        """Return the number of buffered bytes that are not read yet."""
        return self.buffer.pending_bytes

    def touch(self) -> None:
        """Extend the idle deadline and record the time of this use."""
        self.idle_deadline = time.monotonic() + self.idle_timeout_seconds
        self.last_used_at = datetime.now(UTC)

    def read(self, max_bytes: int) -> str:
        """Return and remove the oldest buffered output.

        Args:
            max_bytes: The largest number of bytes to return.

        Returns:
            The decoded output.
        """
        return self.buffer.read(max_bytes)

    def peek(self, max_bytes: int) -> str:
        """Return the oldest buffered output without removing it.

        Args:
            max_bytes: The largest number of bytes to return.

        Returns:
            The decoded output.
        """
        return self.buffer.peek(max_bytes)

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """Subscribe to output produced after this call.

        Returns:
            A queue containing raw PTY output.  ``None`` marks end of output.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Stop sending output to one terminal stream.

        Args:
            queue: The queue returned by :meth:`subscribe`.
        """
        self._subscribers.discard(queue)

    def resize(self, *, columns: int, rows: int) -> None:
        """Set the terminal window size seen by the child process.

        Args:
            columns: The number of terminal columns.
            rows: The number of terminal rows.

        Raises:
            TerminalError: When the dimensions are invalid or the host cannot
                resize this session's terminal.
        """
        if not 1 <= columns <= 500 or not 1 <= rows <= 500:
            raise TerminalError("Terminal dimensions must be between 1 and 500.")
        if not self.pty or self._master_fd is None:
            return
        import fcntl  # noqa: PLC0415 - POSIX terminal support is optional.
        import struct  # noqa: PLC0415 - POSIX terminal support is optional.
        import termios  # noqa: PLC0415 - POSIX terminal support is optional.

        try:
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, 0, 0),
            )
        except OSError as exc:
            raise TerminalError("This terminal session could not be resized.") from exc

    async def write(self, data: str) -> int:
        """Send input to the child.

        Args:
            data: The text to send.

        Returns:
            The number of bytes sent.  A child that has already exited
            receives nothing, so the write reports no bytes.
        """
        if not self.is_running:
            return 0
        payload = data.encode("utf-8")
        if self.pty and self._master_fd is not None:
            self._pending.extend(payload)
            self._flush_pending()
            if self._pending and not self._writer_registered and self._loop is not None:
                self._loop.add_writer(self._master_fd, self._flush_pending)
                self._writer_registered = True
            return len(payload)
        stream = self.process.stdin
        if stream is None:
            raise TerminalError("This terminal session has no input stream.")
        try:
            stream.write(payload)
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TerminalError("This terminal session's input stream is closed.") from exc
        return len(payload)

    def waits_for_input(self) -> bool:
        """Report whether the session is blocked waiting for input.

        A process that waits for terminal input blocks in a system call that
        watches its standard input.  The kernel publishes that call in
        ``/proc/<pid>/syscall`` on the hosts that record it, and the
        descriptors the call watches are readable from the process's memory.
        A host that does not record the call cannot answer the question, and
        the session then reports no exact wait.

        The session's shell and the commands it runs are inspected, so a
        program that reads the terminal - a pager, a REPL - is recognized as
        well as the shell that waits for a command line.

        Returns:
            True when the session is waiting for input.
        """
        if os.name != "posix":
            return False
        pids = [self.process.pid, *_child_pids(self.process.pid)]
        return any(_waits_for_stdin(pid) for pid in pids)

    def foreground_pgid(self) -> int:
        """Return the process group the session's terminal gives the foreground.

        A shell with job control runs every command in a process group of its
        own, and the terminal names that group as its foreground group.  The
        signal therefore reaches the command that runs and not only the shell
        that started it.  A session without job control keeps every command
        in the group the child was started in.

        Returns:
            The foreground process group, or 0 when the host reports none.
        """
        group = _terminal_foreground_pgid(self.process.pid)
        if group is not None and group > 0:
            return group
        try:
            return os.getpgid(self.process.pid)
        except (ProcessLookupError, OSError):
            return 0

    def signal(self, sig: int) -> bool:
        """Send a signal to the session's foreground process group.

        Args:
            sig: The signal to send.

        Returns:
            True when the signal reached the process group.
        """
        if not self.is_running:
            return False
        if os.name == "posix":
            group = self.foreground_pgid()
            if group <= 0:
                return False
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return False
            return True
        try:
            self.process.send_signal(sig)
        except (ProcessLookupError, OSError, ValueError):
            return False
        return True

    def _signal_group(self, sig: int) -> bool:
        """Send a signal to the group the child was started in.

        The teardown ends the whole session, so it reaches the shell and
        every command the shell runs, not only the foreground group.

        Args:
            sig: The signal to send.

        Returns:
            True when the signal reached the process group.
        """
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(self.process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                return False
            return True
        try:
            self.process.send_signal(sig)
        except (ProcessLookupError, OSError, ValueError):
            return False
        return True

    def _flush_pending(self) -> None:
        """Write the pending input to the pseudo-terminal."""
        master_fd = self._master_fd
        if master_fd is None:
            return
        while self._pending:
            try:
                written = os.write(master_fd, self._pending)
            except BlockingIOError:
                return
            except OSError:
                self._pending.clear()
                break
            del self._pending[:written]
        if not self._pending and self._writer_registered and self._loop is not None:
            self._loop.remove_writer(master_fd)
            self._writer_registered = False

    async def _read_chunk(self) -> bytes:
        """Read the next chunk of output from the child.

        Returns:
            The bytes the child produced, or an empty result at end of output.
        """
        if self._reader is not None:
            return await self._reader.read(65_536)
        stream = self.process.stdout
        if stream is None:
            return b""
        return await stream.read(65_536)

    async def _drain(self) -> None:
        """Keep the buffer filled with what the child prints."""
        try:
            while True:
                chunk = await self._read_chunk()
                if not chunk:
                    return
                self.buffer.append(chunk)
                self.produced_bytes += len(chunk)
                self.last_output_at = time.monotonic()
                for queue in tuple(self._subscribers):
                    queue.put_nowait(chunk)
        except (OSError, EOFError):
            # A pseudo-terminal reports end of output as a read error once the
            # child has gone; the session keeps the output collected so far.
            return
        finally:
            for queue in tuple(self._subscribers):
                queue.put_nowait(None)

    async def _terminate(self) -> None:
        """Stop the child and wait for it to exit."""
        if self.process.returncode is not None:
            return
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass
        self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=2.0)

    async def close(self) -> None:
        """End the child and release the session's resources."""
        if self._closed:
            return
        self._closed = True
        if self._master_fd is not None and self._loop is not None and self._writer_registered:
            self._loop.remove_writer(self._master_fd)
            self._writer_registered = False
        self._pending.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._terminate()
        if self._read_transport is not None:
            self._read_transport.close()
            self._read_transport = None
        self._reader = None
        self._master_fd = None
        self._subscribers.clear()


def _open_pty() -> tuple[int, int]:
    """Open a pseudo-terminal pair.

    Returns:
        The master and slave file descriptors.
    """
    import pty  # noqa: PLC0415 - the module exists only on POSIX hosts.

    return pty.openpty()


async def _connect_reader(master_fd: int) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    """Connect a pseudo-terminal's master end to the event loop.

    Args:
        master_fd: The master file descriptor of the pseudo-terminal.

    Returns:
        The reader that yields the child's output and the transport that
        owns the descriptor.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(master_fd, "rb", buffering=0)
    try:
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    return reader, transport


def _child_pids(pid: int) -> list[int]:
    """Return the pids of one process's direct children.

    Args:
        pid: The process whose children to read.

    Returns:
        The children's pids, or an empty list when the host does not publish
        them.
    """
    try:
        record = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    except (OSError, ValueError):
        return []
    return [int(part) for part in record.split() if part.isdigit()]


def _terminal_foreground_pgid(pid: int) -> int | None:
    """Read the foreground process group of one process's terminal.

    Args:
        pid: The process whose terminal to read.

    Returns:
        The terminal's foreground process group, or None when the host does
        not publish it.
    """
    try:
        record = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    fields = record.rsplit(")", 1)[-1].split()
    if len(fields) < 6:
        return None
    try:
        return int(fields[5])
    except ValueError:
        return None


def _read_syscall(pid: int) -> tuple[int, list[int]] | None:
    """Read the system call one process is blocked in.

    Args:
        pid: The process to inspect.

    Returns:
        The system call's number and its arguments, or None when the process
        is running or the host does not publish the record.
    """
    try:
        record = Path(f"/proc/{pid}/syscall").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    if not record or record == "running" or record.startswith("-1 "):
        return None
    fields = record.split()
    try:
        number = int(fields[0])
        arguments = [int(field, 16) for field in fields[1:7]]
    except ValueError:
        return None
    return number, arguments


def _fd_set_has_stdin(pid: int, address: int) -> bool:
    """Report whether a select call watches the process's standard input.

    Args:
        pid: The process that made the call.
        address: The address of the descriptor set.

    Returns:
        True when the set holds the standard input.
    """
    if address == 0:
        return False
    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            data = os.pread(memory.fileno(), 8, address)
    except OSError:
        return False
    if len(data) < 8:
        return False
    return int.from_bytes(data[:8], "little") % 2 == 1


def _poll_has_stdin(pid: int, address: int, count: int) -> bool:
    """Report whether a poll call watches the process's standard input.

    Args:
        pid: The process that made the call.
        address: The address of the descriptor array.
        count: The number of descriptors in the array.

    Returns:
        True when the array holds the standard input.
    """
    if address == 0 or count <= 0:
        return False
    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            data = os.pread(memory.fileno(), min(count, 1024) * 8, address)
    except OSError:
        return False
    for offset in range(0, len(data) - 7, 8):
        descriptor = int.from_bytes(data[offset : offset + 4], "little", signed=True)
        events = int.from_bytes(data[offset + 4 : offset + 8], "little", signed=True)
        if descriptor == 0 and events & 0x001:
            return True
    return False


def _epoll_has_stdin(pid: int, epfd: int) -> bool:
    """Report whether an epoll call watches the process's standard input.

    Args:
        pid: The process that made the call.
        epfd: The descriptor of the epoll set.

    Returns:
        True when the set holds the standard input.
    """
    try:
        record = Path(f"/proc/{pid}/fdinfo/{epfd}").read_text(encoding="ascii")
    except OSError:
        return False
    return any(re.match(r"^tfd:\s+0\b", line.strip()) is not None for line in record.splitlines())


def _waits_for_stdin(pid: int) -> bool:
    """Report whether one process is blocked reading its standard input.

    A process that waits for terminal input blocks in a system call that
    watches its standard input.  The kernel publishes the call in
    ``/proc/<pid>/syscall``, and the descriptors it watches are readable
    from the process's memory.

    Args:
        pid: The process to inspect.

    Returns:
        True when the process waits for its standard input.
    """
    blocked = _read_syscall(pid)
    if blocked is None:
        return False
    number, arguments = blocked
    first = arguments[0] if len(arguments) > 0 else 0
    second = arguments[1] if len(arguments) > 1 else 0
    third = arguments[2] if len(arguments) > 2 else 0
    for table in SYSCALL_TABLES.values():
        if number == table["read"]:
            return first == 0
        if number == table.get("select") or number == table.get("pselect"):
            return first >= 1 and _fd_set_has_stdin(pid, second)
        if number == table.get("poll") or number == table.get("ppoll"):
            return second >= 1 and _poll_has_stdin(pid, first, second)
        if number == table.get("epoll_wait") or number == table.get("epoll_pwait"):
            return third >= 1 and _epoll_has_stdin(pid, first)
    return False


@dataclass(slots=True)
class TerminalSession:
    """One live terminal session."""

    #: The session's opaque identifier.
    id: str
    #: The tenant that owns the session.
    tenant_id: uuid.UUID
    #: The user that owns the session.
    user_id: uuid.UUID
    #: The conversation that owns the session.
    conversation_id: uuid.UUID
    #: The shell kind the session runs.
    shell: ShellKind
    #: The directory the session runs in.
    cwd: Path
    #: The child process the session drives.
    process: ManagedProcess
    #: The wall-clock time the session was opened.
    created_at: datetime
    #: The wall-clock time of the session's last use.
    last_used_at: datetime
    #: Whether the child runs behind a pseudo-terminal.
    pty: bool

    def owned_by(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Report whether one caller owns the session.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            True when the session belongs to the caller.
        """
        return (
            self.tenant_id == tenant_id
            and self.user_id == user_id
            and self.conversation_id == conversation_id
        )

    def idle_seconds(self) -> float:
        """Return the time since the session was last used.

        Returns:
            The idle time in seconds.
        """
        return max(0.0, (datetime.now(UTC) - self.last_used_at).total_seconds())


@dataclass(frozen=True, slots=True)
class TerminalRead:
    """The result of one read of a terminal session's output."""

    #: The buffered output the read returned.
    output: str
    #: Whether output was left in the buffer after the read.
    truncated: bool
    #: The number of bytes dropped because the buffer reached its cap.
    dropped_bytes: int
    #: Whether the child is still running.
    alive: bool


@dataclass(frozen=True, slots=True)
class WaitObservation:
    """What one observation of a session's foreground state found."""

    #: Whether the child's process is still running.
    alive: bool
    #: Whether the session is blocked waiting for input.
    waiting_for_input: bool
    #: Whether the session produced output since the write.
    produced_output: bool
    #: The number of observations in a row that found the session waiting.
    waiting_streak: int
    #: The seconds since the write was sent.
    elapsed_seconds: float
    #: The seconds since the session last produced output.
    idle_seconds: float


def classify_wait(
    observation: WaitObservation,
    *,
    idle_window_seconds: float,
    budget_seconds: float,
) -> WaitReason | None:
    """Return the reason a write settled with, or None while it keeps waiting.

    The reasons are reported in the order of the evidence behind them.  A
    session whose process is gone settles first, because no other reason
    applies to it.  A session that is blocked waiting for input settles next,
    because the kernel records that state exactly.  A session that went quiet
    settles next, because silence only suggests that the command finished.  A
    write that ran out of time settles last.

    Args:
        observation: What one observation of the session found.
        idle_window_seconds: The silence after which the session counts as
            idle.
        budget_seconds: The longest the write may wait.

    Returns:
        The reason the write settled with, or None while it keeps waiting.
    """
    if not observation.alive:
        return "session_exit"
    if (
        observation.waiting_for_input
        and observation.produced_output
        and observation.waiting_streak >= WAIT_CONFIRMATIONS
        and observation.elapsed_seconds >= WAIT_SETTLE_SECONDS
    ):
        return "stdin_read"
    if observation.idle_seconds >= idle_window_seconds:
        return "inferred_idle"
    if observation.elapsed_seconds >= budget_seconds:
        return "timeout"
    return None


async def _wait_for_reason(
    process: ManagedProcess,
    *,
    budget_seconds: float,
    idle_window_seconds: float = DEFAULT_IDLE_WINDOW_SECONDS,
) -> WaitReason:
    """Wait until a session reports why it stopped.

    The wait observes the session rather than the write: the session's
    process, its foreground state, and its output.  A session that is asked
    for more input is reported as such, a session that goes quiet is reported
    as probably finished, and a session that runs past the budget is reported
    as timed out.

    Args:
        process: The session's process.
        budget_seconds: The longest the wait may last.
        idle_window_seconds: The silence after which the session counts as
            idle.

    Returns:
        The reason the session settled with.
    """
    started = time.monotonic()
    bytes_at_start = process.produced_bytes
    last_output = started
    streak = 0
    while True:
        now = time.monotonic()
        alive = process.is_running
        produced = process.produced_bytes > bytes_at_start
        if produced:
            last_output = max(last_output, process.last_output_at)
        waiting = alive and process.waits_for_input()
        streak = streak + 1 if waiting else 0
        reason = classify_wait(
            WaitObservation(
                alive=alive,
                waiting_for_input=waiting,
                produced_output=produced,
                waiting_streak=streak,
                elapsed_seconds=now - started,
                idle_seconds=now - last_output,
            ),
            idle_window_seconds=idle_window_seconds,
            budget_seconds=budget_seconds,
        )
        if reason is not None:
            return reason
        await asyncio.sleep(WAIT_POLL_INTERVAL_SECONDS)


#: The live sessions of one conversation.
type TerminalSessionList = list[TerminalSession]


@dataclass(frozen=True, slots=True)
class TerminalJob:
    """One background write to a terminal session.

    A job has the same fields the conversation's other background work
    reports, so one reader can present both, plus the fields that describe
    what the write did to the session.
    """

    #: The job's identifier.
    job_id: str
    #: The session the job writes to.
    session_id: str
    #: The tenant that owns the job.
    tenant_id: uuid.UUID
    #: The user that owns the job.
    user_id: uuid.UUID
    #: The conversation that owns the job.
    conversation_id: uuid.UUID
    #: The job's status: running, completed, or failed.
    status: str
    #: The number of bytes the write sent.
    written_bytes: int | None
    #: The reason the write settled with.
    wait_reason: str | None
    #: The failure the job ran into.
    error: str | None
    #: The wall-clock time the job was created.
    created_at: datetime
    #: The wall-clock time the job started.
    started_at: datetime | None
    #: The wall-clock time the job ended.
    completed_at: datetime | None

    def owned_by(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Report whether one caller owns the job.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            True when the job belongs to the caller.
        """
        return (
            self.tenant_id == tenant_id
            and self.user_id == user_id
            and self.conversation_id == conversation_id
        )


#: The background writes of one conversation.
type TerminalJobList = list[TerminalJob]


class TerminalJobStore:
    """The background terminal writes one deployment is running.

    The store owns the jobs and the tasks that run them, so a job outlives
    the tool call that started it and its task is not collected while it
    runs.
    """

    def __init__(self) -> None:
        """Create a store without jobs."""
        self._jobs: dict[str, TerminalJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def create(
        self,
        *,
        session_id: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalJob:
        """Record a new job and return it.

        Args:
            session_id: The session the job writes to.
            tenant_id: The tenant that owns the job.
            user_id: The user that owns the job.
            conversation_id: The conversation that owns the job.

        Returns:
            The new job.
        """
        now = datetime.now(UTC)
        job = TerminalJob(
            job_id=uuid.uuid4().hex,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            status="running",
            written_bytes=None,
            wait_reason=None,
            error=None,
            created_at=now,
            started_at=now,
            completed_at=None,
        )
        self._jobs[job.job_id] = job
        return job

    def track(self, task: asyncio.Task[None]) -> None:
        """Keep a running task alive until it ends.

        Args:
            task: The task that runs a job.
        """
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def complete(self, job_id: str, *, written_bytes: int, wait_reason: str) -> None:
        """Record the outcome of a job that finished.

        Args:
            job_id: The job's identifier.
            written_bytes: The number of bytes the write sent.
            wait_reason: The reason the write settled with.
        """
        self._update(
            job_id,
            status="completed",
            written_bytes=written_bytes,
            wait_reason=wait_reason,
            completed_at=datetime.now(UTC),
        )

    def fail(self, job_id: str, *, error: str) -> None:
        """Record the failure of a job.

        Args:
            job_id: The job's identifier.
            error: The failure the job ran into.
        """
        self._update(job_id, status="failed", error=error, completed_at=datetime.now(UTC))

    def get(
        self,
        job_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalJob | None:
        """Return one job the caller owns.

        Args:
            job_id: The job's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The job, or None when no job with that id belongs to the caller.
        """
        job = self._jobs.get(str(job_id))
        if job is None or not job.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return None
        return job

    def list(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalJobList:
        """Return the caller's jobs, oldest first.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The caller's jobs.
        """
        return sorted(
            (
                job
                for job in self._jobs.values()
                if job.owned_by(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            ),
            key=lambda job: job.created_at,
        )

    async def cancel_all(self) -> None:
        """Stop every task that runs a job."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def _update(self, job_id: str, **changes: Any) -> None:
        """Replace one job's fields.

        Args:
            job_id: The job's identifier.
            changes: The fields to replace.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        self._jobs[job_id] = replace(job, **changes)


class TerminalManager:
    """The registry of live terminal sessions.

    The manager bounds the sessions one deployment may hold and the sessions
    one conversation may hold, sweeps the sessions that went idle, and answers
    every request for a session only to the tenant, user, and conversation
    that own it.
    """

    def __init__(
        self,
        *,
        max_sessions_per_conversation: int = DEFAULT_MAX_SESSIONS_PER_CONVERSATION,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        """Create the manager.

        Args:
            max_sessions_per_conversation: The number of live sessions one
                conversation may hold.
            max_sessions: The number of live sessions the deployment may
                hold.
            idle_timeout_seconds: The idle time after which a session is
                swept.
        """
        self._sessions: dict[str, TerminalSession] = {}
        self._jobs = TerminalJobStore()
        self.max_sessions_per_conversation = max(1, int(max_sessions_per_conversation))
        self.max_sessions = max(1, int(max_sessions))
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))

    async def open(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        settings: Any,
        shell: ShellKind = "bash",
        cwd: str | None = None,
        command: str | None = None,
        workspace_root: Path | None = None,
    ) -> TerminalSession:
        """Open one terminal session.

        Args:
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            settings: The deployment settings in force.
            shell: The shell kind to run.
            cwd: The workspace-relative directory to run in.
            command: The command to run at startup, or None for an
                interactive shell.
            workspace_root: The workspace the session is confined to.  The
                caller passes the workspace the conversation works in; without
                one the session starts in the account's default workspace.

        Returns:
            The live session.

        Raises:
            TerminalLimitError: When a session limit is reached.
            WorkspacePathError: When the working directory leaves the
                workspace.
            WorkspaceUnavailableError: When the deployment has no workspace.
            ShellUnavailableError: When the host has no such shell.
        """
        await self.sweep()
        max_per_conversation = _setting_int(
            settings,
            "deepspace_terminal_max_sessions_per_conversation",
            self.max_sessions_per_conversation,
        )
        max_total = _setting_int(settings, "deepspace_terminal_max_sessions", self.max_sessions)
        idle_timeout = _setting_float(
            settings,
            "deepspace_terminal_idle_timeout_seconds",
            self.idle_timeout_seconds,
        )
        max_buffer_bytes = _setting_int(
            settings,
            "deepspace_terminal_max_output_bytes",
            DEFAULT_MAX_BUFFER_BYTES,
        )
        live = self.list(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        if len(live) >= max_per_conversation:
            raise TerminalLimitError(
                f"This conversation already holds {len(live)} live terminal sessions; "
                "close one before opening another."
            )
        if len(self._sessions) >= max_total:
            raise TerminalLimitError(
                "This deployment already holds its maximum number of live terminal sessions."
            )
        workspace = (
            workspace_root
            if workspace_root is not None
            else workspace_root_for(settings, tenant_id, user_id)
        )
        working_dir = _resolve_working_directory(workspace, cwd)
        argv = _build_argv(shell, command)
        env = build_session_environment(
            workspace=workspace,
            shell=shell,
            mode=_sandbox_mode(settings),
        )
        process = await ManagedProcess.start(
            argv=argv,
            shell=shell,
            cwd=working_dir,
            env=env,
            max_buffer_bytes=max_buffer_bytes,
            idle_timeout_seconds=idle_timeout,
        )
        now = datetime.now(UTC)
        session = TerminalSession(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            shell=shell,
            cwd=working_dir,
            process=process,
            created_at=now,
            last_used_at=now,
            pty=process.pty,
        )
        self._sessions[session.id] = session
        return session

    def get(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalSession:
        """Return one session the caller owns.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The live session.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise TerminalNotFoundError("No such terminal session.")
        return session

    def list(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalSessionList:
        """Return the live sessions of one conversation.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The conversation's live sessions, oldest first.
        """
        return sorted(
            (
                session
                for session in self._sessions.values()
                if session.owned_by(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            ),
            key=lambda session: session.created_at,
        )

    async def read(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> TerminalRead:
        """Read the buffered output of one session.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            max_bytes: The largest number of bytes to return.

        Returns:
            The output and the state of the session.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        session.process.touch()
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
        output = session.process.read(limit)
        return TerminalRead(
            output=output,
            truncated=session.process.pending_bytes > 0,
            dropped_bytes=session.process.buffer.dropped_bytes,
            alive=session.process.is_alive,
        )

    def subscribe(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> tuple[TerminalSession, str, asyncio.Queue[bytes | None]]:
        """Subscribe to one owned session's live output.

        The buffered output is consumed before the subscription starts, so a
        WebSocket receives each byte exactly once even when the shell prints
        between the REST open request and the stream handshake.

        Args:
            session_id: The session to stream.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The conversation that owns the session.

        Returns:
            The session, buffered output, and live output queue.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        session.process.touch()
        initial_output = session.process.read(DEFAULT_MAX_BUFFER_BYTES)
        queue = session.process.subscribe()
        return session, initial_output, queue

    def unsubscribe(self, session: TerminalSession, queue: asyncio.Queue[bytes | None]) -> None:
        """Stop one live output subscription.

        Args:
            session: The session that owns the subscription.
            queue: The queue returned by :meth:`subscribe`.
        """
        session.process.unsubscribe(queue)

    async def resize(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        columns: int,
        rows: int,
    ) -> None:
        """Resize one owned terminal session.

        Args:
            session_id: The session to resize.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The conversation that owns the session.
            columns: The number of terminal columns.
            rows: The number of terminal rows.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        session.process.resize(columns=columns, rows=rows)
        session.process.touch()

    async def write(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        data: str,
    ) -> int:
        """Send input to one session.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            data: The text to send.

        Returns:
            The number of bytes sent.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
            TerminalError: When the session has no usable input stream.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        written = await session.process.write(data)
        session.process.touch()
        return written

    async def wait_for_reason(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        wait_seconds: int = 0,
    ) -> WaitReason:
        """Report why one session settled after a write.

        Without a wait the session has nothing to report, so the reason is
        ``sent`` unless the session's process is already gone.  A wait
        observes the session until it asks for more input, goes quiet, runs
        out of time, or ends.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            wait_seconds: How long to wait for the session to settle.

        Returns:
            The reason the session settled with.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if wait_seconds <= 0:
            return "sent" if session.process.is_running else "session_exit"
        return await _wait_for_reason(
            session.process,
            budget_seconds=float(wait_seconds),
        )

    async def signal(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        sig: int,
    ) -> bool:
        """Send a signal to one session's foreground process group.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            sig: The signal to send.

        Returns:
            True when the signal reached the process group.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        delivered = session.process.signal(sig)
        session.process.touch()
        return delivered

    async def start_background_write(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        data: str,
    ) -> TerminalJob:
        """Hand a write to a background job and return the job.

        The job sends the input and waits for the session to settle, so a
        write that takes minutes does not hold the turn that started it.

        Args:
            session_id: The session's identifier.
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            data: The text to send.

        Returns:
            The job that runs the write.

        Raises:
            TerminalNotFoundError: When no session with that id belongs to
                the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        job = self._jobs.create(
            session_id=session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        task = asyncio.create_task(
            self._run_background_write(
                job.job_id,
                session_id=session.id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                data=data,
            )
        )
        self._jobs.track(task)
        return job

    def job(
        self,
        job_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalJob | None:
        """Return one background write the caller owns.

        Args:
            job_id: The job's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The job, or None when no job with that id belongs to the caller.
        """
        return self._jobs.get(
            job_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    def jobs(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TerminalJobList:
        """Return the caller's background writes, oldest first.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The caller's background writes.
        """
        return self._jobs.list(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    async def _run_background_write(
        self,
        job_id: str,
        *,
        session_id: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        data: str,
    ) -> None:
        """Run one background write and record its outcome.

        Args:
            job_id: The job's identifier.
            session_id: The session the write goes to.
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            data: The text to send.
        """
        try:
            written = await self.write(
                session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                data=data,
            )
            reason = await self.wait_for_reason(
                session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                wait_seconds=MAX_WAIT_SECONDS,
            )
        except TerminalError as exc:
            self._jobs.fail(job_id, error=str(exc))
            return
        self._jobs.complete(job_id, written_bytes=written, wait_reason=reason)

    async def close(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Close one session and forget it.

        Closing a session that is already gone is not an error.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            True when a session was closed.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return False
        await self._shutdown(session)
        return True

    async def close_all_for_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> int:
        """Close every live session of one conversation.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The number of sessions that were closed.
        """
        sessions = self.list(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        for session in sessions:
            await self._shutdown(session)
        return len(sessions)

    async def close_all(self) -> int:
        """Close every live session of the deployment.

        Returns:
            The number of sessions that were closed.
        """
        await self._jobs.cancel_all()
        sessions = list(self._sessions.values())
        for session in sessions:
            await self._shutdown(session)
        return len(sessions)

    async def sweep(self) -> Sequence[str]:
        """Close the sessions that exceeded their idle lifetime.

        Returns:
            The identifiers of the sessions that were closed.
        """
        now = time.monotonic()
        expired = [
            session for session in self._sessions.values() if session.process.idle_deadline <= now
        ]
        for session in expired:
            await self._shutdown(session)
        return [session.id for session in expired]

    async def _shutdown(self, session: TerminalSession) -> None:
        """End one session's child and forget the session.

        Args:
            session: The session to close.
        """
        self._sessions.pop(session.id, None)
        await session.process.close()


def _resolve_working_directory(workspace: Path, raw_cwd: str | None) -> Path:
    """Resolve the directory a session runs in.

    Args:
        workspace: The workspace root the session is confined to.
        raw_cwd: The workspace-relative directory, or None for the root.

    Returns:
        The absolute working directory.

    Raises:
        WorkspacePathError: When the directory leaves the workspace.
        TerminalError: When the directory cannot be created.
    """
    if not raw_cwd:
        return workspace
    resolved = resolve_within_root(workspace, raw_cwd)
    try:
        resolved.absolute.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TerminalError(
            "The terminal session's working directory could not be created."
        ) from exc
    return resolved.absolute


_MANAGER: TerminalManager | None = None


def terminal_manager() -> TerminalManager:
    """Return the process-wide terminal manager.

    Returns:
        The manager that owns this deployment's live terminal sessions.
    """
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TerminalManager()
    return _MANAGER


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_WINDOW_SECONDS",
    "DEFAULT_MAX_BUFFER_BYTES",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_MAX_SESSIONS_PER_CONVERSATION",
    "DEFAULT_READ_BYTES",
    "MAX_READ_BYTES",
    "MAX_WAIT_SECONDS",
    "SHELL_KINDS",
    "SUPPORTED_SIGNALS",
    "ManagedProcess",
    "OutputBuffer",
    "ShellKind",
    "ShellUnavailableError",
    "TerminalError",
    "TerminalJob",
    "TerminalJobStore",
    "TerminalLimitError",
    "TerminalManager",
    "TerminalNotFoundError",
    "TerminalRead",
    "TerminalSession",
    "WaitObservation",
    "WaitReason",
    "available_shells",
    "build_session_environment",
    "classify_wait",
    "default_shell",
    "resolve_shell",
    "signal_number",
    "terminal_manager",
]
