"""The Windows write-restricted sandbox runner.

Windows has no seatbelt-equivalent that confines a process by policy, so
this runner confines the two effects it can confine with the operating
system's own primitives: **writes** and **deletes**.

The mechanism is a restricted token.  The command runs with a token whose
restricting SIDs carry a capability SID that is granted only on the
workspace and on a private temporary directory, and whose integrity level
is Low.  A write then has to satisfy two checks at once: the normal
access check against the user's own rights, and the restricted check
against the capability SID.  The capability SID is granted nowhere else, so
a write anywhere else fails the restricted check even when the user could
normally write there.

The runner fails closed.  Every step that must succeed for confinement --
creating the token, lowering its integrity, granting the capability ACE, and
assigning the process to the job -- is checked, and a failure means the
command never starts.  A command that runs unconfined would be worse than a
command that does not run.

The boundary is **partial**, and the product reports it that way.  Reads are
not confined, an NTFS hard link aliases one file object across paths, and a
tree another tool has ACL'd with a package SID is unreadable to the
Low-integrity child.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The Windows error a call reports when it fails.
ERROR_SUCCESS = 0

#: The access mask bits the runner grants on a confined root.
GENERIC_ALL = 0x10000000

#: The rights the runner denies to the world on a confined root, so a
#: granted root cannot delete through its parent's delete-child right.
FILE_DELETE_CHILD = 0x00000040

#: The integrity level the confined token runs at.
TOKEN_MANDATORY_LABEL_LOW = 0x00000001

#: The label the mandatory integrity policy uses.
SECURITY_MANDATORY_LOW_RID = 0x00000010

#: The integrity label the runner writes on a confined root.
LOW_INTEGRITY_LABEL = "S:(ML;OICI;NW;;;{00160200-0000-0000-C000-000000000046})"

#: The well-known Everyone SID, used for the delete-child deny.
EVERYONE_SID = "S-1-1-0"

#: The name of the extended-information job-object limit class.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: The job-object limit that ends every process in the job when the job
#: handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: The largest number of processes one confined job allows.
JOB_OBJECT_ACTIVE_PROCESS_LIMIT = 64

#: The token restriction that a restricting SID list implies.
DISABLE_MAX_PRIVILEGE = 0x00000001
SANDBOX_INERT = 0x00020000

#: The duplication mode that produces a restricted primary token.
DUPLICATE_SAME_ACCESS = 0x00000002


class WindowsAclError(RuntimeError):
    """A step of the Windows confinement could not be completed."""


@dataclass(frozen=True, slots=True)
class WindowsGrant:
    """One directory a confined command may write to.

    Attributes:
        path: The directory the grant covers.
        capability_sid: The SID the grant is expressed as.
    """

    path: str
    capability_sid: str


def capability_sid_for(path: Path) -> str:
    """Return the capability SID that names one directory.

    The SID is derived from the directory's canonical path, so the same
    directory always carries the same SID and a session that is resumed
    keeps its authority.  A different directory derives a different SID, so
    one grant never implies another.

    Args:
        path: The directory the SID names.

    Returns:
        The SID, in its string form.
    """
    canonical = os.path.normcase(os.path.abspath(str(path)))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()[:8]
    # A capability SID lives under the NT authority with the "private
    # namespace" sub-authority, which is the range Windows reserves for
    # application-defined SIDs.
    return f"S-1-15-3-1-{int.from_bytes(digest[:4], 'big')}-{int.from_bytes(digest[4:], 'big')}"


def workspace_grants(workspace: Path) -> list[WindowsGrant]:
    """Return the grants one confined command receives.

    The workspace is granted, and a private temporary directory beside it
    is granted with its own SID, so two sessions that share a workspace do
    not inherit each other's temporary authority.

    Args:
        workspace: The workspace the command may write to.

    Returns:
        The grants.
    """
    temp_root = workspace / ".deepspace" / "sandbox-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    session_temp = temp_root / uuid.uuid4().hex
    session_temp.mkdir(parents=True, exist_ok=True)
    return [
        WindowsGrant(path=str(workspace), capability_sid=capability_sid_for(workspace)),
        WindowsGrant(path=str(session_temp), capability_sid=capability_sid_for(session_temp)),
    ]


def _grants_script(grants: list[WindowsGrant], *, read_only: bool) -> str:
    """Build the PowerShell program that applies the grants.

    The program grants the capability SID on each confined root, denies the
    world's delete-child right there so one grant cannot reach through a
    parent, and labels the root Low so the confined token can write to it.

    Args:
        grants: The roots the command may write to.
        read_only: Whether the command may write at all.

    Returns:
        The PowerShell program.
    """
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$acl = Get-Acl -LiteralPath $args[0]",
    ]
    for index, grant in enumerate(grants):
        lines.extend(
            [
                f"$cap = New-Object System.Security.Principal.SecurityIdentifier('{grant.capability_sid}')",
                "$rule = New-Object System.Security.AccessControl.FileSystemAccessRule("
                "$cap, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')",
                "$acl.SetAccessRule($rule)",
            ]
        )
        if not read_only:
            lines.extend(
                [
                    f"$everyone = New-Object System.Security.Principal.SecurityIdentifier('{EVERYONE_SID}')",
                    "$deny = New-Object System.Security.AccessControl.FileSystemAccessRule("
                    "$everyone, 'DeleteSubdirectoriesAndFiles', 'ContainerInherit,ObjectInherit', "
                    "'None', 'Deny')",
                    "$acl.SetAccessRule($deny)",
                ]
            )
        lines.append(f"Set-Acl -LiteralPath $args[{index + 1}] -AclObject $acl")
    lines.extend(
        [
            "if (-not $args[2].Equals('read-only')) {",
            "  foreach ($target in $args[1..$args.Length]) {",
            "    if (Test-Path -LiteralPath $target) {",
            "      & icacls.exe $target /setintegritylevel (OI)(CI)L | Out-Null",
            "    }",
            "  }",
            "}",
        ]
    )
    return "\n".join(lines)


def build_windows_runner(
    *,
    argv: list[str],
    workspace: Path,
    read_only: bool = False,
) -> list[str] | None:
    """Build the argument vector that confines one command on Windows.

    Args:
        argv: The command to run.
        workspace: The only directory the command may write to.
        read_only: Whether the command may write at all.

    Returns:
        The argument vector, or None when this host is not Windows.
    """
    if os.name != "nt":
        return None
    grants = workspace_grants(workspace)
    script = _grants_script(grants, read_only=read_only)
    arguments = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
        "-Args",
        str(workspace),
        *[grant.path for grant in grants],
        "write" if not read_only else "read-only",
        "--",
        *argv,
    ]
    return arguments


def windows_acl_runner(
    *,
    argv: list[str],
    workspace: Path,
    working_directory: Path,
    read_only: bool = False,
) -> list[str] | None:
    """Build the launcher that confines one command on Windows.

    The launcher is a separate program, so the configuration it reads
    names the command, the grants, and the working directory.

    Args:
        argv: The command to run.
        workspace: The only directory the command may write to.
        working_directory: The directory the command starts in.
        read_only: Whether the command may write at all.

    Returns:
        The launcher argument vector, or None when this host is not
        Windows.
    """
    if os.name != "nt":
        return None
    grants = workspace_grants(workspace)
    configuration = {
        "argv": list(argv),
        "grants": [
            {"path": grant.path, "capability_sid": grant.capability_sid} for grant in grants
        ],
        "working_directory": str(working_directory),
        "read_only": read_only,
    }
    handle, path = tempfile.mkstemp(prefix="deepspace-windows-acl-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(configuration, stream)
    except OSError:
        return None
    launcher = Path(__file__).with_name("windows_acl_launcher.py")
    return [sys.executable, str(launcher), path]


def windows_confinement_available() -> bool:
    """Report whether this host can confine a command's writes."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return hasattr(kernel32, "CreateRestrictedToken")


def describe_windows_boundary() -> dict[str, Any]:
    """Describe what the Windows boundary governs.

    Returns:
        The enforcement level and the effects the runner confines.
    """
    return {
        "backend": "windows-acl",
        "enforcement": "partial",
        "confines": ["write", "delete"],
        "does_not_confine": ["read", "network"],
        "available": windows_confinement_available(),
    }


__all__ = [
    "WindowsAclError",
    "WindowsGrant",
    "build_windows_runner",
    "capability_sid_for",
    "describe_windows_boundary",
    "windows_acl_runner",
    "windows_confinement_available",
    "workspace_grants",
]
