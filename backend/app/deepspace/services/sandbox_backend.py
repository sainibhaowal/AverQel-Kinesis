"""Operating-system sandbox backends for the DeepSpace shell tools.

The shell tools run real commands.  This module wraps those commands in the
strongest cage the host can actually provide:

* Linux uses ``bwrap`` (bubblewrap) with a private mount, PID, IPC, UTS, and
  cgroup namespace.  Only the system directories are visible read-only, the
  workspace is the only writable path, and the user's home directory is
  hidden.
* macOS uses ``sandbox-exec`` with a seatbelt profile that denies everything
  except the system directories, the workspace, and temporary files.
* Any platform with Docker can run the command in a network-isolated
  container when a sandbox image is configured.
* ``none`` keeps the inspection-only behavior for a deployment that cannot
  install a backend.

The backend is chosen at runtime.  A local install therefore gets a real cage
when its operating system offers one and keeps working when it does not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.deepspace.services.landlock import landlock_usable, wrap_command
from app.deepspace.services.windows_acl import (
    windows_acl_runner,
    windows_confinement_available,
)

#: The backend names a deployment may select.
SANDBOX_BACKENDS = (
    "auto",
    "bwrap",
    "landlock",
    "sandbox-exec",
    "docker",
    "windows",
    "none",
)

#: System directories the Linux cage exposes read-only when they exist.
_BWRAP_SYSTEM_DIRECTORIES = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/opt",
    "/nix",
)


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """One command wrapped for a sandbox backend.

    Attributes:
        argv: The complete argument vector to start.
        backend: The backend that produced the wrapper, or ``none``.
        sandboxed: Whether an operating-system cage is active.
    """

    argv: list[str]
    backend: str
    sandboxed: bool


class SandboxBackend(Protocol):
    """One way to contain a command."""

    name: str

    def available(self) -> bool:
        """Report whether this backend can run on the host."""
        ...

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap one argument vector for this backend."""
        ...


class SandboxUnavailableError(RuntimeError):
    """No sandbox backend on this host can confine a command.

    The error is the fail-closed answer: a command that cannot be confined
    is refused, and the message names the runners the platform offers, so
    the operator knows what to install.
    """

    def __init__(self, *, platform: str, options: Sequence[str]) -> None:
        """Create the error.

        Args:
            platform: The platform the request ran on.
            options: The runners that platform offers.
        """
        self.platform = platform
        self.options = tuple(options)
        super().__init__(
            f"No operating-system sandbox is usable on {platform}. "
            f"Install one of: {', '.join(self.options) if self.options else 'none'}."
        )


#: The runners each platform can use, in preference order.
RUNNER_OPTIONS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "linux": ("bubblewrap", "Landlock", "Docker"),
    "darwin": ("sandbox-exec", "Docker"),
    "win32": ("the Windows job-object runner", "Docker"),
    "other": ("Docker",),
}


def runner_options(platform: str | None = None) -> tuple[str, ...]:
    """Return the runners a platform offers.

    Args:
        platform: The platform to report for. The running platform is used
            when this is omitted.

    Returns:
        The runner names.
    """
    key = platform or sys.platform
    if key.startswith("linux"):
        return RUNNER_OPTIONS_BY_PLATFORM["linux"]
    if key == "darwin":
        return RUNNER_OPTIONS_BY_PLATFORM["darwin"]
    if key == "win32":
        return RUNNER_OPTIONS_BY_PLATFORM["win32"]
    return RUNNER_OPTIONS_BY_PLATFORM["other"]


def _bwrap_system_binds() -> list[str]:
    """Return the read-only system binds that exist on this host."""
    binds: list[str] = []
    for directory in _BWRAP_SYSTEM_DIRECTORIES:
        if os.path.exists(directory):
            binds.extend(["--ro-bind", directory, directory])
    return binds


