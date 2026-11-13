from __future__ import annotations

import asyncio
import ctypes
import io
import json
import signal
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.deepspace.services import (
    native_containment,
    subprocess_stdio,
    terminals,
    windows_job,
    windows_pty_host,
)

pytestmark = pytest.mark.unit_no_db


class _FakeJob:
    def __init__(self) -> None:
        self.terminated = 0
        self.closed = False

    def terminate(self, exit_code: int = 1) -> None:
        self.terminated = exit_code

    def close(self) -> None:
        self.closed = True


class _FakeController:
    def __init__(self) -> None:
        self.job = _FakeJob()
        self.assigned: list[int] = []

    def create(self) -> _FakeJob:
        return self.job

    def assign_and_resume(self, _job: _FakeJob, process: Any) -> None:
        self.assigned.append(process.pid)

    def assign_pid(self, _job: _FakeJob, process_id: int) -> None:
        self.assigned.append(process_id)


def test_windows_job_range_owns_signal_and_close_lifecycle() -> None:
    runtime = native_containment.WindowsJobRuntime.__new__(native_containment.WindowsJobRuntime)
    controller = _FakeController()
    runtime._controller = controller

    managed = runtime.assign_suspended_process(type("Process", (), {"pid": 123})())

    assert managed.platform == "windows"
    assert controller.assigned == [123]
    managed.signal("TERM")
    assert controller.job.terminated == 1
    managed.mark_stopped()
    assert controller.job.closed is True
    assert managed not in native_containment._LIVE_RANGES


def test_windows_job_assigns_suspended_child_before_resuming_primary_thread() -> None:
    events: list[str] = []
    kernel32 = type("FakeKernel32", (), {})()
    kernel32.CreateJobObjectW = Mock(side_effect=lambda *_: 100)
    kernel32.SetInformationJobObject = Mock(side_effect=lambda *_: True)
    kernel32.AssignProcessToJobObject = Mock(side_effect=lambda *_: events.append("assign") or True)
    kernel32.TerminateJobObject = Mock(return_value=True)
    kernel32.CloseHandle = Mock(side_effect=lambda handle: events.append(f"close:{handle}"))
    kernel32.CreateToolhelp32Snapshot = Mock(return_value=200)

    def first_thread(_snapshot: int, entry_pointer: Any) -> bool:
        entry = ctypes.cast(entry_pointer, ctypes.POINTER(windows_job._ThreadEntry32)).contents
        entry.th32OwnerProcessID = 42
        entry.th32ThreadID = 43
        return True

    kernel32.Thread32First = Mock(side_effect=first_thread)
    kernel32.Thread32Next = Mock(return_value=False)
    kernel32.OpenThread = Mock(return_value=300)
    kernel32.OpenProcess = Mock(return_value=400)
    kernel32.ResumeThread = Mock(side_effect=lambda _thread: events.append("resume") or 1)

    controller = windows_job.WindowsJobController(kernel32=kernel32)
    job = controller.create()

    class FakeTransport:
        @staticmethod
        def get_extra_info(name: str) -> Any:
            assert name == "subprocess"
            return type("FakePopen", (), {"_handle": 500})()

    process = type("FakeAsyncProcess", (), {"pid": 42, "_transport": FakeTransport()})()
    controller.assign_and_resume(job, process)

    assert events.index("assign") < events.index("resume")
    assert "close:200" in events
    assert "close:300" in events
    assert kernel32.ResumeThread.call_args.args == (300,)
    job.close()
    assert job.closed is True


def _kernel32_mock() -> Any:
    kernel32 = SimpleNamespace()
    for name in (
        "CreateJobObjectW",
        "AssignProcessToJobObject",
        "SetInformationJobObject",
        "TerminateJobObject",
        "CloseHandle",
        "CreateToolhelp32Snapshot",
        "Thread32First",
        "Thread32Next",
        "OpenThread",
        "ResumeThread",
        "OpenProcess",
    ):
        setattr(kernel32, name, Mock())
    return kernel32


def test_windows_job_reports_unavailable_controller_and_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        windows_job.ctypes,
        "WinDLL",
        Mock(side_effect=OSError("not Windows")),
        raising=False,
    )
    with pytest.raises(windows_job.WindowsJobError, match="only on Windows"):
        windows_job.WindowsJobController()

    monkeypatch.setattr(windows_job.ctypes, "get_last_error", lambda: 5, raising=False)
    assert "Windows error 5" in str(windows_job._last_error("failed"))


