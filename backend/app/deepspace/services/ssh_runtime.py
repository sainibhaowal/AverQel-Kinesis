"""The remote runtime: the whole tool surface on a reviewed remote host.

The command runner reaches a host and returns what a command printed.  The
remote runtime adds the rest of the surface an agent needs on a machine the
deployment does not own: a file system it can read, write, list, and remove
files in, a terminal it can keep open and talk to, and a command whose
output arrives while the command runs.

The rules the command runner already holds carry over and are not repeated
here.  The connection checks the host key against the reviewed entry, the
private key never leaves the server, and the client process receives the
reduced environment, so a credential held in the server's environment is
not readable from the connection.

What the runtime adds is a boundary.  Every path a caller names is
resolved *on the host*, inside the host's reviewed remote directory, and a
path that leaves that directory is refused.  The resolution happens on the
host because only the host can see the links: a link inside the directory
that points outside it is followed there and refused there, not here.

The remote terminal is a registry of its own.  It does not share a session
id with a local terminal session, it belongs to the tenant, user, and
conversation that opened it, and it is closed when it goes idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.deepspace.services.ssh import (
    SshExecutor,
    SshHost,
    SshResult,
    SshTrust,
    build_remote_command,
    build_ssh_argv,
    prepare_trust,
    release_trust,
    remote_environment,
)
from app.deepspace.services.terminals import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_MAX_SESSIONS_PER_CONVERSATION,
    DEFAULT_READ_BYTES,
    MAX_READ_BYTES,
    OutputBuffer,
)
from app.deepspace.services.workspace_root import WorkspacePathError, normalize_relative_path

#: The exit code the remote command uses when a path leaves the reviewed directory.
REMOTE_PATH_REFUSED = 4

#: The exit code the remote command uses when the path does not exist.
REMOTE_NOT_FOUND = 5

#: The exit code the remote command uses when the path is not the kind the call needs.
REMOTE_WRONG_KIND = 6

#: The exit code the remote command uses when the reviewed directory is not there.
REMOTE_NO_WORKSPACE = 7

#: The prefix the remote command marks the lines it reports facts on.
META_PREFIX = "DEEPSPACE_META "

#: The largest file one write may send, in bytes.
MAX_WRITE_BYTES = 1_000_000

#: The prefix every remote terminal session id carries.
TERMINAL_SESSION_PREFIX = "sshterm-"

#: The prefix every streaming command session id carries.
STREAM_SESSION_PREFIX = "sshstream-"

#: The signals a caller may send to a remote terminal session.
SUPPORTED_REMOTE_SIGNALS: tuple[str, ...] = (
    "SIGINT",
    "SIGTERM",
    "SIGHUP",
    "SIGQUIT",
    "SIGKILL",
    "SIGTSTP",
)

#: The control characters a remote terminal's line discipline turns into a signal.
TERMINAL_SIGNAL_CHARACTERS: dict[str, bytes] = {
    "SIGINT": b"\x03",
    "SIGQUIT": b"\x1c",
    "SIGTSTP": b"\x1a",
}

#: The signals that end a remote session by closing the connection.
TERMINATING_SIGNALS: frozenset[str] = frozenset({"SIGTERM", "SIGHUP", "SIGKILL"})

#: The shell prelude that resolves and checks a path on the host.
#:
#: The prelude is plain POSIX shell.  It walks the requested path one
#: segment at a time from the reviewed directory, follows every link it
#: meets, and prints the physical path it reached.  The path is accepted
#: only when that physical path is the reviewed directory or lies under it,
#: so a link that points out of the directory is refused on the host.
_RESOLVER = """
ds_norm() {
  _ds_out=
  _ds_rest=$1
  while [ -n "$_ds_rest" ]; do
    _ds_seg=${_ds_rest%%/*}
    if [ "$_ds_rest" = "$_ds_seg" ]; then _ds_rest=""; else _ds_rest=${_ds_rest#*/}; fi
    case "$_ds_seg" in
      ''|.) continue ;;
      ..) _ds_out=${_ds_out%/*}; [ -n "$_ds_out" ] || _ds_out=/ ;;
      *) _ds_out=$_ds_out/$_ds_seg ;;
    esac
  done
  [ -n "$_ds_out" ] || _ds_out=/
  printf '%s\\n' "$_ds_out"
}
ds_root() {
  _ds_r=$(cd -P "$DS_ROOT" 2>/dev/null && pwd -P) || return 1
  printf '%s\\n' "$_ds_r"
}
ds_phys() {
  _ds_p=$1
  _ds_n=0
  while [ "$_ds_n" -lt 32 ]; do
    _ds_n=$((_ds_n + 1))
    _ds_d=${_ds_p%/*}
    [ -n "$_ds_d" ] || _ds_d=/
    _ds_b=${_ds_p##*/}
    [ -n "$_ds_b" ] || _ds_b=/
    if [ -L "$_ds_d/$_ds_b" ]; then
      _ds_t=$(readlink "$_ds_d/$_ds_b" 2>/dev/null) || return 1
      case "$_ds_t" in
        /*) _ds_p=$_ds_t ;;
        *) _ds_p=$(ds_norm "$_ds_d/$_ds_t") ;;
      esac
      continue
    fi
    _ds_pd=$(cd -P "$_ds_d" 2>/dev/null && pwd -P) || return 1
    printf '%s\\n' "$_ds_pd/$_ds_b"
    return 0
  done
  return 1
}
ds_resolve() {
  _ds_root=$(ds_root) || return 7
  _ds_cur=$_ds_root
  _ds_rest=$1
  while [ -n "$_ds_rest" ]; do
    _ds_seg=${_ds_rest%%/*}
    if [ "$_ds_rest" = "$_ds_seg" ]; then _ds_rest=""; else _ds_rest=${_ds_rest#*/}; fi
    [ -n "$_ds_seg" ] || continue
    case "$_ds_seg" in
      .|..) return 4 ;;
    esac
    _ds_next=$_ds_cur/$_ds_seg
    if [ -L "$_ds_next" ]; then
      _ds_next=$(ds_phys "$_ds_next") || return 1
    fi
    _ds_cur=$_ds_next
  done
  case "$_ds_cur/" in
    "$_ds_root"/*) printf '%s\\n' "$_ds_cur" ;;
    *) return 4 ;;
  esac
}
ds_resolve_mkdir() {
  _ds_root=$(ds_root) || return 7
  _ds_cur=$_ds_root
  _ds_rest=$1
  while [ -n "$_ds_rest" ]; do
    _ds_seg=${_ds_rest%%/*}
    if [ "$_ds_rest" = "$_ds_seg" ]; then _ds_rest=""; else _ds_rest=${_ds_rest#*/}; fi
    [ -n "$_ds_seg" ] || continue
    case "$_ds_seg" in
      .|..) return 4 ;;
    esac
    _ds_next=$_ds_cur/$_ds_seg
    if [ -L "$_ds_next" ]; then
      _ds_next=$(ds_phys "$_ds_next") || return 1
    elif [ ! -d "$_ds_next" ]; then
      mkdir "$_ds_next" || return 1
    fi
    _ds_cur=$_ds_next
    case "$_ds_cur/" in
      "$_ds_root"/*) : ;;
      *) return 4 ;;
    esac
  done
  printf '%s\\n' "$_ds_cur"
}
"""


class SshRuntimeError(RuntimeError):
    """A remote runtime operation failed."""


class RemotePathRefusedError(SshRuntimeError):
    """The path leaves the host's reviewed remote directory."""


class RemoteNotFoundError(SshRuntimeError):
    """The path does not exist on the host."""


class SshTerminalNotFoundError(SshRuntimeError):
    """No live remote session with that id belongs to the caller."""


def key_directory_for(settings: Any) -> Path:
    """Return the directory the deployment keeps its private keys in.

    Args:
        settings: The deployment settings in force.

    Returns:
        The directory.
    """
    configured = str(getattr(settings, "deepspace_ssh_key_directory", "") or "")
    if configured:
        return Path(configured).expanduser()
    from app.core.averqel_home import get_averqel_home

    return Path(get_averqel_home()) / "ssh"


def remote_workspace_for(host: SshHost) -> str:
    """Return the host's reviewed remote directory.

    A host without a reviewed directory has no boundary a path can be
    confined to, so its files are not reachable.

    Args:
        host: The host to reach.

    Returns:
        The reviewed remote directory.

    Raises:
        RemotePathRefusedError: When the host has no reviewed directory.
    """
    workspace = str(host.remote_workspace or "").strip()
    if not workspace:
        raise RemotePathRefusedError(
            "This SSH host has no reviewed remote directory, so its files cannot be reached."
        )
    return workspace


def normalize_remote_path(raw: str) -> str:
    """Return the remote-directory-relative form of one path.

    The path is relative to the host's reviewed remote directory.  A path
    that is absolute, or that names a parent directory, is refused before
    anything reaches the host.  The directory itself is named by an empty
    relative path.

    Args:
        raw: The path the model supplied.

    Returns:
        The normalized POSIX-style relative path, empty for the reviewed
        directory itself.

    Raises:
        RemotePathRefusedError: When the path is malformed or absolute.
    """
    if str(raw or "").strip() in {".", "./"}:
        return ""
    try:
        return normalize_relative_path(raw)
    except WorkspacePathError as exc:
        raise RemotePathRefusedError(str(exc)) from exc


def resolver_prelude(root: str) -> str:
    """Return the shell prelude that confines a path on the host.

    Args:
        root: The host's reviewed remote directory.

    Returns:
        The prelude, with the directory bound.
    """
    return f"DS_ROOT={shlex.quote(root)}\n{_RESOLVER}"


def build_read_command(host: SshHost, relative: str) -> str:
    """Build the command that reads one remote file.

    The command resolves the path on the host, refuses a path that leaves
    the reviewed directory, and then prints the file.  The file's size is
    reported on the error stream, so the content on the standard output
    stays exactly the file's bytes.

    Args:
        host: The host to reach.
        relative: The path relative to the reviewed remote directory.

    Returns:
        The remote command line.
    """
    return "\n".join(
        [
            resolver_prelude(remote_workspace_for(host)),
            f"_path=$(ds_resolve {shlex.quote(relative)}) || exit $?",
            'if [ -d "$_path" ]; then exit 6; fi',
            'if [ ! -f "$_path" ]; then exit 5; fi',
            '_size=$(wc -c < "$_path" | tr -d "[:space:]")',
            'printf \'DEEPSPACE_META size=%s path=%s\\n\' "$_size" "$_path" >&2',
            'cat "$_path"',
        ]
    )


def build_write_command(host: SshHost, relative: str) -> str:
    """Build the command that writes one remote file.

    The command resolves the path on the host, creates the directories the
    file needs, and then writes the content it receives on its standard
    input to a temporary name in the same directory and renames it over the
    target.  The rename is atomic, so a reader never sees half a file.

    Args:
        host: The host to reach.
        relative: The path relative to the reviewed remote directory.

    Returns:
        The remote command line.
    """
    parent, _, base = relative.rpartition("/")
    return "\n".join(
        [
            resolver_prelude(remote_workspace_for(host)),
            f"_dir=$(ds_resolve_mkdir {shlex.quote(parent)}) || exit $?",
            f"_base={shlex.quote(base)}",
            '[ -n "$_base" ] || exit 5',
            '_target=$(ds_phys "$_dir/$_base") || exit 1',
            "_root=$(ds_root) || exit 1",
            'if [ -d "$_target" ]; then exit 6; fi',
            'case "$_target/" in',
            '  "$_root"/*) : ;;',
            "  *) exit 4 ;;",
            "esac",
            '_tmp="$_dir/.deepspace-tmp.$$"',
            "trap 'rm -f \"$_tmp\"' 0",
            'cat > "$_tmp" || exit 1',
            'mv -f "$_tmp" "$_target" || exit 1',
            '_size=$(wc -c < "$_target" | tr -d "[:space:]")',
            'printf \'DEEPSPACE_META size=%s path=%s\\n\' "$_size" "$_target" >&2',
        ]
    )


def build_list_command(host: SshHost, relative: str) -> str:
    """Build the command that lists one remote directory.

    The command resolves the path on the host, refuses a path that leaves
    the reviewed directory, and then prints one line per entry: the kind of
    the entry, its size in bytes, and its name.  A name that holds a
    newline is reported without the rest of the name, because the listing
    is one entry per line.

    Args:
        host: The host to reach.
        relative: The path relative to the reviewed remote directory.

    Returns:
        The remote command line.
    """
    return "\n".join(
        [
            resolver_prelude(remote_workspace_for(host)),
            f"_path=$(ds_resolve {shlex.quote(relative)}) || exit $?",
            'if [ ! -d "$_path" ]; then exit 6; fi',
            "printf 'DEEPSPACE_META path=%s\\n' \"$_path\" >&2",
            'ls -1A "$_path" | while IFS= read -r _ds_name; do',
            '  _ds_entry="$_path/$_ds_name"',
            '  [ -e "$_ds_entry" ] || [ -L "$_ds_entry" ] || continue',
            '  if [ -L "$_ds_entry" ]; then',
            "    _ds_kind=symlink",
            "    _ds_size=0",
            '  elif [ -d "$_ds_entry" ]; then',
            "    _ds_kind=directory",
            "    _ds_size=0",
            '  elif [ -f "$_ds_entry" ]; then',
            "    _ds_kind=file",
            "    _ds_size=$(wc -c < \"$_ds_entry\" | tr -d '[:space:]')",
            "  else",
            "    _ds_kind=other",
            "    _ds_size=0",
            "  fi",
            '  printf \'%s\\t%s\\t%s\\n\' "$_ds_kind" "$_ds_size" "$_ds_name"',
            "done",
        ]
    )


def build_delete_command(host: SshHost, relative: str) -> str:
    """Build the command that removes one remote file.

    Args:
        host: The host to reach.
        relative: The path relative to the reviewed remote directory.

    Returns:
        The remote command line.
    """
    return "\n".join(
        [
            resolver_prelude(remote_workspace_for(host)),
            f"_path=$(ds_resolve {shlex.quote(relative)}) || exit $?",
            'if [ ! -e "$_path" ] && [ ! -L "$_path" ]; then exit 5; fi',
            'if [ -d "$_path" ]; then exit 6; fi',
            'rm -f "$_path" || exit 1',
            "printf 'DEEPSPACE_META path=%s\\n' \"$_path\" >&2",
        ]
    )


def build_terminal_command(host: SshHost) -> str:
    """Build the command that opens an interactive shell on the host.

    The shell runs in the host's reviewed remote directory and keeps the
    terminal the connection allocated, so a program that checks whether it
    has a terminal behaves the way it does for a person.

    Args:
        host: The host to reach.

    Returns:
        The remote command line.
    """
    remote_workspace_for(host)
    return build_remote_command(host, 'exec "${SHELL:-/bin/sh}"')


def parse_meta(stderr: str) -> dict[str, str]:
    """Return the facts the remote command reported.

    Args:
        stderr: The command's standard error.

    Returns:
        The reported fields, empty when the command reported none.
    """
    values: dict[str, str] = {}
    for line in stderr.splitlines():
        if not line.startswith(META_PREFIX):
            continue
        for pair in line[len(META_PREFIX) :].split(" "):
            key, _, value = pair.partition("=")
            if key:
                values[key] = value
    return values


def _parse_size(value: str) -> int:
    """Return the byte count one reported field carries.

    Args:
        value: The reported value.

    Returns:
        The byte count, or zero when the value is not a number.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _raise_for_exit(host: SshHost, result: SshResult) -> None:
    """Raise the failure one remote command reported.

    Args:
        host: The host the command ran on.
        result: The command's outcome.

    Raises:
        RemotePathRefusedError: When the path leaves the reviewed directory.
        RemoteNotFoundError: When the path does not exist.
        SshRuntimeError: When the command failed for another reason.
    """
    if result.exit_code == 0:
        return
    if result.exit_code == REMOTE_PATH_REFUSED:
        raise RemotePathRefusedError(
            "The requested path resolves outside the host's reviewed remote directory."
        )
    if result.exit_code == REMOTE_NOT_FOUND:
        raise RemoteNotFoundError("No such path on the host.")
    if result.exit_code == REMOTE_WRONG_KIND:
        raise SshRuntimeError("The path on the host is not the kind this call needs.")
    if result.exit_code == REMOTE_NO_WORKSPACE:
        raise SshRuntimeError("The host's reviewed remote directory could not be reached.")
    raise SshRuntimeError(
        f"The remote command on {host.name!r} failed with exit code {result.exit_code}."
    )


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One file on a remote host.

    Attributes:
        path: The path relative to the host's reviewed remote directory.
        content: The file's content.
        size_bytes: The file's size in bytes.
    """

    path: str
    content: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    """One entry of a remote directory.

    Attributes:
        name: The entry's name.
        kind: The entry's kind: file, directory, symlink, or other.
        size_bytes: The entry's size in bytes.
    """

    name: str
    kind: str
    size_bytes: int


class SshRuntime:
    """The remote file surface of one reviewed host."""

    def __init__(self, *, key_directory: Path) -> None:
        """Create the runtime.

        Args:
            key_directory: The directory the deployment keeps its private
                keys in.
        """
        self.key_directory = key_directory
        self._executor = SshExecutor(key_directory=key_directory)

    async def read_file(self, host: SshHost, path: str) -> RemoteFile:
        """Read one file on a host.

        Args:
            host: The host to reach.
            path: The path relative to the host's reviewed remote directory.

        Returns:
            The file.

        Raises:
            RemotePathRefusedError: When the path leaves the reviewed
                directory.
            RemoteNotFoundError: When the file does not exist.
            SshRuntimeError: When the read failed.
        """
        relative = normalize_remote_path(path)
        result = await self._executor.run_prepared(
            host,
            command=build_read_command(host, relative),
        )
        _raise_for_exit(host, result)
        meta = parse_meta(result.stderr)
        return RemoteFile(
            path=relative or ".",
            content=result.stdout,
            size_bytes=_parse_size(meta.get("size", "")),
        )

    async def write_file(self, host: SshHost, path: str, content: str) -> RemoteFile:
        """Write one file on a host.

        The file is written to a temporary name and renamed over the
        target, so a reader never sees half a file.

        Args:
            host: The host to reach.
            path: The path relative to the host's reviewed remote directory.
            content: The text the file holds.

        Returns:
            The file that was written.

        Raises:
            RemotePathRefusedError: When the path leaves the reviewed
                directory.
            SshRuntimeError: When the write failed.
        """
        relative = normalize_remote_path(path)
        if not relative:
            raise SshRuntimeError("A path is required.")
        payload = content.encode("utf-8")
        if len(payload) > MAX_WRITE_BYTES:
            raise SshRuntimeError(f"A remote file may be at most {MAX_WRITE_BYTES} bytes.")
        result = await self._executor.run_prepared(
            host,
            command=build_write_command(host, relative),
            stdin=payload,
        )
        _raise_for_exit(host, result)
        meta = parse_meta(result.stderr)
        return RemoteFile(
            path=relative,
            content="",
            size_bytes=_parse_size(meta.get("size", "")) or len(payload),
        )

    async def list_directory(self, host: SshHost, path: str) -> tuple[str, list[RemoteEntry]]:
        """List one directory on a host.

        Args:
            host: The host to reach.
            path: The path relative to the host's reviewed remote directory.

        Returns:
            The directory's relative path and its entries.

        Raises:
            RemotePathRefusedError: When the path leaves the reviewed
                directory.
            RemoteNotFoundError: When the directory does not exist.
            SshRuntimeError: When the listing failed.
        """
        relative = normalize_remote_path(path)
        result = await self._executor.run_prepared(
            host,
            command=build_list_command(host, relative),
        )
        _raise_for_exit(host, result)
        entries: list[RemoteEntry] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            kind, _, rest = line.partition("\t")
            size_text, _, name = rest.partition("\t")
            entries.append(
                RemoteEntry(
                    name=name,
                    kind=kind,
                    size_bytes=_parse_size(size_text),
                )
            )
        return relative or ".", entries

    async def delete_file(self, host: SshHost, path: str) -> str:
        """Remove one file on a host.

        Args:
            host: The host to reach.
            path: The path relative to the host's reviewed remote directory.

        Returns:
            The relative path of the file that was removed.

        Raises:
            RemotePathRefusedError: When the path leaves the reviewed
                directory.
            RemoteNotFoundError: When the file does not exist.
            SshRuntimeError: When the removal failed.
        """
        relative = normalize_remote_path(path)
        if not relative:
            raise SshRuntimeError("A path is required.")
        result = await self._executor.run_prepared(
            host,
            command=build_delete_command(host, relative),
        )
        _raise_for_exit(host, result)
        return relative


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    buffer: OutputBuffer,
    wakeup: asyncio.Event | None = None,
) -> None:
    """Keep one buffer filled with what a stream produced.

    Args:
        stream: The stream to read, or None when the process has none.
        buffer: The buffer the output goes into.
        wakeup: The event that reports that output arrived, or None when
            the reader has no caller waiting for it.
    """
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            buffer.append(chunk)
            if wakeup is not None:
                wakeup.set()
    except (OSError, ValueError):
        # A stream that ends with an error has nothing more to give; the
        # session keeps the output collected so far.
        return


@dataclass(slots=True)
class SshTerminalSession:
    """One live remote terminal session.

    The session owns an ssh connection that keeps a pseudo-terminal open on
    the host.  The output the host prints is buffered, and the session
    belongs to the tenant, user, and conversation that opened it.
    """

    #: The session's opaque identifier.
    id: str
    #: The tenant that owns the session.
    tenant_id: uuid.UUID
    #: The user that owns the session.
    user_id: uuid.UUID
    #: The conversation that owns the session.
    conversation_id: uuid.UUID
    #: The host the session runs on.
    host: SshHost
    #: The connection that drives the session.
    process: asyncio.subprocess.Process
    #: The trust settings the connection uses.
    trust: SshTrust
    #: The buffered output the host printed.
    stdout: OutputBuffer = field(default_factory=OutputBuffer)
    #: The buffered diagnostics the connection printed.
    stderr: OutputBuffer = field(default_factory=OutputBuffer)
    #: The wall-clock time the session was opened.
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The wall-clock time of the session's last use.
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The idle time after which the session is swept.
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    #: The monotonic deadline after which the session is swept.
    idle_deadline: float = field(default_factory=time.monotonic)
    _readers: list[asyncio.Task[None]] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

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

    @property
    def alive(self) -> bool:
        """Report whether the connection is still running."""
        return not self._closed and getattr(self.process, "returncode", None) is None

    def touch(self) -> None:
        """Extend the idle deadline and record the time of this use."""
        self.idle_deadline = time.monotonic() + self.idle_timeout_seconds
        self.last_used_at = datetime.now(UTC)

    def start_readers(self) -> None:
        """Begin collecting what the host prints."""
        self._readers = [
            asyncio.create_task(_drain_stream(self.process.stdout, self.stdout)),
            asyncio.create_task(_drain_stream(self.process.stderr, self.stderr)),
        ]

    async def write(self, data: str) -> int:
        """Send input to the remote session.

        Args:
            data: The text to send.

        Returns:
            The number of bytes sent.

        Raises:
            SshRuntimeError: When the session has no usable input stream.
        """
        stdin = getattr(self.process, "stdin", None)
        if stdin is None:
            raise SshRuntimeError("This remote terminal session has no input stream.")
        payload = data.encode("utf-8")
        try:
            stdin.write(payload)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise SshRuntimeError("This remote terminal session's input stream is closed.") from exc
        return len(payload)

    def signal(self, name: str) -> bool:
        """Send a signal to the remote session.

        A remote session cannot be signalled the way a local one is: the
        process that runs the command lives on the host.  An interrupt, a
        quit, and a stop are delivered through the remote terminal's line
        discipline, which raises them in the remote foreground process
        group.  A termination ends the connection, which tears the remote
        session down.  A continue has no meaning for a session that was
        never stopped, and is not delivered.

        Args:
            name: The signal's name.

        Returns:
            True when the signal reached the remote session.
        """
        character = TERMINAL_SIGNAL_CHARACTERS.get(name)
        if character is not None:
            stdin = getattr(self.process, "stdin", None)
            if stdin is None:
                return False
            try:
                stdin.write(character)
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                return False
            return True
        if name in TERMINATING_SIGNALS:
            return self._terminate()
        return False

    def _terminate(self) -> bool:
        """End the connection, which ends the remote session.

        Returns:
            True when the connection was running and was ended.
        """
        if getattr(self.process, "returncode", None) is not None:
            return False
        try:
            self.process.terminate()
        except (ProcessLookupError, OSError):
            return False
        return True

    async def close(self) -> None:
        """End the connection and release the session's resources."""
        if self._closed:
            return
        self._closed = True
        for reader in self._readers:
            reader.cancel()
        for reader in self._readers:
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._readers = []
        stdin = getattr(self.process, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                stdin.close()
        if getattr(self.process, "returncode", None) is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
        release_trust(self.trust)


@dataclass(frozen=True, slots=True)
class SshTerminalRead:
    """The result of one read of a remote terminal session.

    Attributes:
        output: The buffered output the read returned.
        truncated: Whether output was left in the buffer after the read.
        dropped_bytes: The number of bytes dropped because the buffer
            reached its cap.
        alive: Whether the connection is still running.
    """

    output: str
    truncated: bool
    dropped_bytes: int
    alive: bool


class SshTerminalManager:
    """The registry of live remote terminal sessions.

    The manager bounds the sessions one deployment may hold and the
    sessions one conversation may hold, sweeps the sessions that went idle,
    and answers every request for a session only to the tenant, user, and
    conversation that own it.
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
        self._sessions: dict[str, SshTerminalSession] = {}
        self.max_sessions_per_conversation = max(1, int(max_sessions_per_conversation))
        self.max_sessions = max(1, int(max_sessions))
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))

    async def open(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        host: SshHost,
        key_directory: Path,
        idle_timeout_seconds: float | None = None,
    ) -> SshTerminalSession:
        """Open one remote terminal session.

        Args:
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            host: The host the session runs on.
            key_directory: The directory the deployment keeps its private
                keys in.
            idle_timeout_seconds: The idle time after which the session is
                swept, or None for the manager's own limit.

        Returns:
            The live session.

        Raises:
            SshRuntimeError: When the session could not be opened.
        """
        await self.sweep()
        live = self.list(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if len(live) >= self.max_sessions_per_conversation:
            raise SshRuntimeError(
                f"This conversation already holds {len(live)} live remote terminal "
                "sessions; close one before opening another."
            )
        if len(self._sessions) >= self.max_sessions:
            raise SshRuntimeError(
                "This deployment already holds its maximum number of live remote terminal sessions."
            )
        command = build_terminal_command(host)
        trust = prepare_trust(host, key_directory=key_directory)
        argv = build_ssh_argv(
            host,
            command=command,
            key_path=trust.key_path,
            known_hosts_path=trust.known_hosts_path,
            tty=True,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=remote_environment(),
            )
        except OSError as exc:
            release_trust(trust)
            raise SshRuntimeError("The SSH client could not be started.") from exc
        timeout = self.idle_timeout_seconds
        if idle_timeout_seconds is not None:
            timeout = max(0.0, float(idle_timeout_seconds))
        session = SshTerminalSession(
            id=f"{TERMINAL_SESSION_PREFIX}{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            host=host,
            process=process,
            trust=trust,
            idle_timeout_seconds=timeout,
        )
        session.touch()
        session.start_readers()
        self._sessions[session.id] = session
        return session

    def get(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> SshTerminalSession:
        """Return one session the caller owns.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The live session.

        Raises:
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise SshTerminalNotFoundError("No such remote terminal session.")
        return session

    def list(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> list[SshTerminalSession]:
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
    ) -> SshTerminalRead:
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
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        session.touch()
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
        output = session.stdout.read(limit)
        return SshTerminalRead(
            output=output,
            truncated=session.stdout.pending_bytes > 0,
            dropped_bytes=session.stdout.dropped_bytes,
            alive=session.alive,
        )

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
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
            SshRuntimeError: When the session has no usable input stream.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        written = await session.write(data)
        session.touch()
        return written

    async def signal(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        name: str,
    ) -> bool:
        """Send a signal to one session.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            name: The signal's name.

        Returns:
            True when the signal reached the remote session.

        Raises:
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        delivered = session.signal(name)
        session.touch()
        return delivered

    async def close(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Close one session and forget it.

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
        expired = [session for session in self._sessions.values() if session.idle_deadline <= now]
        for session in expired:
            await self._shutdown(session)
        return [session.id for session in expired]

    async def _shutdown(self, session: SshTerminalSession) -> None:
        """End one session's connection and forget the session.

        Args:
            session: The session to close.
        """
        self._sessions.pop(session.id, None)
        await session.close()


@dataclass(slots=True)
class SshStreamSession:
    """One live streaming command session.

    The session owns a command that is still running on a host.  Its
    output arrives while the command runs, and the session belongs to the
    tenant, user, and conversation that started it.
    """

    #: The session's opaque identifier.
    id: str
    #: The tenant that owns the session.
    tenant_id: uuid.UUID
    #: The user that owns the session.
    user_id: uuid.UUID
    #: The conversation that owns the session.
    conversation_id: uuid.UUID
    #: The host the command runs on.
    host: SshHost
    #: The connection that drives the command.
    process: asyncio.subprocess.Process
    #: The trust settings the connection uses.
    trust: SshTrust
    #: The buffered output the command produced.
    stdout: OutputBuffer = field(default_factory=OutputBuffer)
    #: The buffered diagnostics the command produced.
    stderr: OutputBuffer = field(default_factory=OutputBuffer)
    #: The wall-clock time the session was opened.
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The wall-clock time of the session's last use.
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The idle time after which the session is swept.
    idle_timeout_seconds: float = 300.0
    #: The monotonic deadline after which the session is swept.
    idle_deadline: float = field(default_factory=time.monotonic)
    #: The event that reports that the command produced output.
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    _readers: list[asyncio.Task[None]] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

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

    @property
    def running(self) -> bool:
        """Report whether the command is still running."""
        return not self._closed and getattr(self.process, "returncode", None) is None

    def touch(self) -> None:
        """Extend the idle deadline and record the time of this use."""
        self.idle_deadline = time.monotonic() + self.idle_timeout_seconds
        self.last_used_at = datetime.now(UTC)

    def start_readers(self) -> None:
        """Begin collecting what the command prints."""
        self._readers = [
            asyncio.create_task(_drain_stream(self.process.stdout, self.stdout, self.wakeup)),
            asyncio.create_task(_drain_stream(self.process.stderr, self.stderr, self.wakeup)),
        ]

    async def close(self) -> None:
        """End the command and release the session's resources."""
        if self._closed:
            return
        self._closed = True
        for reader in self._readers:
            reader.cancel()
        for reader in self._readers:
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._readers = []
        stdin = getattr(self.process, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                stdin.close()
        if getattr(self.process, "returncode", None) is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
        release_trust(self.trust)


@dataclass(frozen=True, slots=True)
class SshStreamRead:
    """The result of one read of a streaming command session.

    Attributes:
        stdout: The output the read returned.
        stderr: The diagnostics the read returned.
        exit_code: The command's exit code, or None while it runs.
        running: Whether the command is still running.
        truncated: Whether output was left in the buffer after the read.
    """

    stdout: str
    stderr: str
    exit_code: int | None
    running: bool
    truncated: bool


class SshStreamRegistry:
    """The registry of live streaming command sessions."""

    def __init__(
        self,
        *,
        max_sessions: int = 16,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        """Create the registry.

        Args:
            max_sessions: The number of live sessions the deployment may
                hold.
            idle_timeout_seconds: The idle time after which a session is
                swept.
        """
        self._sessions: dict[str, SshStreamSession] = {}
        self.max_sessions = max(1, int(max_sessions))
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))

    async def open(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        host: SshHost,
        command: str,
        key_directory: Path,
    ) -> SshStreamSession:
        """Start one command that streams its output.

        Args:
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            host: The host to reach.
            command: The command to run.
            key_directory: The directory the deployment keeps its private
                keys in.

        Returns:
            The live session.

        Raises:
            SshRuntimeError: When the command could not be started.
        """
        await self.sweep()
        if len(self._sessions) >= self.max_sessions:
            raise SshRuntimeError(
                "This deployment already holds its maximum number of live streaming sessions."
            )
        trust = prepare_trust(host, key_directory=key_directory)
        argv = build_ssh_argv(
            host,
            command=build_remote_command(host, command),
            key_path=trust.key_path,
            known_hosts_path=trust.known_hosts_path,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=remote_environment(),
            )
        except OSError as exc:
            release_trust(trust)
            raise SshRuntimeError("The SSH client could not be started.") from exc
        session = SshStreamSession(
            id=f"{STREAM_SESSION_PREFIX}{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            host=host,
            process=process,
            trust=trust,
            idle_timeout_seconds=self.idle_timeout_seconds,
        )
        session.touch()
        session.start_readers()
        self._sessions[session.id] = session
        return session

    def get(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> SshStreamSession:
        """Return one session the caller owns.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The live session.

        Raises:
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise SshTerminalNotFoundError("No such streaming session.")
        return session

    async def read(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        max_bytes: int = DEFAULT_READ_BYTES,
        wait_seconds: float = 0.0,
    ) -> SshStreamRead:
        """Read the output one streaming command has produced.

        A wait ends as soon as the command produced output, so a caller
        that waits for the first line does not have to wait for the whole
        command.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            max_bytes: The largest number of bytes to return.
            wait_seconds: The longest the read waits for output.

        Returns:
            The output and the state of the command.

        Raises:
            SshTerminalNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        session.touch()
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        stdout = ""
        stderr = ""
        while True:
            session.wakeup.clear()
            stdout = session.stdout.read(limit)
            stderr = session.stderr.read(limit)
            if stdout or stderr or not session.running:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.wakeup.wait(), timeout=remaining)
        return SshStreamRead(
            stdout=stdout,
            stderr=stderr,
            exit_code=session.process.returncode,
            running=session.running,
            truncated=session.stdout.pending_bytes > 0,
        )

    async def close(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Close one session and forget it.

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

    async def close_all(self) -> int:
        """Close every live session of the deployment.

        Returns:
            The number of sessions that were closed.
        """
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
        expired = [session for session in self._sessions.values() if session.idle_deadline <= now]
        for session in expired:
            await self._shutdown(session)
        return [session.id for session in expired]

    async def _shutdown(self, session: SshStreamSession) -> None:
        """End one session's command and forget the session.

        Args:
            session: The session to close.
        """
        self._sessions.pop(session.id, None)
        await session.close()


_TERMINAL_MANAGER: SshTerminalManager | None = None
_STREAM_REGISTRY: SshStreamRegistry | None = None


def ssh_terminal_manager() -> SshTerminalManager:
    """Return the process-wide remote terminal manager.

    Returns:
        The manager that owns this deployment's live remote terminal
        sessions.
    """
    global _TERMINAL_MANAGER
    if _TERMINAL_MANAGER is None:
        _TERMINAL_MANAGER = SshTerminalManager()
    return _TERMINAL_MANAGER


def ssh_stream_registry() -> SshStreamRegistry:
    """Return the process-wide streaming command registry.

    Returns:
        The registry that owns this deployment's live streaming sessions.
    """
    global _STREAM_REGISTRY
    if _STREAM_REGISTRY is None:
        _STREAM_REGISTRY = SshStreamRegistry()
    return _STREAM_REGISTRY


__all__ = [
    "MAX_WRITE_BYTES",
    "META_PREFIX",
    "REMOTE_NOT_FOUND",
    "REMOTE_NO_WORKSPACE",
    "REMOTE_PATH_REFUSED",
    "REMOTE_WRONG_KIND",
    "STREAM_SESSION_PREFIX",
    "SUPPORTED_REMOTE_SIGNALS",
    "TERMINAL_SESSION_PREFIX",
    "RemoteEntry",
    "RemoteFile",
    "RemoteNotFoundError",
    "RemotePathRefusedError",
    "SshRuntime",
    "SshRuntimeError",
    "SshStreamRead",
    "SshStreamRegistry",
    "SshStreamSession",
    "SshTerminalManager",
    "SshTerminalNotFoundError",
    "SshTerminalRead",
    "SshTerminalSession",
    "build_delete_command",
    "build_list_command",
    "build_read_command",
    "build_terminal_command",
    "build_write_command",
    "key_directory_for",
    "normalize_remote_path",
    "parse_meta",
    "remote_workspace_for",
    "resolver_prelude",
    "ssh_stream_registry",
    "ssh_terminal_manager",
]