def _bwrap_extra_binds(paths: Sequence[str]) -> list[str]:
    """Return the read-only binds for the extra system paths a caller asked for.

    Args:
        paths: The system directories the confined command may read.

    Returns:
        The ``--ro-bind`` arguments for the paths that exist on this host.
    """
    binds: list[str] = []
    for path in paths:
        candidate = str(path)
        if candidate and os.path.exists(candidate):
            binds.extend(["--ro-bind", candidate, candidate])
    return binds


@lru_cache(maxsize=1)
def _bwrap_usable() -> bool:
    """Probe once whether this host can create a bubblewrap namespace."""
    binary = shutil.which("bwrap")
    if binary is None:
        return False
    arguments = [
        binary,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        *_bwrap_system_binds(),
        "--",
        "/bin/true",
    ]
    try:
        completed = subprocess.run(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class BwrapBackend:
    """Contain a command with bubblewrap on Linux."""

    name = "bwrap"

    def available(self) -> bool:
        """Report whether bubblewrap can create a namespace here."""
        return _bwrap_usable()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap a command in a bubblewrap mount and process namespace."""
        arguments = [
            shutil.which("bwrap") or "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if network:
            arguments.append("--share-net")
        arguments.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/home",
                "--tmpfs",
                "/root",
                *_bwrap_system_binds(),
                *_bwrap_extra_binds(extra_read_only_paths),
                "--ro-bind" if read_only else "--bind",
                str(workspace),
                str(workspace),
                "--chdir",
                str(cwd),
                "--setenv",
                "HOME",
                str(workspace),
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "DEEPSPACE_SHELL",
                "1",
                "--",
                *argv,
            ]
        )
        return SandboxCommand(argv=arguments, backend=self.name, sandboxed=True)


@lru_cache(maxsize=1)
def _seatbelt_usable() -> bool:
    """Probe once whether the seatbelt sandbox confines a command.

    The probe runs a command under a profile that denies every read, and
    checks that the read failed.  A seatbelt installation that cannot
    confine anything is reported as unusable, so a deployment never
    believes it has a cage it does not have.
    """
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return False
    profile = "(version 1)\n(deny default)\n(allow process*)\n"
    try:
        completed = subprocess.run(
            [binary, "-p", profile, "/bin/cat", "/etc/hosts"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # The confined read must fail, so a non-zero exit proves the profile
    # denied it.
    return completed.returncode != 0


def _seatbelt_profile(
    workspace: Path,
    *,
    network: bool,
    read_only: bool,
    extra_read_only_paths: Sequence[str] = (),
) -> str:
    """Return the macOS seatbelt profile that admits only the workspace."""
    roots = [
        directory
        for directory in (
            "/usr",
            "/bin",
            "/sbin",
            "/System",
            "/Library",
            "/Applications",
            "/opt",
            "/private/etc",
            "/private/var/db/dyld",
            "/private/var/folders",
            "/dev",
            "/etc",
            *extra_read_only_paths,
        )
        if os.path.exists(directory)
    ]
    read_rules = " ".join(f'(subpath "{root}")' for root in roots)
    escaped_workspace = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    network_rule = "(allow network*)" if network else "(deny network*)"
    write_rule = (
        ""
        if read_only
        else f'(allow file-write* (subpath "{escaped_workspace}") (subpath "/private/tmp"))\n'
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        f'(allow file-read* {read_rules} (subpath "{escaped_workspace}"))\n'
        f"{write_rule}"
        f"{network_rule}\n"
    )


class SandboxExecBackend:
    """Contain a command with the macOS seatbelt sandbox."""

    name = "sandbox-exec"

    def available(self) -> bool:
        """Report whether the seatbelt sandbox is present and usable.

        The seatbelt binary can be present on a host where the profile
        system refuses to confine anything, so the backend probes it with a
        real read of a file the profile must refuse.  A binary that cannot
        confine is not a usable backend.
        """
        binary = shutil.which("sandbox-exec")
        if binary is None or sys.platform != "darwin":
            return False
        return _seatbelt_usable()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap a command in a seatbelt profile."""
        del cwd
        profile = _seatbelt_profile(
            workspace,
            network=network,
            read_only=read_only,
            extra_read_only_paths=extra_read_only_paths,
        )
        return SandboxCommand(
            argv=[shutil.which("sandbox-exec") or "sandbox-exec", "-p", profile, *argv],
            backend=self.name,
            sandboxed=True,
        )


class WindowsBackend:
    """Confine a command on Windows with a restricted token.

    The command runs with a token whose restricting SIDs carry a capability
    SID that only the workspace grants, and whose integrity level is Low.
    A write then has to satisfy the user's own rights *and* the capability
    grant, so a write anywhere else fails even when the user could normally
    write there.

    The runner fails closed.  Every step that must succeed for confinement
    is checked, and a failure means the command never starts, because a
    command that runs without containment is worse than a command that does
    not run.  The boundary is partial: it confines writes and deletes, not
    reads and not the network.  A deployment that needs a full file-system
    boundary on Windows configures the Docker backend with a Windows
    container image.
    """

    name = "windows-acl"

    def available(self) -> bool:
        """Report whether this host can confine a command this way."""
        return windows_confinement_available()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap a command in the Windows restricted-token launcher."""
        del network
        del extra_read_only_paths
        wrapped = windows_acl_runner(
            argv=list(argv),
            workspace=workspace,
            working_directory=cwd,
            read_only=read_only,
        )
        if wrapped is None:
            return SandboxCommand(argv=list(argv), backend="none", sandboxed=False)
        return SandboxCommand(argv=wrapped, backend=self.name, sandboxed=True)


class LandlockBackend:
    """Contain a command on Linux with the Landlock kernel module.

    Landlock confines a process by path without a namespace, so it is the
    runner for a host where unprivileged user namespaces are unavailable
    and bubblewrap therefore cannot work.  It confines file effects only;
    it does not restrict the network.
    """

    name = "landlock"

    def available(self) -> bool:
        """Report whether this host can confine a command with Landlock."""
        return landlock_usable()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap a command in the Landlock launcher."""
        del cwd
        del network
        mode = "read-only" if read_only else "workspace-write"
        wrapped = wrap_command(
            argv=list(argv),
            mode=mode,
            workspace=workspace,
            extra_readable=tuple(extra_read_only_paths),
        )
        if wrapped is None:
            return SandboxCommand(argv=list(argv), backend="none", sandboxed=False)
        return SandboxCommand(argv=wrapped, backend=self.name, sandboxed=True)


class DockerBackend:
    """Contain a command in a network-isolated container."""

    name = "docker"

    def __init__(self, image: str | None) -> None:
        """Create the backend for one configured image.

        Args:
            image: The container image, or None when the deployment has not
                chosen one.
        """
        self.image = image

    def available(self) -> bool:
        """Report whether a Docker engine and an image are configured."""
        return bool(self.image) and shutil.which("docker") is not None

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        network: bool,
        read_only: bool = False,
        extra_read_only_paths: Sequence[str] = (),
    ) -> SandboxCommand:
        """Wrap a command in a container with the workspace mounted."""
        del extra_read_only_paths
        arguments = [shutil.which("docker") or "docker", "run", "--rm"]
        if os.name == "posix":
            arguments.append("--init")
        if not network:
            arguments.extend(["--network", "none"])
        mount = f"type=bind,source={workspace},target={workspace}"
        if read_only:
            mount += ",readonly"
        arguments.extend(["--mount", mount, "-w", str(cwd)])
        if os.name == "posix":
            arguments.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        arguments.extend([str(self.image), *argv])
        return SandboxCommand(argv=arguments, backend=self.name, sandboxed=True)