def test_windows_job_create_and_assignment_failures_close_owned_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(windows_job.ctypes, "get_last_error", raising=False)
    kernel32 = _kernel32_mock()
    controller = windows_job.WindowsJobController(kernel32=kernel32)
    kernel32.CreateJobObjectW.return_value = 0
    with pytest.raises(windows_job.WindowsJobError, match="could not be created"):
        controller.create()

    kernel32.CreateJobObjectW.return_value = 100
    kernel32.SetInformationJobObject.return_value = False
    with pytest.raises(windows_job.WindowsJobError, match="kill-on-close"):
        controller.create()
    kernel32.CloseHandle.assert_called_once_with(100)

    job = windows_job.WindowsJob(kernel32, 200)
    kernel32.AssignProcessToJobObject.return_value = False
    with pytest.raises(windows_job.WindowsJobError, match="Windows error 0"):
        job.assign(300)

    job.terminate(9)
    kernel32.TerminateJobObject.assert_called_once_with(200, 9)
    job.close()
    job.close()
    kernel32.CloseHandle.assert_any_call(200)
    with pytest.raises(windows_job.WindowsJobError, match="already closed"):
        job.assign(300)
    job.terminate(10)
    assert kernel32.TerminateJobObject.call_count == 1


@pytest.mark.parametrize("resume_result", [0, 0xFFFFFFFF])
def test_windows_job_rejects_unresumable_suspended_process_and_closes_thread(
    resume_result: int,
) -> None:
    kernel32 = _kernel32_mock()
    kernel32.CreateToolhelp32Snapshot.return_value = 200

    def first_thread(_snapshot: int, entry_pointer: Any) -> bool:
        entry = ctypes.cast(entry_pointer, ctypes.POINTER(windows_job._ThreadEntry32)).contents
        entry.th32OwnerProcessID = 42
        entry.th32ThreadID = 43
        return True

    kernel32.Thread32First.side_effect = first_thread
    kernel32.Thread32Next.return_value = False
    kernel32.OpenThread.return_value = 300
    kernel32.AssignProcessToJobObject.return_value = True
    kernel32.ResumeThread.return_value = resume_result
    controller = windows_job.WindowsJobController(kernel32=kernel32)
    job = windows_job.WindowsJob(kernel32, 100)

    process = SimpleNamespace(
        pid=42,
        _transport=SimpleNamespace(get_extra_info=lambda _name: SimpleNamespace(_handle=500)),
    )
    with pytest.raises(windows_job.WindowsJobError, match="could not be resumed"):
        controller.assign_and_resume(job, process)

    kernel32.CloseHandle.assert_any_call(300)
    kernel32.CloseHandle.assert_any_call(200)


def test_windows_job_requires_process_handle_and_assign_pid_closes_handle() -> None:
    kernel32 = _kernel32_mock()
    controller = windows_job.WindowsJobController(kernel32=kernel32)
    job = windows_job.WindowsJob(kernel32, 100)

    with pytest.raises(windows_job.WindowsJobError, match="handle is unavailable"):
        controller.assign_and_resume(job, SimpleNamespace(pid=42))

    kernel32.OpenProcess.return_value = 0
    with pytest.raises(windows_job.WindowsJobError, match="could not be opened"):
        controller.assign_pid(job, 42)

    kernel32.OpenProcess.return_value = 600
    kernel32.AssignProcessToJobObject.return_value = True
    controller.assign_pid(job, 42)
    kernel32.CloseHandle.assert_called_once_with(600)


