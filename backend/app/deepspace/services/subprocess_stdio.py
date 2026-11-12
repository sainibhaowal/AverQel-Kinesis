"""Per-stream stdio dispositions for DeepSpace subprocess execution.

Python-native port of the DeepSeek Harness SubprocessStdio vocabulary:
- stdin: 'ignore' | 'pipe' | {data: str}
- stdout/stderr: 'pipe' | 'ignore' | 'inherit' | SubprocessCollect{max_bytes, spill?}
- control: 'pipe' (optional duplex channel)
- Offset-based readers (readFrom) for collected streams
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.http_proxy import resolve_proxy_policy
from app.core.launch_environment import LaunchEnvironment
from app.deepspace.services.output_collector import OutputCollector, SpillOptions

logger = logging.getLogger(__name__)
_SENSITIVE_ENV = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)
_CREATE_SUSPENDED = 0x00000004


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def build_subprocess_environment(
    explicit: Mapping[str, str | None] | None = None,
    *,
    parent: dict[str, str] | None = None,
    apply_proxy_policy: bool = True,
) -> dict[str, str]:
    """Scrub inherited credentials and harness state, then apply trusted overrides.

    Explicit entries are an intentional opt-in and therefore survive the
    inherited-environment scrub. Proxy variables are normalized using the
    same policy as DeepSpace HTTP clients.
    """
    inherited = os.environ.copy() if parent is None else dict(parent)
    env = {
        key: value
        for key, value in inherited.items()
        if not _SENSITIVE_ENV.search(key) and not key.upper().startswith("DSH_")
    }
    snapshot = LaunchEnvironment(
        process=env,
        project_env=Path("/nonexistent/.env"),
        user_env=Path("/nonexistent/.env"),
    )
    if apply_proxy_policy:
        env = resolve_proxy_policy(snapshot).child_environment(env)
    for key, value in (explicit or {}).items():
        matches = [name for name in env if name.casefold() == key.casefold()]
        for name in matches:
            env.pop(name, None)
        if value is not None:
            env[key] = str(value)
    return env


def resolve_executable(command: str, *, env: dict[str, str], cwd: str | None = None) -> str:
    """Resolve a child executable against its effective PATH and working directory."""
    name = str(command).strip()
    if not name or "\x00" in name:
        raise ValueError("subprocess executable must be a non-empty path")
    has_separator = os.sep in name or (os.altsep is not None and os.altsep in name)
    if os.path.isabs(name) or has_separator:
        candidate = name if os.path.isabs(name) else os.path.join(cwd or os.getcwd(), name)
        resolved_path = os.path.realpath(candidate)
        if not os.path.isfile(resolved_path) or not os.access(resolved_path, os.X_OK):
            raise FileNotFoundError(f"subprocess executable is not executable: {name}")
        return resolved_path
    resolved_name = shutil.which(name, path=env.get("PATH"))
    if resolved_name is None:
        raise FileNotFoundError(f"subprocess executable was not found on PATH: {name}")
    return os.path.realpath(resolved_name)


@dataclass
class SubprocessCollect:
    max_bytes: int
    spill: dict[str, Any] | None = None  # {max_bytes: int}


@dataclass
class SubprocessStdio:
    stdin: str | dict[str, str] = "ignore"  # 'ignore' | 'pipe' | {'data': str}
    stdout: str | SubprocessCollect = "pipe"  # 'pipe' | 'ignore' | 'inherit' | SubprocessCollect
    stderr: str | SubprocessCollect = "pipe"
    control: str | None = None  # 'pipe' or None


@dataclass
class SubprocessOutcome:
    exit_code: int | None
    signal: str | None


@dataclass
class SubprocessOutputRead:
    text: str
    next_offset: int
    lossy: bool
    spill_path: str | None = None


class SubprocessOutputReader:
    """Offset-based reader for one collected stream."""

    def __init__(self, collector: OutputCollector) -> None:
        self._collector = collector

    def read_from(self, from_byte: int) -> SubprocessOutputRead:
        r = self._collector.read_from(from_byte)
        return SubprocessOutputRead(
            text=r.text,
            next_offset=r.next_offset,
            lossy=r.lossy,
            spill_path=r.spill_path,
        )


@dataclass
class SubprocessHandle:
    """Live subprocess handle with streams and collected output."""

    process: asyncio.subprocess.Process
    stdin: Any = None
    stdout: Any = None
    stderr: Any = None
    control: Any = None
    collected: dict[str, SubprocessOutputReader] = field(default_factory=dict)
    containment_range: Any = None
    _collectors: dict[str, OutputCollector] = field(default_factory=dict)
    _drain_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _wait_task: asyncio.Task[SubprocessOutcome] | None = None
    _grace_ms: int = 2000

    async def wait(self) -> SubprocessOutcome:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(self._wait_and_drain())
        return await asyncio.shield(self._wait_task)

    async def _wait_and_drain(self) -> SubprocessOutcome:
        try:
            exit_code = await self.process.wait()
            if self._drain_tasks:
                await asyncio.gather(*self._drain_tasks)
            for collector in self._collectors.values():
                collector.seal()
            signal_name = None
            if exit_code is not None and exit_code < 0:
                try:
                    signal_name = signal.Signals(-exit_code).name
                except ValueError:
                    signal_name = None
            return SubprocessOutcome(exit_code=exit_code, signal=signal_name)
        finally:
            if self.containment_range is not None:
                self.containment_range.mark_stopped()

    def terminate(self) -> None:
        try:
            if self.containment_range is not None:
                self.containment_range.signal("TERM")
            elif os.name == "nt":
                self.process.terminate()
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def terminate_and_wait(self) -> SubprocessOutcome:
        """Terminate the managed process group, escalating to kill after grace."""
        self.terminate()
        try:
            return await asyncio.wait_for(self.wait(), timeout=max(0, self._grace_ms) / 1000)
        except TimeoutError:
            try:
                if self.containment_range is not None:
                    self.containment_range.signal("KILL")
                elif os.name == "nt":
                    self.process.kill()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return await self.wait()

    def collect_final(self, label: str) -> dict[str, Any] | None:
        c = self._collectors.get(label)
        if c is None:
            return None
        r = c.finalize()
        return {"text": r.text, "truncated": r.truncated, "spill_path": r.spill_path}


async def spawn_with_stdio(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    parent_environment: dict[str, str] | None = None,
    apply_proxy_policy: bool = True,
    stdio: SubprocessStdio | None = None,
    grace_ms: int = 2000,
    spill_dir: str | None = None,
    stream_limit: int = 65_536,
) -> SubprocessHandle:
    """Spawn a process with per-stream stdio dispositions."""
    if stdio is None:
        stdio = SubprocessStdio()
    if isinstance(stream_limit, bool) or not isinstance(stream_limit, int) or stream_limit <= 0:
        raise ValueError("subprocess stream_limit must be a positive integer")

    if stdio.control not in {None, "pipe"}:
        raise ValueError("subprocess control must be None or 'pipe'")
    if stdio.control == "pipe":
        raise NotImplementedError(
            "the fd-7 DSH subprocess control channel is not implemented on this platform"
        )

    child_env = build_subprocess_environment(
        env,
        parent=parent_environment,
        apply_proxy_policy=apply_proxy_policy,
    )
    if not argv:
        raise ValueError("subprocess argv must contain an executable")
    resolved_argv = [resolve_executable(argv[0], env=child_env, cwd=cwd), *argv[1:]]

    stdin_mode = stdio.stdin
    stdout_mode = stdio.stdout
    stderr_mode = stdio.stderr

    if not isinstance(stdin_mode, dict) and stdin_mode not in {"ignore", "pipe"}:
        raise ValueError("subprocess stdin must be 'ignore', 'pipe', or a data mapping")
    if isinstance(stdout_mode, str) and stdout_mode not in {"pipe", "ignore", "inherit"}:
        raise ValueError("subprocess stdout must be 'pipe', 'ignore', 'inherit', or collect")
    if isinstance(stderr_mode, str) and stderr_mode not in {"pipe", "ignore", "inherit"}:
        raise ValueError("subprocess stderr must be 'pipe', 'ignore', 'inherit', or collect")

    # Ignored streams use DEVNULL so verbose children cannot fill an unread pipe.
    stdin_pipe = (
        asyncio.subprocess.PIPE
        if stdin_mode in ("pipe",) or isinstance(stdin_mode, dict)
        else asyncio.subprocess.DEVNULL if stdin_mode == "ignore" else None
    )
    stdout_pipe = (
        {
            "pipe": asyncio.subprocess.PIPE,
            "ignore": asyncio.subprocess.DEVNULL,
            "inherit": None,
        }.get(stdout_mode, asyncio.subprocess.PIPE)
        if isinstance(stdout_mode, str)
        else asyncio.subprocess.PIPE
    )
    stderr_pipe = (
        {
            "pipe": asyncio.subprocess.PIPE,
            "ignore": asyncio.subprocess.DEVNULL,
            "inherit": None,
        }.get(stderr_mode, asyncio.subprocess.PIPE)
        if isinstance(stderr_mode, str)
        else asyncio.subprocess.PIPE
    )

    spawn_options: dict[str, Any] = {"start_new_session": not _is_windows_runtime()}
    if _is_windows_runtime():
        spawn_options["creationflags"] = int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ) | int(getattr(subprocess, "CREATE_SUSPENDED", _CREATE_SUSPENDED))
    process = await asyncio.create_subprocess_exec(
        *resolved_argv,
        cwd=cwd,
        stdin=stdin_pipe,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        env=child_env,
        limit=stream_limit,
        **spawn_options,
    )

    handle = SubprocessHandle(process=process, _grace_ms=max(0, int(grace_ms)))
    if os.name == "posix" and not _is_windows_runtime():
        try:
            from app.deepspace.services.native_containment import get_containment_manager

            handle.containment_range = get_containment_manager().track_process_group(process.pid)
        except Exception:  # noqa: BLE001 - process launch remains available without tracking
            logger.warning("Could not register subprocess for host-exit cleanup", exc_info=True)
    else:
        try:
            from app.deepspace.services.native_containment import get_containment_manager

            handle.containment_range = get_containment_manager().assign_suspended_process(process)
        except Exception:
            process.kill()
            await process.wait()
            raise

    # Handle stdin
    if isinstance(stdin_mode, dict) and process.stdin is not None:
        data = stdin_mode.get("data", "")
        process.stdin.write(data.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
    elif stdin_mode == "pipe" and process.stdin is not None:
        handle.stdin = process.stdin

    # Handle stdout
    if stdout_mode == "pipe" and process.stdout is not None:
        handle.stdout = process.stdout
    elif isinstance(stdout_mode, SubprocessCollect):
        if process.stdout is not None:
            sd = spill_dir or tempfile.gettempdir()
            spill_opts = None
            if stdout_mode.spill is not None:
                spill_opts = SpillOptions(
                    max_bytes=stdout_mode.spill.get("max_bytes", stdout_mode.max_bytes * 4),
                    directory=sd,
                )
            collector = OutputCollector(stdout_mode.max_bytes, "stdout", spill_opts)
            handle._collectors["stdout"] = collector
            handle.collected["stdout"] = SubprocessOutputReader(collector)
            # Start draining
            handle._drain_tasks.append(
                asyncio.create_task(_drain_stream(process.stdout, collector))
            )

    # Handle stderr
    if stderr_mode == "pipe" and process.stderr is not None:
        handle.stderr = process.stderr
    elif isinstance(stderr_mode, SubprocessCollect):
        if process.stderr is not None:
            sd = spill_dir or tempfile.gettempdir()
            spill_opts = None
            if stderr_mode.spill is not None:
                spill_opts = SpillOptions(
                    max_bytes=stderr_mode.spill.get("max_bytes", stderr_mode.max_bytes * 4),
                    directory=sd,
                )
            collector = OutputCollector(stderr_mode.max_bytes, "stderr", spill_opts)
            handle._collectors["stderr"] = collector
            handle.collected["stderr"] = SubprocessOutputReader(collector)
            handle._drain_tasks.append(
                asyncio.create_task(_drain_stream(process.stderr, collector))
            )

    # Reap the child even when a caller only consumes its exposed streams.
    # This also releases its host-exit process-group registration promptly.
    handle._wait_task = asyncio.create_task(handle._wait_and_drain())
    return handle


async def _drain_stream(stream: asyncio.StreamReader, collector: OutputCollector) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        collector.push(chunk)
    collector.seal()