def _configured_backend(settings: Any) -> str:
    """Return the normalized backend name from the settings."""
    raw = str(getattr(settings, "deepspace_sandbox_backend", "auto") or "auto").strip().lower()
    return raw if raw in SANDBOX_BACKENDS else "auto"


def _network_enabled(settings: Any) -> bool:
    """Return whether the sandbox keeps network access."""
    return bool(getattr(settings, "deepspace_sandbox_network", True))


def build_sandbox_command(
    settings: Any,
    argv: Sequence[str],
    *,
    workspace: Path,
    cwd: Path,
    read_only: bool = False,
    network: bool | None = None,
    extra_read_only_paths: Sequence[str] = (),
) -> SandboxCommand:
    """Wrap one command in the best available backend.

    Args:
        settings: The deployment settings.
        argv: The shell program and its arguments.
        workspace: The only writable directory the command may see.
        cwd: The directory the command starts in.
        read_only: Whether the command may write inside the workspace.
        network: Whether the command keeps network access. ``None`` keeps the
            deployment's configured network policy.
        extra_read_only_paths: Additional system paths the command may read.
            A program that runs inside the cage needs its interpreter and
            standard library visible.

    Returns:
        The wrapped command.  ``sandboxed`` is False when no backend is
        available; the caller decides whether that is acceptable.
    """
    configured = _configured_backend(settings)
    keep_network = _network_enabled(settings) if network is None else network
    candidates: list[SandboxBackend] = []
    if configured == "auto":
        if sys.platform.startswith("linux"):
            candidates.append(BwrapBackend())
            candidates.append(LandlockBackend())
        elif sys.platform == "darwin":
            candidates.append(SandboxExecBackend())
        elif sys.platform == "win32":
            # Windows has no seatbelt-equivalent, so the job-object runner
            # comes before the container backend.
            candidates.append(WindowsBackend())
        candidates.append(DockerBackend(getattr(settings, "deepspace_sandbox_image", None)))
    elif configured == "bwrap":
        candidates.append(BwrapBackend())
    elif configured == "landlock":
        candidates.append(LandlockBackend())
    elif configured == "sandbox-exec":
        candidates.append(SandboxExecBackend())
    elif configured == "windows":
        candidates.append(WindowsBackend())
    elif configured == "docker":
        candidates.append(DockerBackend(getattr(settings, "deepspace_sandbox_image", None)))
    else:
        return SandboxCommand(argv=list(argv), backend="none", sandboxed=False)

    for backend in candidates:
        if backend.available():
            return backend.wrap(
                argv,
                workspace=workspace,
                cwd=cwd,
                network=keep_network,
                read_only=read_only,
                extra_read_only_paths=extra_read_only_paths,
            )
    return SandboxCommand(argv=list(argv), backend="none", sandboxed=False)


