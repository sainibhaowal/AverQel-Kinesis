from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import native_containment, subprocess_stdio
from app.deepspace.services.output_collector import OutputCollector
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessHandle,
    SubprocessOutputReader,
    SubprocessStdio,
    build_subprocess_environment,
    resolve_executable,
    spawn_with_stdio,
)

pytestmark = pytest.mark.unit_no_db
_SIGTERM_NUMBER = int(getattr(signal, "SIGTERM", 15))
_SIGKILL_NUMBER = int(getattr(signal, "SIGKILL", 9))


def _use_posix_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a POSIX-specific fake-process test independent of its host OS."""
    monkeypatch.setattr(subprocess_stdio, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(subprocess_stdio, "_is_windows_runtime", lambda: False)


def test_environment_scrubbing_and_case_insensitive_explicit_overrides() -> None:
    result = build_subprocess_environment(
        {"safe": "explicit", "REMOVE_ME": None, "DSH_CHILD": "intentional"},
        parent={
            "PATH": "/usr/bin",
            "API_TOKEN": "secret",
            "SAFE": "inherited",
            "REMOVE_ME": "remove",
            "dsh_run_id": "internal",
        },
        apply_proxy_policy=False,
    )
    assert result == {
        "PATH": "/usr/bin",
        "safe": "explicit",
        "DSH_CHILD": "intentional",
    }

    inherited = build_subprocess_environment(
        parent={"PATH": "/usr/bin", "SAFE_VALUE": "kept"},
        apply_proxy_policy=False,
    )
    assert inherited["SAFE_VALUE"] == "kept"


def test_executable_resolution_handles_invalid_names_paths_and_path_lookup(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        resolve_executable("", env={"PATH": ""})
    with pytest.raises(ValueError, match="non-empty"):
        resolve_executable("bad\x00name", env={"PATH": ""})

    executable = Path(sys.executable)
    executable_directory = str(executable.parent)
    assert resolve_executable(str(executable), env={"PATH": ""}) == os.path.realpath(executable)
    assert resolve_executable(
        f".{os.sep}{executable.name}", env={"PATH": ""}, cwd=executable_directory
    ) == os.path.realpath(executable)
    assert resolve_executable(
        executable.name, env={"PATH": executable_directory}
    ) == os.path.realpath(executable)

    not_executable = tmp_path / "plain-file"
    not_executable.write_text("contents", encoding="utf-8")
    not_executable.chmod(0o644)
    with pytest.raises(FileNotFoundError, match="not executable"):
        resolve_executable(str(not_executable), env={"PATH": ""})
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        resolve_executable("missing", env={"PATH": ""})


@pytest.mark.parametrize(
    ("suffix", "pathext", "is_executable"),
    [
        (".EXE", ".EXE;.CMD", True),
        (".cmd", ".EXE;.CMD", True),
        (".txt", ".EXE;.CMD", False),
    ],
)
def test_windows_executable_resolution_uses_pathext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    pathext: str,
    is_executable: bool,
) -> None:
    monkeypatch.setattr(subprocess_stdio, "_is_windows_runtime", lambda: True)
    candidate = tmp_path / f"child{suffix}"
    candidate.write_text("payload", encoding="utf-8")

    if is_executable:
        assert resolve_executable(
            str(candidate), env={"PATH": "", "PATHEXT": pathext}
        ) == os.path.realpath(candidate)
    else:
        with pytest.raises(FileNotFoundError, match="not executable"):
            resolve_executable(str(candidate), env={"PATH": "", "PATHEXT": pathext})


class _FakeWriter:
    def __init__(self) -> None:
        self.data: list[bytes] = []
        self.closed = False
        self.drains = 0

    def write(self, value: bytes) -> None:
        self.data.append(value)

    async def drain(self) -> None:
        self.drains += 1

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 1234

    def __init__(self, *, stdout: bool = True, stderr: bool = True) -> None:
        self.stdin: _FakeWriter | None = _FakeWriter()
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None
        self.returncode: int | None = None
        self.killed = False
        self.terminated = False
        if stdout:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"standard output")
            self.stdout.feed_eof()
        if stderr:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"standard error")
            self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1 if subprocess_stdio._is_windows_runtime() else -_SIGKILL_NUMBER

    def terminate(self) -> None:
        self.terminated = True


class _FakeRange:
    def __init__(self) -> None:
        self.signals: list[str] = []
        self.stopped = False

    def signal(self, name: str) -> None:
        self.signals.append(name)

    def mark_stopped(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_spawn_routes_data_streams_and_reaps_through_managed_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_posix_runtime(monkeypatch)
    process = _FakeProcess()
    calls: list[dict[str, Any]] = []
    containment = _FakeRange()

    async def create_process(*argv: str, **kwargs: Any) -> _FakeProcess:
        calls.append({"argv": argv, **kwargs})
        return process

    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        lambda: SimpleNamespace(track_process_group=lambda _pid: containment),
    )

    handle = await spawn_with_stdio(
        ["fake-child", "--mode"],
        parent_environment={"PATH": "/bin"},
        apply_proxy_policy=False,
        stdio=SubprocessStdio(
            stdin={"data": "input"},
            stdout=SubprocessCollect(max_bytes=128, spill={"max_bytes": 512}),
            stderr=SubprocessCollect(max_bytes=128),
        ),
        spill_dir="/tmp",
    )
    outcome = await handle.wait()

    assert outcome.exit_code == 0
    assert calls[0]["start_new_session"] is True
    assert calls[0]["stdin"] == asyncio.subprocess.PIPE
    assert process.stdin is not None
    assert process.stdin.data == [b"input"]
    assert process.stdin.drains == 1
    assert process.stdin.closed is True
    assert handle.containment_range is containment
    assert containment.stopped is True
    assert handle.collected["stdout"].read_from(0).text == "standard output"
    assert handle.collected["stderr"].read_from(0).text == "standard error"
    assert "standard output" in handle.collect_final("stdout")["text"]
    assert handle.collect_final("missing") is None


@pytest.mark.asyncio
async def test_spawn_pipes_stdin_and_supports_inherited_or_ignored_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_posix_runtime(monkeypatch)
    process = _FakeProcess(stdout=False, stderr=False)
    observed: list[dict[str, Any]] = []

    async def create_process(*_argv: str, **kwargs: Any) -> _FakeProcess:
        observed.append(kwargs)
        return process

    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        lambda: SimpleNamespace(track_process_group=lambda _pid: None),
    )
    handle = await spawn_with_stdio(
        ["child"],
        apply_proxy_policy=False,
        stdio=SubprocessStdio(stdin="pipe", stdout="inherit", stderr="ignore"),
    )
    assert handle.stdin is process.stdin
    assert observed[0]["stdout"] is None
    assert observed[0]["stderr"] == asyncio.subprocess.DEVNULL
    assert observed[0]["stdin"] == asyncio.subprocess.PIPE
    assert (await handle.wait()).exit_code == 0


@pytest.mark.asyncio
async def test_collectors_handle_missing_streams_and_stderr_spill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_posix_runtime(monkeypatch)
    process_without_streams = _FakeProcess(stdout=False, stderr=False)
    process_with_stderr = _FakeProcess(stdout=True, stderr=True)
    processes = iter((process_without_streams, process_with_stderr))

    async def create_process(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        return next(processes)

    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        lambda: SimpleNamespace(track_process_group=lambda _pid: None),
    )

    empty = await spawn_with_stdio(
        ["child"],
        apply_proxy_policy=False,
        stdio=SubprocessStdio(
            stdout=SubprocessCollect(max_bytes=10),
            stderr=SubprocessCollect(max_bytes=10),
        ),
    )
    assert empty.collect_final("stdout") is None
    assert empty.collect_final("stderr") is None
    await empty.wait()

    stderr = await spawn_with_stdio(
        ["child"],
        apply_proxy_policy=False,
        stdio=SubprocessStdio(
            stdout=SubprocessCollect(max_bytes=100),
            stderr=SubprocessCollect(max_bytes=100, spill={"max_bytes": 500}),
        ),
    )
    await stderr.wait()
    assert stderr.collected["stderr"].read_from(0).text == "standard error"


@pytest.mark.asyncio
async def test_spawn_validation_fails_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    launches = 0

    async def forbidden_launch(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        nonlocal launches
        launches += 1
        return _FakeProcess()

    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", forbidden_launch)
    with pytest.raises(ValueError, match="stream_limit"):
        await spawn_with_stdio([sys.executable], stream_limit=True)
    with pytest.raises(ValueError, match="control"):
        await spawn_with_stdio([sys.executable], stdio=SubprocessStdio(control="bad"))
    with pytest.raises(NotImplementedError, match="fd-7"):
        await spawn_with_stdio([sys.executable], stdio=SubprocessStdio(control="pipe"))
    with pytest.raises(ValueError, match="argv"):
        await spawn_with_stdio([])
    for stdio, message in (
        (SubprocessStdio(stdin="bad"), "stdin"),
        (SubprocessStdio(stdout="bad"), "stdout"),
        (SubprocessStdio(stderr="bad"), "stderr"),
    ):
        with pytest.raises(ValueError, match=message):
            await spawn_with_stdio([sys.executable], stdio=stdio)
    assert launches == 0


@pytest.mark.asyncio
async def test_spawn_degrades_when_posix_containment_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_posix_runtime(monkeypatch)
    process = _FakeProcess()

    async def create_process(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        lambda: (_ for _ in ()).throw(RuntimeError("containment unavailable")),
    )
    handle = await spawn_with_stdio(
        ["child"], apply_proxy_policy=False, stdio=SubprocessStdio(stdout="ignore")
    )
    assert handle.containment_range is None
    assert (await handle.wait()).exit_code == 0


@pytest.mark.asyncio
async def test_spawn_kills_child_if_windows_containment_cannot_be_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=False, stderr=False)

    async def create_process(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr(subprocess_stdio, "_is_windows_runtime", lambda: True)
    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        lambda: SimpleNamespace(
            assign_suspended_process=lambda _process: (_ for _ in ()).throw(
                RuntimeError("no Job Object")
            )
        ),
    )
    with pytest.raises(RuntimeError, match="Job Object"):
        await spawn_with_stdio(
            ["child"], apply_proxy_policy=False, stdio=SubprocessStdio(stdout="ignore")
        )
    assert process.killed is True
    assert process.returncode == 0


def test_reader_and_handle_cover_final_output_and_unusual_exit_codes() -> None:
    collector = OutputCollector(max_bytes=100, label="stdout")
    collector.push(b"captured")
    reader = SubprocessOutputReader(collector)
    assert reader.read_from(0).text == "captured"

    class Process:
        pid = 8

        async def wait(self) -> int:
            return -999

    async def exercise() -> None:
        handle = SubprocessHandle(process=Process())  # type: ignore[arg-type]
        outcome = await handle.wait()
        assert outcome.exit_code == -999
        assert outcome.signal is None

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_termination_uses_windows_and_posix_fallbacks_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_stdio.signal, "SIGTERM", _SIGTERM_NUMBER, raising=False)
    monkeypatch.setattr(subprocess_stdio.signal, "SIGKILL", _SIGKILL_NUMBER, raising=False)

    class WaitingProcess:
        pid = 45

        def __init__(self, *, kill_exit_code: int) -> None:
            self.exited = asyncio.Event()
            self.killed = False
            self.terminated = False
            self.kill_exit_code = kill_exit_code

        async def wait(self) -> int:
            await self.exited.wait()
            return self.kill_exit_code

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.exited.set()

    windows = WaitingProcess(kill_exit_code=1)
    monkeypatch.setattr(subprocess_stdio, "os", SimpleNamespace(name="nt"))
    windows_handle = SubprocessHandle(process=windows, _grace_ms=0)  # type: ignore[arg-type]
    result = await windows_handle.terminate_and_wait()
    assert windows.terminated is True
    assert windows.killed is True
    assert result.exit_code == 1
    assert result.signal is None

    posix = WaitingProcess(kill_exit_code=-_SIGKILL_NUMBER)
    signals: list[tuple[int, int]] = []

    def kill_group(pid: int, value: int) -> None:
        signals.append((pid, value))
        if value == _SIGKILL_NUMBER:
            posix.exited.set()

    monkeypatch.setattr(subprocess_stdio, "os", SimpleNamespace(name="posix", killpg=kill_group))
    posix_handle = SubprocessHandle(process=posix, _grace_ms=0)  # type: ignore[arg-type]
    posix_outcome = await posix_handle.terminate_and_wait()
    assert signals == [(45, _SIGTERM_NUMBER), (45, _SIGKILL_NUMBER)]
    try:
        expected_signal = signal.Signals(_SIGKILL_NUMBER).name
    except ValueError:
        expected_signal = None
    assert posix_outcome.signal == expected_signal

    contained = WaitingProcess(kill_exit_code=1)
    process_range = _FakeRange()
    process_range.signal = lambda name: (
        process_range.signals.append(name),
        contained.exited.set() if name == "KILL" else None,
    )
    contained_handle = SubprocessHandle(
        process=contained,
        containment_range=process_range,
        _grace_ms=0,
    )  # type: ignore[arg-type]
    await contained_handle.terminate_and_wait()
    assert process_range.signals == ["TERM", "KILL"]

    disappeared = WaitingProcess(kill_exit_code=-_SIGKILL_NUMBER)
    vanished_signals: list[int] = []

    def vanished_group(_pid: int, value: int) -> None:
        vanished_signals.append(value)
        if value == _SIGKILL_NUMBER:
            disappeared.exited.set()
            raise ProcessLookupError

    monkeypatch.setattr(
        subprocess_stdio.os,
        "killpg",
        vanished_group,
        raising=False,
    )
    vanished_handle = SubprocessHandle(process=disappeared, _grace_ms=0)  # type: ignore[arg-type]
    await vanished_handle.terminate_and_wait()
    assert vanished_signals == [_SIGTERM_NUMBER, _SIGKILL_NUMBER]


def test_termination_ignores_already_exited_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 9

        @staticmethod
        def terminate() -> None:
            raise ProcessLookupError

    monkeypatch.setattr(subprocess_stdio, "os", SimpleNamespace(name="nt"))
    SubprocessHandle(process=Process()).terminate()  # type: ignore[arg-type]

    class PosixRangeProcess:
        pid = 10

    monkeypatch.setattr(
        subprocess_stdio,
        "os",
        SimpleNamespace(
            name="posix", killpg=lambda *_args: (_ for _ in ()).throw(ProcessLookupError())
        ),
    )
    SubprocessHandle(process=PosixRangeProcess()).terminate()  # type: ignore[arg-type]
