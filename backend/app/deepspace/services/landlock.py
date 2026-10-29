"""The Landlock runner for DeepSpace.

Landlock is a Linux kernel security module that confines a process by
path.  Unlike a mount namespace, it needs no privileges and no helper
binary, so it is the runner for a host where unprivileged user namespaces
are unavailable and bubblewrap therefore cannot work.

The runner confines **file effects**.  It does not restrict the network,
so a deployment that must keep a command off the network needs bubblewrap
or the container backend instead.

Every host is probed before the runner is used.  A host whose kernel
refuses the rules is reported as unavailable, and the runtime falls back
to the next runner rather than claiming a confinement it does not have.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.deepspace.services.landlock_launcher import (
    access_mask,
    supported_abi,
)

#: The name of the launcher file, next to this module.
LAUNCHER_FILE = "landlock_launcher.py"

#: The read rights a readable path is granted.
READ_ACCESS = ("execute", "read_file", "read_dir")

#: The write rights a writable path is granted.
WRITE_ACCESS = (
    "write_file",
    "remove_dir",
    "remove_file",
    "make_char",
    "make_dir",
    "make_regular",
    "make_socket",
    "make_fifo",
    "make_block",
    "make_sym",
    "truncate",
    "refer",
)

#: The rights a read-only deployment grants everywhere.
READ_ONLY_ACCESS = ("execute", "read_file", "read_dir")

#: The exit code the launcher uses when it cannot confine a command.
RUNNER_FAILURE_EXIT = 1

#: The stderr signature the launcher prints when it fails.
RUNNER_FAILURE_SIGNATURE = "landlock-run:"


@dataclass(frozen=True, slots=True)
class LandlockRule:
    """One path rule.

    Attributes:
        path: The absolute path the rule covers.
        access: The access names the rule grants.
    """

    path: str
    access: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable rule."""
        return {"path": self.path, "access": list(self.access)}


def launcher_path() -> Path:
    """Return the path of the launcher file."""
    return Path(__file__).with_name(LAUNCHER_FILE)


def interpreter() -> str:
    """Return the interpreter that runs the launcher.

    The system interpreter is used rather than the worker's virtual
    environment, so the confined command cannot import the application's own
    modules through the launcher.

    Returns:
        The interpreter path, or an empty string when there is none.
    """
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("python3") or ""


def build_rules(
    *,
    mode: str,
    workspace: Path,
    extra_readable: tuple[str, ...] = (),
) -> list[LandlockRule]:
    """Build the path rules for one call.

    A read-only deployment grants reading everywhere and writing nowhere.
    A workspace-write deployment grants reading everywhere and writing
    inside the workspace and inside a private temporary directory.

    Args:
        mode: ``read-only`` or ``workspace-write``.
        workspace: The only writable directory.
        extra_readable: Additional paths the command may read.

    Returns:
        The rules the launcher applies.
    """
    rules = [LandlockRule(path="/", access=READ_ONLY_ACCESS)]
    for path in extra_readable:
        rules.append(LandlockRule(path=str(path), access=READ_ACCESS))
    if mode == "read-only":
        return rules
    rules.append(LandlockRule(path=str(workspace), access=READ_ACCESS + WRITE_ACCESS))
    rules.append(LandlockRule(path="/tmp", access=READ_ACCESS + WRITE_ACCESS))
    return rules


def rules_payload(rules: list[LandlockRule]) -> str:
    """Return the JSON the launcher reads.

    Args:
        rules: The rules to encode.

    Returns:
        The JSON payload.
    """
    return json.dumps([rule.as_dict() for rule in rules])


@lru_cache(maxsize=1)
def landlock_abi() -> int:
    """Return the Landlock ABI this kernel supports, or zero."""
    if sys.platform != "linux":
        return 0
    try:
        return supported_abi()
    except OSError:
        return 0


@lru_cache(maxsize=1)
def landlock_usable() -> bool:
    """Report whether this host can confine a command with Landlock.

    The probe applies a real ruleset and then tries to write outside it.
    A host whose kernel refuses the rules is not usable, and the runtime
    must not claim a sandbox it does not have.

    Returns:
        True when Landlock confines a command on this host.
    """
    if landlock_abi() <= 0:
        return False
    python = interpreter()
    if not python:
        return False
    rules = build_rules(mode="workspace-write", workspace=Path("/tmp"))
    try:
        completed = subprocess.run(
            [
                python,
                str(launcher_path()),
                rules_payload(rules),
                "--probe",
                "/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def wrap_command(
    *,
    argv: list[str],
    mode: str,
    workspace: Path,
    extra_readable: tuple[str, ...] = (),
) -> list[str] | None:
    """Wrap a command in the Landlock launcher.

    Args:
        argv: The command to run.
        mode: ``read-only`` or ``workspace-write``.
        workspace: The only writable directory.
        extra_readable: Additional paths the command may read.

    Returns:
        The wrapped command, or None when this host cannot confine a
        command with Landlock.
    """
    python = interpreter()
    if not python or not landlock_usable():
        return None
    rules = build_rules(mode=mode, workspace=workspace, extra_readable=extra_readable)
    return [
        python,
        str(launcher_path()),
        rules_payload(rules),
        "--",
        *argv,
    ]


__all__ = [
    "LAUNCHER_FILE",
    "READ_ACCESS",
    "READ_ONLY_ACCESS",
    "RUNNER_FAILURE_EXIT",
    "RUNNER_FAILURE_SIGNATURE",
    "WRITE_ACCESS",
    "LandlockRule",
    "access_mask",
    "build_rules",
    "interpreter",
    "landlock_abi",
    "landlock_usable",
    "launcher_path",
    "rules_payload",
    "wrap_command",
]
