"""Remote command execution over SSH.

A host is a machine the deployment has reviewed.  Two rules make the
remote boundary safe:

* **The host key is checked.**  The connection is made against a
  ``known_hosts`` file built from the reviewed host's own entry, with
  ``StrictHostKeyChecking`` on.  A host that presents a different key is
  refused, so a machine that answers for a reviewed name cannot take its
  place.
* **The key never leaves the server.**  The private key is read from the
  path the deployment resolves, and the reference to it - never the key -
  is what the database holds.  A key file that other users can read is
  refused.

A command runs in the host's reviewed remote directory, with a timeout and
an output cap, and the deployment's own secrets never reach the remote
shell.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.ssh_host import DeepSpaceSshHost
from app.deepspace.services.ssh_config import SshRuntimeConfig, parse_ssh_runtime_config
from app.deepspace.services.subprocess_stdio import SubprocessStdio, spawn_with_stdio

#: The largest output one remote command may produce, in bytes.
MAX_OUTPUT_BYTES = 256_000

#: The largest command one call may send, in characters.
MAX_COMMAND_CHARS = 8_000

#: The environment the remote shell receives.
_ALLOWED_ENVIRONMENT = (
    "LANG",
    "LC_ALL",
    "TERM",
    "PATH",
    "HOME",
    "TZ",
)

#: The exit code ssh uses when the host key does not match.
_HOST_KEY_MISMATCH = 255


class SshError(RuntimeError):
    """A remote command could not be run."""


class SshHostNotFoundError(SshError):
    """The named host is not a host this tenant may reach."""


class SshTrustError(SshError):
    """The host's identity or key could not be trusted."""