def test_windows_job_finds_no_primary_thread_and_closes_snapshot() -> None:
    kernel32 = _kernel32_mock()
    kernel32.CreateToolhelp32Snapshot.return_value = 200
    thread_rows = iter([(7, 10), (42, 11)])

    def first_thread(_snapshot: int, entry_pointer: Any) -> bool:
        owner, thread_id = next(thread_rows)
        entry = ctypes.cast(entry_pointer, ctypes.POINTER(windows_job._ThreadEntry32)).contents
        entry.th32OwnerProcessID = owner
        entry.th32ThreadID = thread_id
        return True

    def next_thread(_snapshot: int, entry_pointer: Any) -> bool:
        try:
            owner, thread_id = next(thread_rows)
        except StopIteration:
            return False
        entry = ctypes.cast(entry_pointer, ctypes.POINTER(windows_job._ThreadEntry32)).contents
        entry.th32OwnerProcessID = owner
        entry.th32ThreadID = thread_id
        return True

    kernel32.Thread32First.side_effect = first_thread
    kernel32.Thread32Next.side_effect = next_thread
    kernel32.OpenThread.return_value = 0
    controller = windows_job.WindowsJobController(kernel32=kernel32)

    with pytest.raises(windows_job.WindowsJobError, match="could not be opened"):
        controller._open_primary_thread(42)

    assert kernel32.OpenThread.call_args.args[-1] == 11
    kernel32.CloseHandle.assert_called_once_with(200)


def test_windows_job_rejects_invalid_thread_snapshot() -> None:
    kernel32 = _kernel32_mock()
    kernel32.CreateToolhelp32Snapshot.return_value = (
        windows_job.WindowsJobController._INVALID_HANDLE_VALUE
    )
    controller = windows_job.WindowsJobController(kernel32=kernel32)

    with pytest.raises(windows_job.WindowsJobError, match="could not be located"):
        controller._open_primary_thread(42)

    kernel32.CloseHandle.assert_not_called()


def test_windows_pty_host_spawns_target_and_frames_output_inside_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    parent_socket, child_socket = socket.socketpair()
    child_socket.sendall(b"interactive output")
    child_socket.close()

    class FakePty:
        pid = 2468
        exitstatus = 0

        def __init__(self) -> None:
            self.fileobj = parent_socket
            self.alive = True
            self.spawn_args: tuple[Any, ...] | None = None

        @classmethod
        def spawn(cls, argv, *, cwd, env, dimensions):
            fake = cls()
            fake.spawn_args = (argv, cwd, env, dimensions)
            ptys.append(fake)
            return fake

        def isalive(self) -> bool:
            return self.alive

        def terminate(self, force: bool = False) -> bool:
            del force
            self.alive = False
            return True

        def wait(self) -> int:
            self.alive = False
            return 0

        def close(self, force: bool = False) -> None:
            del force
            self.fileobj.close()

    ptys: list[FakePty] = []
    winpty = type("FakeWinpty", (), {"PtyProcess": FakePty})()
    monkeypatch.setitem(sys.modules, "winpty", winpty)
    request = {
        "op": "start",
        "argv": ["powershell.exe"],
        "cwd": "C:/workspace",
        "env": {"PATH": "C:/Windows/System32"},
        "rows": 32,
        "columns": 100,
    }
    stdin = io.TextIOWrapper(io.BytesIO((json.dumps(request) + "\n").encode()), encoding="utf-8")
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
    monkeypatch.setattr(windows_pty_host.sys, "stdin", stdin)
    monkeypatch.setattr(windows_pty_host.sys, "stdout", stdout)

    assert windows_pty_host.run_host() == 0
    assert ptys[0].spawn_args == (
        ["powershell.exe"],
        "C:/workspace",
        {"PATH": "C:/Windows/System32"},
        (32, 100),
    )
    stdout.flush()
    raw = stdout_bytes.getvalue()
    frames = []
    offset = 0
    while offset < len(raw):
        kind, length = windows_pty_host._HEADER.unpack_from(raw, offset)
        offset += windows_pty_host._HEADER.size
        payload = raw[offset : offset + length]
        offset += length
        frames.append((kind, payload))
    assert frames == [
        (windows_pty_host._READY, struct.pack("!I", 2468)),
        (windows_pty_host._OUTPUT, b"interactive output"),
        (windows_pty_host._EXIT, struct.pack("!i", 0)),
    ]