def sandbox_status(settings: Any) -> dict[str, Any]:
    """Return the backend a deployment would use, for settings and diagnostics.

    Args:
        settings: The deployment settings.

    Returns:
        The configured backend, the selected backend, whether it can cage
        a command on this host, the platform the report describes, and the
        typed error a command would fail with when no cage is usable.
    """
    probe = build_sandbox_command(
        settings,
        ["/bin/true"],
        workspace=Path.cwd(),
        cwd=Path.cwd(),
    )
    available = probe.sandboxed
    return {
        "configured": _configured_backend(settings),
        "selected": probe.backend,
        "available": available,
        "network": _network_enabled(settings),
        "platform": sys.platform,
        "enforcement": _enforcement_for(probe.backend),
        "error_code": None if available else "SANDBOX_UNAVAILABLE",
        "runner_options": list(runner_options()),
    }


def _enforcement_for(backend: str) -> str:
    """Return how much of the promised boundary a backend governs.

    Args:
        backend: The selected backend's name.

    Returns:
        ``full`` for a backend that governs every promised file effect and
        the network, and ``partial`` for one that governs only part of it.
        The Landlock runner confines file effects but not the network, and
        the Windows restricted-token runner confines writes and deletes but
        not reads.
    """
    if backend in {"bwrap", "sandbox-exec", "docker"}:
        return "full"
    if backend in {"landlock", "windows-acl"}:
        return "partial"
    return "none"


__all__ = [
    "BwrapBackend",
    "DockerBackend",
    "SANDBOX_BACKENDS",
    "SandboxBackend",
    "SandboxCommand",
    "SandboxExecBackend",
    "WindowsBackend",
    "build_sandbox_command",
    "sandbox_status",
]