@dataclass(frozen=True, slots=True)
class SshHost:
    """One reviewed host, as the runtime sees it.

    Attributes:
        id: The host's id.
        tenant_id: The owning tenant.
        user_id: The user who added it.
        name: The host's name inside the workspace.
        hostname: The host or address to connect to.
        port: The port to connect to.
        username: The user the command runs as.
        known_hosts_entry: The host key the connection must present.
        key_secret_ref: The name of the private key the deployment holds.
        remote_workspace: The directory the command runs in.
        enabled: Whether the host may be reached.
        timeout_seconds: How long one command may run.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    hostname: str
    port: int
    username: str
    known_hosts_entry: str
    key_secret_ref: str | None
    remote_workspace: str | None
    enabled: bool
    timeout_seconds: int
    runtime_config: SshRuntimeConfig = field(default_factory=SshRuntimeConfig)

    @property
    def destination(self) -> str:
        """Return the ``user@host`` the command runs as.

        Returns:
            The destination.
        """
        return f"{self.username}@{self.hostname}"

    @property
    def known_hosts_name(self) -> str:
        """Return the name the host key is filed under.

        A non-default port is filed under ``[host]:port``, which is what
        ``known_hosts`` uses.

        Returns:
            The name.
        """
        if self.port == 22:
            return self.hostname
        return f"[{self.hostname}]:{self.port}"


def host_from_row(row: DeepSpaceSshHost) -> SshHost:
    """Build a runtime host from its stored row.

    Args:
        row: The stored host.

    Returns:
        The runtime host.
    """
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return SshHost(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        name=row.name,
        hostname=row.hostname,
        port=int(row.port or 22),
        username=row.username,
        known_hosts_entry=row.known_hosts_entry or "",
        key_secret_ref=row.key_secret_ref,
        remote_workspace=row.remote_workspace,
        enabled=bool(row.enabled),
        timeout_seconds=int(row.timeout_seconds or 60),
        runtime_config=parse_ssh_runtime_config(metadata.get("runtime_config")),
    )


class SshHostStore:
    """The hosts one tenant may reach."""

    def __init__(self, db: Session) -> None:
        """Create the store.

        Args:
            db: The database session.
        """
        self.db = db

    def get(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
    ) -> SshHost:
        """Return one host by name.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            name: The host's name.

        Returns:
            The host.

        Raises:
            SshHostNotFoundError: When the tenant has no such host.
        """
        row = self.db.execute(
            select(DeepSpaceSshHost).where(
                DeepSpaceSshHost.tenant_id == tenant_id,
                DeepSpaceSshHost.user_id == user_id,
                DeepSpaceSshHost.name == str(name or "").strip(),
            )
        ).scalar_one_or_none()
        if row is None:
            raise SshHostNotFoundError("No such SSH host.")
        return host_from_row(row)

    def list(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[SshHost]:
        """Return every host the tenant may reach.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The hosts.
        """
        rows = (
            self.db.execute(
                select(DeepSpaceSshHost)
                .where(
                    DeepSpaceSshHost.tenant_id == tenant_id,
                    DeepSpaceSshHost.user_id == user_id,
                )
                .order_by(DeepSpaceSshHost.name)
            )
            .scalars()
            .all()
        )
        return [host_from_row(row) for row in rows]


def resolve_key_path(key_secret_ref: str | None, *, key_directory: Path) -> Path | None:
    """Resolve the private key one host uses.

    The key is named by a reference the deployment controls, and the key
    itself stays on the server.  A key that other users can read is
    refused, because a shared key is not a private key.

    Args:
        key_secret_ref: The name of the private key.
        key_directory: The directory the deployment keeps its keys in.

    Returns:
        The key's path, or None when the host uses the agent's own keys.

    Raises:
        SshTrustError: When the key is missing or readable by others.
    """
    if not key_secret_ref:
        return None
    reference = str(key_secret_ref).strip()
    if not reference:
        return None
    candidate = Path(reference)
    if not candidate.is_absolute():
        candidate = key_directory / reference
    try:
        mode = candidate.stat().st_mode
    except OSError as exc:
        raise SshTrustError("The private key for this host could not be read.") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SshTrustError("The private key for this host is readable by other users.")
    return candidate


@dataclass(frozen=True, slots=True)
class SshTrust:
    """The trust settings one connection needs.

    The connection checks the host key against a file that holds nothing but
    the reviewed entry, and it identifies itself with a private key the
    deployment holds on the server.

    Attributes:
        key_path: The private key the connection uses, or None when the
            host uses the agent's own keys.
        known_hosts_path: The file the host key is checked against.
        directory: The private directory that holds the file.
    """

    key_path: Path | None
    known_hosts_path: Path
    directory: Path


def prepare_trust(host: SshHost, *, key_directory: Path) -> SshTrust:
    """Prepare the trust settings one connection needs.

    The reviewed host key is written into a private directory only the
    server can read, and the private key is resolved from the name the
    deployment holds.  The caller owns the returned directory and releases
    it when the connection ends.

    Args:
        host: The host to reach.
        key_directory: The directory the deployment keeps its private keys
            in.

    Returns:
        The connection's trust settings.

    Raises:
        SshError: When the host may not be reached.
        SshTrustError: When the host's identity or key could not be trusted.
    """
    if not host.enabled:
        raise SshError("This SSH host is not enabled.")
    if not host.known_hosts_entry.strip():
        raise SshTrustError("This SSH host has no reviewed host key.")
    key_path = resolve_key_path(host.key_secret_ref, key_directory=key_directory)
    directory = Path(tempfile.mkdtemp(prefix="deepspace-ssh-"))
    try:
        os.chmod(directory, 0o700)
        known_hosts_path = directory / "known_hosts"
        known_hosts_path.write_text(host.known_hosts_entry.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise SshError("The reviewed host key could not be prepared.") from exc
    return SshTrust(
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        directory=directory,
    )


def release_trust(trust: SshTrust) -> None:
    """Remove the private directory one connection used.

    The file the host key was checked against is removed with it, so a
    connection that ends leaves nothing behind.

    Args:
        trust: The connection's trust settings.
    """
    shutil.rmtree(trust.directory, ignore_errors=True)


def build_ssh_argv(
    host: SshHost,
    *,
    command: str,
    key_path: Path | None,
    known_hosts_path: Path,
    tty: bool = False,
    control_path: Path | None = None,
) -> list[str]:
    """Build the argument vector that runs one command on one host.

    The connection is non-interactive, the host key is checked against the
    reviewed entry, and the command is passed as a single argument so the
    remote shell runs exactly what was asked for.  A connection that opens
    a terminal asks for a pseudo-terminal on the host, so an interactive
    program behaves the way it does for a person.

    Args:
        host: The host to reach.
        command: The command to run.
        key_path: The private key the connection uses.
        known_hosts_path: The file the host key is checked against.
        tty: Whether the connection asks for a pseudo-terminal on the host.

    Returns:
        The argument vector.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"ConnectTimeout={max(5, int(host.timeout_seconds or 60))}",
        "-p",
        str(host.port),
    ]
    if tty:
        argv.append("-tt")
    if control_path is not None:
        argv.extend(
            [
                "-o",
                "ControlMaster=yes",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=no",
            ]
        )
    if key_path is not None:
        argv.extend(["-i", str(key_path)])
    argv.extend([host.destination, command])
    return argv