def test_windows_pty_start_request_rejects_malformed_and_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = {
        "op": "start",
        "argv": ["cmd.exe"],
        "cwd": "C:/workspace",
        "env": {"PATH": "C:/Windows/System32"},
        "rows": 40,
        "columns": 120,
    }
    invalid_requests = [
        {},
        {**valid, "op": "write"},
        {**valid, "argv": []},
        {**valid, "argv": ["cmd.exe", 1]},
        {**valid, "env": {"PATH": None}},
        {**valid, "rows": True},
        {**valid, "columns": 501},
        {**valid, "cwd": ""},
    ]
    for request in invalid_requests:
        data = (json.dumps(request) + "\n").encode()
        monkeypatch.setattr(
            windows_pty_host.sys,
            "stdin",
            io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"),
        )
        with pytest.raises(ValueError):
            windows_pty_host._read_start_request()

    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8"),
    )
    with pytest.raises(ValueError, match="missing"):
        windows_pty_host._read_start_request()

    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b"not-json\n"), encoding="utf-8"),
    )
    with pytest.raises(ValueError):
        windows_pty_host._read_start_request()

    oversized_line = b"x" * (windows_pty_host._MAX_FRAME_BYTES + 1) + b"\n"
    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(oversized_line), encoding="utf-8"),
    )
    with pytest.raises(ValueError, match="maximum size"):
        windows_pty_host._read_start_request()


def test_windows_pty_control_protocol_handles_valid_invalid_and_oversized_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (windows_pty_host._MAX_FRAME_BYTES + 1) + b"\n"
    lines = [
        b"not-json\n",
        b"[]\n",
        b'{"op":"write","text":3}\n',
        b'{"op":"write","text":"boom"}\n',
        b'{"op":"resize","rows":true,"columns":20}\n',
        b'{"op":"resize","rows":30,"columns":90}\n',
        b'{"op":"write","text":"hello"}\n',
        b'{"op":"interrupt"}\n',
        b'{"op":"terminate","force":true}\n',
    ]
    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(payload + b"".join(lines)), encoding="utf-8"),
    )
    calls: list[tuple[object, ...]] = []

    class Pty:
        def write(self, text: str) -> None:
            if text == "boom":
                raise OSError("write failed")
            calls.append(("write", text))

        def setwinsize(self, rows: int, columns: int) -> None:
            calls.append(("resize", rows, columns))

        def sendintr(self) -> None:
            calls.append(("interrupt",))

        def terminate(self, *, force: bool) -> None:
            calls.append(("terminate", force))

        @staticmethod
        def isalive() -> bool:
            return False

    stopped = threading.Event()
    windows_pty_host._handle_controls(Pty(), threading.Lock(), stopped)
    assert calls == [
        ("resize", 30, 90),
        ("write", "hello"),
        ("interrupt",),
        ("terminate", True),
    ]
    assert stopped.is_set()

    class FailingTerminatePty:
        @staticmethod
        def isalive() -> bool:
            return True

        @staticmethod
        def terminate(*, force: bool) -> None:
            del force
            raise EOFError("already gone")

    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8"),
    )
    stopped.clear()
    windows_pty_host._handle_controls(FailingTerminatePty(), threading.Lock(), stopped)
    assert stopped.is_set()


def test_windows_pty_output_pump_frames_data_and_ignores_eof_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames: list[tuple[int, bytes]] = []
    write_frame = windows_pty_host._write_frame
    monkeypatch.setattr(
        windows_pty_host,
        "_write_frame",
        lambda kind, payload, _lock: frames.append((kind, payload)),
    )

    class Socket:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def recv(self, _size: int) -> bytes:
            chunk = next(self.chunks)
            if isinstance(chunk, BaseException):
                raise chunk
            return chunk

    class Pty:
        def __init__(self, chunks):
            self.fileobj = Socket(chunks)

    stopped = threading.Event()
    lock = threading.Lock()
    with pytest.raises(ValueError, match="maximum size"):
        write_frame(
            windows_pty_host._OUTPUT,
            b"x" * (windows_pty_host._MAX_FRAME_BYTES + 1),
            lock,
        )
    windows_pty_host._pump_output(Pty([b"one", b"two", b""]), lock, stopped)
    windows_pty_host._pump_output(Pty([EOFError()]), lock, stopped)
    windows_pty_host._pump_output(Pty([OSError()]), lock, stopped)
    stopped.set()
    windows_pty_host._pump_output(Pty([b"ignored"]), lock, stopped)
    assert frames == [
        (windows_pty_host._OUTPUT, b"one"),
        (windows_pty_host._OUTPUT, b"two"),
    ]


