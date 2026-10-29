"""The Landlock launcher.

The launcher is the process that applies a Landlock ruleset and then becomes
the command.  Landlock restricts the calling thread, so the restriction has
to be applied in the process that will run the command, before it starts.

The launcher is deliberately self-contained: it uses only the standard
library, so the system interpreter can run it and the command it starts
inherits the restriction without the application code being importable.

The launcher is invoked as::

    python3 landlock_launcher.py <rules-json> -- <command> [args...]

and the rules JSON is a list of ``{"path": ..., "access": [...]}`` entries.
The access names are the ones in :mod:`app.deepspace.services.landlock`, so
both sides share one vocabulary.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import sys
from typing import Any

#: The Landlock system calls, by number.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

#: The flag that asks for the supported Landlock ABI version.
_CREATE_RULESET_VERSION = 1

#: The flag that makes the restriction apply to the command's descendants.
_RESTRICT_SELF_INHERIT = 1

#: The rule type for a path-beneath rule.
_RULE_PATH_BENEATH = 1

#: The access rights, by name, in bit order.
_ACCESS_BITS: dict[str, int] = {
    "execute": 1 << 0,
    "write_file": 1 << 1,
    "read_file": 1 << 2,
    "read_dir": 1 << 3,
    "remove_dir": 1 << 4,
    "remove_file": 1 << 5,
    "make_char": 1 << 6,
    "make_dir": 1 << 7,
    "make_regular": 1 << 8,
    "make_socket": 1 << 9,
    "make_fifo": 1 << 10,
    "make_block": 1 << 11,
    "make_sym": 1 << 12,
}

#: The access rights that arrived with later Landlock ABIs.
_LATER_ACCESS_BITS: dict[str, tuple[int, int]] = {
    # name: (bit, minimum ABI)
    "refer": (1 << 13, 2),
    "truncate": (1 << 14, 3),
    "ioctl_device": (1 << 15, 5),
}

#: The rights every Landlock ruleset must handle to be meaningful.
_READ_ACCESS = _ACCESS_BITS["execute"] | _ACCESS_BITS["read_file"] | _ACCESS_BITS["read_dir"]

#: The rights that let a path be created, changed, and removed.
_WRITE_ACCESS = (
    _ACCESS_BITS["write_file"]
    | _ACCESS_BITS["remove_dir"]
    | _ACCESS_BITS["remove_file"]
    | _ACCESS_BITS["make_char"]
    | _ACCESS_BITS["make_dir"]
    | _ACCESS_BITS["make_regular"]
    | _ACCESS_BITS["make_socket"]
    | _ACCESS_BITS["make_fifo"]
    | _ACCESS_BITS["make_block"]
    | _ACCESS_BITS["make_sym"]
)


class _RulesetAttr(ctypes.Structure):
    """The attribute the kernel reads when a ruleset is created."""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    """The attribute the kernel reads when a path rule is added."""

    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> Any:
    """Return the C library handle."""
    return ctypes.CDLL(None, use_errno=True)


def supported_abi() -> int:
    """Return the Landlock ABI this kernel supports.

    Returns:
        The ABI version, or ``0`` when the kernel has no Landlock.
    """
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_NR_CREATE_RULESET),
        ctypes.c_long(0),
        ctypes.c_long(0),
        ctypes.c_long(_CREATE_RULESET_VERSION),
    )
    return int(version) if version > 0 else 0


def access_mask(names: list[str], abi: int) -> int:
    """Return the access bit mask for a list of access names.

    Args:
        names: The access names to include.
        abi: The kernel's Landlock ABI version.

    Returns:
        The bit mask, with the rights the ABI does not know removed.
    """
    mask = 0
    for name in names:
        bit = _ACCESS_BITS.get(name)
        if bit is not None:
            mask |= bit
            continue
        later = _LATER_ACCESS_BITS.get(name)
        if later is not None and abi >= later[1]:
            mask |= later[0]
    return mask


def apply_rules(rules: list[dict[str, Any]]) -> None:
    """Apply the rules and restrict the current process.

    Args:
        rules: The path rules, each with a ``path`` and an ``access`` list.

    Raises:
        OSError: When the kernel refuses the ruleset or a rule.
    """
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    abi = supported_abi()
    if abi <= 0:
        raise OSError(38, "Landlock is not available on this kernel")
    handled = 0
    for rule in rules:
        handled |= access_mask(list(rule.get("access") or []), abi)
    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = int(
        libc.syscall(
            ctypes.c_long(_NR_CREATE_RULESET),
            ctypes.byref(attr),
            ctypes.c_long(ctypes.sizeof(attr)),
            ctypes.c_long(0),
        )
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "the Landlock ruleset could not be created")
    try:
        for rule in rules:
            path = str(rule.get("path") or "")
            if not path:
                continue
            parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                beneath = _PathBeneathAttr(
                    allowed_access=access_mask(list(rule.get("access") or []), abi),
                    parent_fd=parent_fd,
                )
                added = int(
                    libc.syscall(
                        ctypes.c_long(_NR_ADD_RULE),
                        ctypes.c_long(_RULE_PATH_BENEATH),
                        ctypes.c_long(ruleset_fd),
                        ctypes.byref(beneath),
                        ctypes.c_long(0),
                    )
                )
                if added < 0:
                    raise OSError(ctypes.get_errno(), f"the Landlock rule for {path} was refused")
            finally:
                os.close(parent_fd)
        # A restricted process may not gain privileges, so the restriction
        # only holds when the process cannot set them again.
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs could not be set")
        restricted = int(
            libc.syscall(
                ctypes.c_long(_NR_RESTRICT_SELF),
                ctypes.c_long(ruleset_fd),
                ctypes.c_long(_RESTRICT_SELF_INHERIT),
            )
        )
        if restricted < 0:
            raise OSError(ctypes.get_errno(), "the Landlock restriction was refused")
    finally:
        os.close(ruleset_fd)


def probe(rules: list[dict[str, Any]], forbidden_path: str) -> int:
    """Report whether the rules confine the process.

    The probe applies the rules and then tries to write outside them.  A
    write that fails proves the rules are in force; a write that succeeds
    proves they are not, and the caller must not claim a sandbox.

    Args:
        rules: The path rules to apply.
        forbidden_path: A path the rules must not allow writing to.

    Returns:
        ``0`` when the rules confined the process, and ``1`` otherwise.
    """
    try:
        apply_rules(rules)
    except OSError as exc:
        print(f"landlock-run: {exc}", file=sys.stderr)
        return 1
    probe_path = os.path.join(forbidden_path, ".landlock-probe")
    try:
        with open(probe_path, "w", encoding="utf-8") as handle:
            handle.write("probe")
    except OSError:
        # The write was refused, so the rules confine the process.
        return 0
    try:
        os.unlink(probe_path)
    except OSError:
        pass
    return 1


def main(argv: list[str]) -> int:
    """Run the launcher.

    Args:
        argv: The launcher's arguments, without the program name.

    Returns:
        The process exit code.
    """
    if len(argv) < 2:
        print("landlock-run: the launcher needs rules and a command", file=sys.stderr)
        return 2
    raw_rules = argv[0]
    if argv[1] == "--probe":
        if len(argv) < 3:
            print("landlock-run: the probe needs a forbidden path", file=sys.stderr)
            return 2
        return probe(json.loads(raw_rules), argv[2])
    if argv[1] != "--":
        print("landlock-run: the launcher needs a -- separator", file=sys.stderr)
        return 2
    command = argv[2:]
    if not command:
        print("landlock-run: the launcher needs a command", file=sys.stderr)
        return 2
    try:
        apply_rules(json.loads(raw_rules))
    except OSError as exc:
        print(f"landlock-run: {exc}", file=sys.stderr)
        return 1
    # The exec replaces this process, so the restriction the ruleset applied
    # is what the command runs under.
    os.execvp(command[0], command)
    raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