def build_ssh_control_argv(
    host: SshHost,
    *,
    key_path: Path | None,
    known_hosts_path: Path,
    control_path: Path,
    operation: str,
    forward_spec: str,
) -> list[str]:
    """Build a narrowly validated OpenSSH control forward/cancel request."""
    if operation not in {"forward", "cancel"}:
        raise SshError("Only SSH forward and cancel control operations are supported.")
    if not forward_spec or "\x00" in forward_spec or len(forward_spec) > 1_024:
        raise SshError("The SSH forwarding specification is invalid.")
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"ConnectTimeout={max(5, int(host.timeout_seconds or 60))}",
        "-o",
        f"ControlPath={control_path}",
        "-p",
        str(host.port),
    ]
    if key_path is not None:
        argv.extend(["-i", str(key_path)])
    argv.extend(["-O", operation, "-L", forward_spec, host.destination])
    return argv


def build_remote_command(host: SshHost, command: str) -> str:
    """Build a command confined to the host's reviewed workspace.

    Bubblewrap is required by default. It exposes the system runtime read-only,
    the reviewed workspace as the only writable project directory, a private
    temporary directory, and no network unless the host policy explicitly
    enables it. A missing or unusable Bubblewrap installation fails closed.

    Args:
        host: The host the command runs on.
        command: The command to run.

    Returns:
        The remote command line.
    """
    text = str(command or "").strip()
    if not text:
        raise SshError("A command is required.")
    if len(text) > MAX_COMMAND_CHARS:
        raise SshError(f"A command may be at most {MAX_COMMAND_CHARS} characters.")
    runtime_config = host.runtime_config
    if runtime_config.sandbox_mode == "disabled":
        if host.remote_workspace:
            return f"cd {shlex.quote(host.remote_workspace)} && {text}"
        return text

    root = str(runtime_config.workspace or host.remote_workspace or "").strip()
    if not root or not root.startswith("/") or "\x00" in root:
        raise SshError("Bubblewrap requires an absolute reviewed remote workspace.")
    if any(part in {".", ".."} for part in PurePosixPath(root).parts):
        raise SshError("The reviewed remote workspace contains an unsafe path segment.")
    reviewed = str(host.remote_workspace or "").rstrip("/") or "/"
    normalized_root = root.rstrip("/") or "/"
    if runtime_config.workspace and normalized_root != reviewed:
        prefix = reviewed.rstrip("/") + "/" if reviewed != "/" else "/"
        if not normalized_root.startswith(prefix):
            raise SshError("The sandbox workspace must stay inside the reviewed host directory.")

    parts = [
        "command -v bwrap >/dev/null 2>&1 || { printf '%s\\n' "
        "'DEEPSPACE_SANDBOX_UNAVAILABLE: Bubblewrap is required on this SSH host.' >&2; exit 78; }",
        "set -- bwrap --die-with-parent --unshare-user --unshare-pid --unshare-uts --unshare-ipc",
    ]
    if not runtime_config.network_access:
        parts.append('set -- "$@" --unshare-net')
    parts.extend(
        [
            "for _ds_path in /usr /bin /sbin /lib /lib64 /etc; do",
            '  if [ -d "$_ds_path" ]; then set -- "$@" --ro-bind "$_ds_path" "$_ds_path"; fi',
            "done",
            'set -- "$@" --dev /dev --proc /proc --tmpfs /tmp',
            f'set -- "$@" --bind {shlex.quote(root)} /workspace --chdir /workspace',
            'set -- "$@" --clearenv --setenv PATH /usr/bin:/bin --setenv HOME /workspace --setenv LANG C.UTF-8',
            f'exec "$@" /bin/sh -lc {shlex.quote(text)}',
        ]
    )
    return "\n".join(parts)