def test_windows_pty_host_reports_start_errors_and_clamps_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_bytes = io.BytesIO()
    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdout",
        io.TextIOWrapper(stdout_bytes, encoding="utf-8"),
    )
    monkeypatch.setattr(
        windows_pty_host,
        "_read_start_request",
        lambda: (_ for _ in ()).throw(ValueError("bad request")),
    )
    assert windows_pty_host.run_host() == 1
    raw = stdout_bytes.getvalue()
    kind, length = windows_pty_host._HEADER.unpack_from(raw)
    assert kind == windows_pty_host._ERROR
    error = json.loads(raw[windows_pty_host._HEADER.size : windows_pty_host._HEADER.size + length])
    assert error["error"] == "ValueError: bad request"

    write_frame = windows_pty_host._write_frame
    monkeypatch.setattr(windows_pty_host, "_write_frame", write_frame)
    monkeypatch.setattr(
        windows_pty_host,
        "_read_start_request",
        lambda: {
            "op": "start",
            "argv": ["cmd.exe"],
            "cwd": "C:/workspace",
            "env": {},
            "rows": 40,
            "columns": 120,
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "winpty",
        SimpleNamespace(
            PtyProcess=SimpleNamespace(
                spawn=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing"))
            )
        ),
    )
    stdout_bytes.seek(0)
    stdout_bytes.truncate(0)
    assert windows_pty_host.run_host() == 1
    kind, _length = windows_pty_host._HEADER.unpack_from(stdout_bytes.getvalue())
    assert kind == windows_pty_host._ERROR

    monkeypatch.setattr(
        windows_pty_host,
        "_write_frame",
        lambda *_args: (_ for _ in ()).throw(BrokenPipeError()),
    )
    assert windows_pty_host.run_host() == 1


def test_windows_pty_host_uses_exitstatus_when_wait_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "op": "start",
        "argv": ["cmd.exe"],
        "cwd": "C:/workspace",
        "env": {},
        "rows": 40,
        "columns": 120,
    }
    monkeypatch.setattr(windows_pty_host, "_read_start_request", lambda: request)

    class Pty:
        pid = 42
        exitstatus = 200
        fileobj = object()
        alive_checks = 0

        def isalive(self) -> bool:
            self.alive_checks += 1
            return self.alive_checks == 1

        @staticmethod
        def wait() -> int:
            raise RuntimeError("wait failed")

        @staticmethod
        def close(*, force: bool) -> None:
            del force
            raise OSError("already closed")

    monkeypatch.setitem(
        sys.modules,
        "winpty",
        type(
            "FakeWinpty",
            (),
            {"PtyProcess": type("Factory", (), {"spawn": lambda *_args, **_kwargs: Pty()})},
        )(),
    )

    class FakeThread:
        def __init__(self, **_kwargs) -> None:
            pass

        @staticmethod
        def start() -> None:
            return None

        @staticmethod
        def join(*, timeout: float) -> None:
            del timeout

    monkeypatch.setattr(windows_pty_host.threading, "Thread", FakeThread)

    class FakeEvent:
        @staticmethod
        def wait(_timeout: float) -> bool:
            return False

        @staticmethod
        def set() -> None:
            return None

    monkeypatch.setattr(windows_pty_host.threading, "Event", FakeEvent)
    output_bytes = io.BytesIO()
    monkeypatch.setattr(
        windows_pty_host.sys,
        "stdout",
        io.TextIOWrapper(output_bytes, encoding="utf-8"),
    )

    assert windows_pty_host.run_host() == 127
    raw = output_bytes.getvalue()
    kind, length = windows_pty_host._HEADER.unpack_from(raw)
    assert kind == windows_pty_host._READY
    offset = windows_pty_host._HEADER.size + length
    kind, length = windows_pty_host._HEADER.unpack_from(raw, offset)
    offset += windows_pty_host._HEADER.size
    assert (kind, struct.unpack("!i", raw[offset : offset + length])[0]) == (
        windows_pty_host._EXIT,
        127,
    )


