"""Platform process-table inspection for DeepSpace.

This is the Python-native port of the AverQel KineSis process inspector.
It provides /proc-based (Linux) and ps-based (macOS) process-table inspection
for terminal readiness, signals, and teardown, plus a Windows stub.

The inspector answers tree/session/alive questions from one shared snapshot
so a readiness poll's cost stays bounded regardless of descendant count.
"""

from __future__ import annotations

import os
import re
import stat as stat_mod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus start identity, preventing teardown escalation after PID reuse."""

    pid: int
    started: str


@dataclass
class ProcStat:
    pid: int
    parent_pid: int
    pgrp: int
    session: int
    state: str
    tty_device: int
    tpgid: int
    started: str


def parse_proc_stat(text: str) -> ProcStat | None:
    """Parse fields used from Linux /proc/<pid>/stat, including parenthesized comm."""
    open_idx = text.find("(")
    close_idx = text.rfind(")")
    if open_idx <= 0 or close_idx <= open_idx:
        return None
    pid = int(text[:open_idx].strip())
    rest = text[close_idx + 2 :].strip().split()
    if len(rest) < 20:
        return None
    state = rest[0]
    parent_pid = int(rest[1])
    pgrp = int(rest[2])
    session = int(rest[3])
    tty_device = int(rest[4])
    tpgid = int(rest[5])
    started = rest[19]
    if state == "" or len(state) != 1:
        return None
    return ProcStat(
        pid=pid,
        parent_pid=parent_pid,
        pgrp=pgrp,
        session=session,
        state=state,
        tty_device=tty_device,
        tpgid=tpgid,
        started=started,
    )


def _read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_dir(path: str) -> list[str] | None:
    try:
        return os.listdir(path)
    except OSError:
        return None


def _linux_device_number(value: int) -> int:
    return value & 0xFFFFFFFF


def read_linux_stat(pid: int) -> ProcStat | None:
    text = _read_file(f"/proc/{pid}/stat")
    if text is None:
        return None
    return parse_proc_stat(text)


def _quiescent(state: str | None) -> bool:
    return state is not None and state in ("Z", "X", "x")


def linux_process_group_has_live_members(process_group_id: int) -> bool | None:
    """Report whether a Linux process group has an executing member."""
    entries = _read_dir("/proc")
    if entries is None:
        return None
    matched = False
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = read_linux_stat(int(entry))
        if stat is None or stat.pgrp != process_group_id:
            continue
        matched = True
        if not _quiescent(stat.state):
            return True
    return False if matched else None


def read_syscall(pid: int, tid: int) -> tuple[int, list[int]] | None:
    """Read the system call one process is blocked in."""
    text = _read_file(f"/proc/{pid}/task/{tid}/syscall")
    if text is None:
        return None
    text = text.strip()
    if not text or text == "running" or text.startswith("-1 "):
        return None
    fields = text.split()
    try:
        number = int(fields[0])
        args = [int(f, 16) for f in fields[1:7]]
    except (ValueError, IndexError):
        return None
    return number, args


def read_linux_terminal_device(pid: int, tty_device: int, tid: int | None = None) -> int | None:
    terminal_device = _linux_device_number(tty_device)
    if terminal_device == 0:
        return None
    path = f"/proc/{pid}/fd/0" if tid is None else f"/proc/{pid}/task/{tid}/fd/0"
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target == "/dev/tty":
        return terminal_device
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if stat_mod.S_ISCHR(st.st_mode) and _linux_device_number(st.st_rdev) == terminal_device:
        return terminal_device
    return None


# Syscall tables for Linux architectures
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
    "aarch64": {"read": 63, "pselect": 72, "ppoll": 73, "epoll_pwait": 22},
}


def _fd_set_has_stdin(pid: int, address: int) -> bool:
    if address == 0:
        return False
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            f.seek(address)
            data = f.read(8)
            if not data:
                return False
            return data[0] % 2 == 1
    except OSError:
        return False


def _poll_has_stdin(pid: int, address: int, count: int) -> bool:
    if address == 0 or count <= 0:
        return False
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            f.seek(address)
            data = f.read(min(count, 1024) * 8)
            if not data:
                return False
            for offset in range(0, len(data) - 7, 8):
                if data[offset] == 0 and data[offset + 4] & 0x01:
                    # fd 0 in pollfd
                    pass
            # Simplified: check if any pollfd has fd==0 and POLLIN
            for offset in range(0, len(data) - 7, 8):
                fd_val = int.from_bytes(data[offset : offset + 4], "little", signed=True)
                events = int.from_bytes(data[offset + 4 : offset + 6], "little")
                if fd_val == 0 and (events & 0x001):
                    return True
            return False
    except OSError:
        return False


def _epoll_has_stdin(pid: int, tid: int, epfd: int) -> bool:
    try:
        with open(f"/proc/{pid}/task/{tid}/fdinfo/{epfd}") as f:
            for line in f:
                line = line.strip()
                if re.match(r"^tfd:\s+0\b", line):
                    return True
    except OSError:
        pass
    return False


def syscall_waits_on_stdin(
    pid: int, tid: int, syscall_num: int, args: list[int], arch: str = "x86_64"
) -> bool:
    table = SYSCALL_TABLES.get(arch)
    if table is None:
        # Try all tables
        for t in SYSCALL_TABLES.values():
            result = _check_syscall_table(t, pid, tid, syscall_num, args)
            if result:
                return True
        return False
    return _check_syscall_table(table, pid, tid, syscall_num, args)


def _check_syscall_table(
    table: dict[str, int], pid: int, tid: int, syscall_num: int, args: list[int]
) -> bool:
    a0 = args[0] if len(args) > 0 else 0
    a1 = args[1] if len(args) > 1 else 0
    a2 = args[2] if len(args) > 2 else 0

    if syscall_num == table.get("read") and a0 == 0:
        return True
    if syscall_num in (table.get("select", -1), table.get("pselect", -1)):
        if a0 >= 1:
            return _fd_set_has_stdin(pid, a1)
    if syscall_num in (table.get("poll", -1), table.get("ppoll", -1)):
        if a1 >= 1:
            return _poll_has_stdin(pid, a0, a1)
    if syscall_num in (table.get("epoll_wait", -1), table.get("epoll_pwait", -1)):
        if a2 >= 1:
            return _epoll_has_stdin(pid, tid, a0)
    return False


class ProcessInspector:
    """Platform process inspector for DeepSpace."""

    def __init__(self, platform: str = "linux", arch: str = "x86_64"):
        self.platform = platform
        self.arch = arch

    def foreground_pgid(self, shell_pid: int) -> int | None:
        if self.platform == "linux":
            stat = read_linux_stat(shell_pid)
            if stat is None:
                return None
            tpgid = stat.tpgid
            return tpgid if tpgid > 0 else None
        # macOS: use ps
        if self.platform == "darwin":
            try:
                import subprocess

                out = subprocess.check_output(
                    ["/bin/ps", "-o", "tpgid=", "-p", str(shell_pid)],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                val = int(out)
                return val if val > 0 else None
            except (ValueError, subprocess.SubprocessError):
                return None
        return None

    def is_stdin_waiting(self, pgid: int, shell_pid: int) -> bool:
        if self.platform != "linux":
            return False
        shell_stat = read_linux_stat(shell_pid)
        if shell_stat is None:
            return False
        terminal_device = read_linux_terminal_device(shell_pid, shell_stat.tty_device)
        if terminal_device is None:
            return False
        entries = _read_dir("/proc")
        if entries is None:
            return False
        for entry in entries:
            if not entry.isdigit():
                continue
            proc_stat = read_linux_stat(int(entry))
            if proc_stat is None or proc_stat.pgrp != pgid:
                continue
            tids = _read_dir(f"/proc/{entry}/task")
            if tids is None:
                continue
            for tid in tids:
                if not tid.isdigit():
                    continue
                syscall = read_syscall(int(entry), int(tid))
                if syscall is None:
                    continue
                num, args = syscall
                if syscall_waits_on_stdin(int(entry), int(tid), num, args, self.arch):
                    td = read_linux_terminal_device(int(entry), proc_stat.tty_device, int(tid))
                    if td == terminal_device:
                        return True
        return False

    def snapshot(self) -> dict[str, Any]:
        """Read the process table once."""
        if self.platform == "linux":
            pids = _read_dir("/proc")
            if pids is None:
                raise RuntimeError("Cannot inspect processes: /proc directory is unreadable")
            rows = []
            complete = True
            for entry in pids:
                if not entry.isdigit():
                    continue
                stat = read_linux_stat(int(entry))
                if stat is None:
                    complete = False
                    continue
                rows.append(
                    {
                        "pid": stat.pid,
                        "parent_pid": stat.parent_pid,
                        "started": stat.started,
                        "session": stat.session,
                        "state": stat.state,
                    }
                )
            return {"rows": rows, "complete": complete, "platform": "linux"}
        if self.platform == "darwin":
            import subprocess as sp

            try:
                out = sp.check_output(
                    ["/bin/ps", "-axo", "pid=,ppid=,lstart="],
                    text=True,
                    stderr=sp.DEVNULL,
                )
            except sp.SubprocessError as exc:
                raise RuntimeError("Cannot inspect processes") from exc
            rows = []
            complete = True
            for line in out.splitlines():
                m = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$", line)
                if m is None:
                    if line.strip():
                        complete = False
                    continue
                rows.append(
                    {
                        "pid": int(m.group(1)),
                        "parent_pid": int(m.group(2)),
                        "started": m.group(3),
                        "session": None,
                        "state": None,
                    }
                )
            return {"rows": rows, "complete": complete, "platform": "darwin"}
        raise RuntimeError(f"Unsupported platform: {self.platform}")

    def is_alive(self, identity: ProcessIdentity) -> bool:
        if self.platform == "linux":
            stat = read_linux_stat(identity.pid)
            if stat is None:
                return False
            return stat.started == identity.started and stat.state not in ("Z", "X", "x")
        # macOS
        snap = self.snapshot()
        for row in snap["rows"]:
            if row["pid"] == identity.pid and row["started"] == identity.started:
                return True
        return False

    def signal_group(self, pgid: int, signal_name: str) -> None:
        import signal as sig_mod

        sig = getattr(sig_mod, signal_name, None)
        if sig is None:
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            pass

    def signal_process(self, identity: ProcessIdentity, signal_name: str) -> None:
        import signal as sig_mod

        sig = getattr(sig_mod, signal_name, None)
        if sig is None:
            return
        if not self.is_alive(identity):
            return
        try:
            os.kill(identity.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            pass

    def tree(self, root_pid: int, snapshot: dict[str, Any] | None = None) -> list[ProcessIdentity]:
        if snapshot is None:
            snapshot = self.snapshot()
        rows = snapshot["rows"]
        by_pid = {r["pid"]: r for r in rows}
        root = by_pid.get(root_pid)
        if root is None:
            return []
        by_parent: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_parent.setdefault(r["parent_pid"], []).append(r)
        visited: set[int] = set()
        result: list[ProcessIdentity] = []

        def visit(entry: dict[str, Any]) -> None:
            if entry["pid"] in visited:
                return
            visited.add(entry["pid"])
            for child in by_parent.get(entry["pid"], []):
                visit(child)
            result.append(ProcessIdentity(pid=entry["pid"], started=entry["started"]))

        visit(root)
        return result

    def session_members(
        self, session_id: int, snapshot: dict[str, Any] | None = None
    ) -> list[ProcessIdentity]:
        if snapshot is None:
            snapshot = self.snapshot()
        return [
            ProcessIdentity(pid=r["pid"], started=r["started"])
            for r in snapshot["rows"]
            if r.get("session") == session_id
        ]


def create_process_inspector(
    platform: str | None = None, arch: str | None = None
) -> ProcessInspector:
    import platform as plat

    if platform is None:
        platform = (
            "linux"
            if plat.system() == "Linux"
            else ("darwin" if plat.system() == "Darwin" else "windows")
        )
    if arch is None:
        arch = "x86_64" if plat.machine() in ("x86_64", "AMD64") else "aarch64"
    return ProcessInspector(platform=platform, arch=arch)