def remote_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment the remote command receives.

    The environment carries the paths a command needs and none of the
    deployment's secrets.

    Args:
        extra: The variables the confinement adds.

    Returns:
        The environment.
    """
    values: dict[str, str] = {}
    for name in _ALLOWED_ENVIRONMENT:
        value = os.environ.get(name)
        if value:
            values[name] = value
    values["DEEPSPACE_SANDBOX"] = "1"
    values["DEEPSPACE_SANDBOX_BACKEND"] = "ssh"
    if extra:
        values.update(extra)
    return values


@dataclass(frozen=True, slots=True)
class SshResult:
    """The outcome of one remote command.

    Attributes:
        exit_code: The command's exit code.
        stdout: The command's standard output.
        stderr: The command's standard error.
        truncated: Whether the output was cut short.
    """

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    stdout_bytes: bytes = b""


class SshExecutor:
    """Runs one command on one reviewed host."""

    def __init__(self, *, key_directory: Path) -> None:
        """Create the executor.

        Args:
            key_directory: The directory the deployment keeps its private
                keys in.
        """
        self.key_directory = key_directory

    async def run(self, host: SshHost, *, command: str) -> SshResult:
        """Run one command on one host.

        Args:
            host: The host to reach.
            command: The command to run.

        Returns:
            The outcome.

        Raises:
            SshError: When the command could not be run.
            SshTrustError: When the host's identity could not be trusted.
        """
        return await self.run_prepared(host, command=build_remote_command(host, command))

    async def run_prepared(
        self,
        host: SshHost,
        *,
        command: str,
        stdin: bytes | None = None,
    ) -> SshResult:
        """Run a command the caller has already prepared.

        The prepared command runs as it is, so a caller that confines a
        path on the host keeps that confinement.  The connection itself is
        the same one every command uses: the reviewed host key is checked,
        the key stays on the server, and the client process receives the
        reduced environment.

        Args:
            host: The host to reach.
            command: The command line the remote shell runs.
            stdin: The bytes the command reads from its standard input, or
                None when the command reads no input.

        Returns:
            The outcome.

        Raises:
            SshError: When the command could not be run.
            SshTrustError: When the host's identity could not be trusted.
        """
        trust = prepare_trust(host, key_directory=self.key_directory)
        try:
            argv = build_ssh_argv(
                host,
                command=command,
                key_path=trust.key_path,
                known_hosts_path=trust.known_hosts_path,
            )
            try:
                process_handle = await spawn_with_stdio(
                    argv,
                    parent_environment=remote_environment(),
                    stdio=SubprocessStdio(
                        stdin="pipe" if stdin is not None else "ignore",
                        stdout="pipe",
                        stderr="pipe",
                    ),
                )
                process = process_handle.process
            except OSError as exc:
                raise SshError("The SSH client could not be started.") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=stdin),
                    timeout=max(5, int(host.timeout_seconds or 60)),
                )
            except TimeoutError:
                await process_handle.terminate_and_wait()
                raise SshError(
                    f"The command on {host.name!r} ran for longer than its timeout."
                ) from None
        finally:
            release_trust(trust)
        exit_code = int(process.returncode or 0)
        if exit_code == _HOST_KEY_MISMATCH and b"host key" in (stderr or b"").lower():
            raise SshTrustError(f"The host key for {host.name!r} does not match the reviewed key.")
        stdout_text = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        stderr_text = stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return SshResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            truncated=len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES,
            stdout_bytes=stdout[:MAX_OUTPUT_BYTES],
        )


def host_summary(host: SshHost) -> dict[str, Any]:
    """Return the public view of one host.

    The view carries no key material and no path to a key.

    Args:
        host: The host.

    Returns:
        The summary.
    """
    return {
        "id": str(host.id),
        "name": host.name,
        "hostname": host.hostname,
        "port": host.port,
        "username": host.username,
        "enabled": host.enabled,
        "remote_workspace": host.remote_workspace,
        "timeout_seconds": host.timeout_seconds,
        "has_key": bool(host.key_secret_ref),
        "runtime_config": host.runtime_config.model_dump(mode="json"),
    }


__all__ = [
    "MAX_COMMAND_CHARS",
    "MAX_OUTPUT_BYTES",
    "SshError",
    "SshExecutor",
    "SshHost",
    "SshHostNotFoundError",
    "SshHostStore",
    "SshResult",
    "SshTrust",
    "SshTrustError",
    "build_remote_command",
    "build_ssh_control_argv",
    "build_ssh_argv",
    "host_from_row",
    "host_summary",
    "prepare_trust",
    "release_trust",
    "remote_environment",
    "resolve_key_path",
]