@pytest.mark.asyncio
async def test_shared_launcher_assigns_windows_child_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeProcess:
        pid = 7654
        returncode: int | None = None
        stdin = None
        stdout = None
        stderr = None

        def __init__(self) -> None:
            self._transport = type(
                "FakeTransport",
                (),
                {"get_extra_info": lambda _self, _name: type("FakePopen", (), {"_handle": 900})()},
            )()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    process = FakeProcess()

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        calls.append({"argv": argv, **kwargs})
        return process

    class FakeRange:
        stopped = False

        def signal(self, _name: str) -> None:
            return None

        def mark_stopped(self) -> None:
            self.stopped = True

    range_obj = FakeRange()

    class FakeContainment:
        def assign_suspended_process(self, child: FakeProcess) -> FakeRange:
            assert child is process
            return range_obj

    monkeypatch.setattr(subprocess_stdio, "_is_windows_runtime", lambda: True)
    monkeypatch.setattr(subprocess_stdio, "resolve_executable", lambda command, **_kwargs: command)
    monkeypatch.setattr(subprocess_stdio.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        subprocess_stdio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(
        native_containment, "get_containment_manager", lambda: FakeContainment(), raising=False
    )

    handle = await subprocess_stdio.spawn_with_stdio(
        [sys.executable, "-c", "pass"],
        stdio=subprocess_stdio.SubprocessStdio(stdout="ignore", stderr="ignore"),
    )

    assert calls[0]["creationflags"] == 0x204
    assert calls[0]["start_new_session"] is False
    assert handle.containment_range is range_obj
    assert (await handle.wait()).exit_code == 0
    assert range_obj.stopped is True


@pytest.mark.asyncio
async def test_windows_terminal_backend_uses_job_contained_pty_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminals.sys, "platform", "win32")

    class FakeRange:
        stopped = False
        signals: list[str] = []

        def mark_stopped(self) -> None:
            self.stopped = True

        def signal(self, name: str) -> None:
            self.signals.append(name)

    range_obj = FakeRange()
    output = asyncio.StreamReader()
    output.feed_data(
        terminals._WindowsPtyHost._HEADER.pack(terminals._WindowsPtyHost._READY, 4)
        + struct.pack("!I", 12345)
    )

    class FakeProcess:
        pid = 999
        returncode: int | None = None

        def kill(self) -> None:
            self.returncode = -9

    fake_process = FakeProcess()
    control_messages: list[dict[str, Any]] = []

    class FakeWriter:
        closing = False

        def write(self, payload: bytes) -> None:
            message = json.loads(payload)
            control_messages.append(message)
            if message.get("op") == "terminate":
                fake_process.returncode = 0
                output.feed_data(
                    terminals._WindowsPtyHost._HEADER.pack(terminals._WindowsPtyHost._EXIT, 4)
                    + struct.pack("!i", 0)
                )

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return self.closing

        def close(self) -> None:
            self.closing = True

    writer = FakeWriter()

    class FakeHandle:
        process = fake_process
        stdin = writer
        stdout = output
        containment_range = range_obj

        async def wait(self):
            return None

        async def terminate_and_wait(self):
            fake_process.returncode = -9
            return None

    fake_handle = FakeHandle()
    launch_calls: list[dict[str, Any]] = []

    async def fake_spawn(argv, **kwargs):
        launch_calls.append({"argv": argv, **kwargs})
        return fake_handle

    monkeypatch.setattr("app.deepspace.services.subprocess_stdio.spawn_with_stdio", fake_spawn)

    managed = await terminals.ManagedProcess.start(
        argv=["powershell.exe"],
        shell="pwsh",
        cwd=Path.cwd(),
        env={"PATH": "/Windows/System32"},
        rows=32,
        columns=100,
    )
    assert managed.pty is True
    assert managed.process.pid == 12345
    assert managed.containment_range is range_obj
    assert launch_calls[0]["argv"][1:3] == [
        "-m",
        "app.deepspace.services.windows_pty_host",
    ]
    startup = control_messages[0]
    assert startup["argv"] == ["powershell.exe"]
    assert startup["cwd"] == str(Path.cwd())
    assert startup["env"] == {"PATH": "/Windows/System32"}
    assert (startup["rows"], startup["columns"]) == (32, 100)

    managed.resize(columns=120, rows=40)
    assert await managed.write("echo ok") == len("echo ok")
    assert managed.signal(signal.SIGINT) is True
    assert {message["op"] for message in control_messages} >= {
        "start",
        "resize",
        "write",
        "interrupt",
    }
    await managed.close()
    assert any(message.get("op") == "terminate" for message in control_messages)
    assert range_obj.stopped is True
