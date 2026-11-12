from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.deepspace.services import native_containment, ptc_runtime


def test_non_posix_child_uses_process_handle_without_claiming_group_containment(
    monkeypatch,
) -> None:
    process = SimpleNamespace(pid=123, kill=Mock(), terminate=Mock())
    monkeypatch.setattr(native_containment, "os", SimpleNamespace(name="nt"))

    assert native_containment.process_group_spawn_options() == {}
    assert native_containment.track_started_process(process) is None
    native_containment.terminate_process_tree(process)

    process.kill.assert_called_once_with()
    process.terminate.assert_not_called()


def test_posix_group_tracking_failure_does_not_break_launch(monkeypatch) -> None:
    process = SimpleNamespace(pid=123)
    monkeypatch.setattr(native_containment, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        native_containment,
        "get_containment_manager",
        Mock(side_effect=OSError("manager unavailable")),
    )

    assert native_containment.track_started_process(process) is None


def test_posix_group_tracking_is_available_outside_linux(monkeypatch) -> None:
    if os.name != "posix":
        pytest.skip("POSIX process groups are unavailable on this host.")
    manager = native_containment.NativeContainmentManager()
    monkeypatch.setattr(manager, "platform", "darwin")
    monkeypatch.setattr(native_containment.os, "getpgid", Mock(return_value=123))
    kill_group = Mock()
    monkeypatch.setattr(native_containment.os, "killpg", kill_group)

    managed_range = manager.track_process_group(123)

    assert managed_range is not None
    assert managed_range.platform == "posix"
    managed_range.signal("SIGTERM")
    kill_group.assert_called_once_with(123, native_containment._signal_number("SIGTERM"))
    managed_range.mark_stopped()
    assert managed_range not in manager.live_ranges()


def test_posix_group_termination_falls_back_to_process_handle(monkeypatch) -> None:
    process = SimpleNamespace(pid=123, kill=Mock(), terminate=Mock())
    fake_os = SimpleNamespace(
        name="posix",
        killpg=Mock(side_effect=ProcessLookupError),
    )
    monkeypatch.setattr(native_containment, "os", fake_os)

    native_containment.terminate_process_tree(process, force=False)

    fake_os.killpg.assert_called_once()
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_managed_linux_process_group_is_tracked_and_terminated(monkeypatch) -> None:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        pytest.skip("Native process-group tracking is implemented and verified on Linux only.")

    manager = native_containment.NativeContainmentManager()
    monkeypatch.setattr(native_containment, "_containment_manager", manager)
    options = native_containment.process_group_spawn_options()
    assert options == {"start_new_session": True}

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        **options,
    )
    managed_range = native_containment.track_started_process(process)
    try:
        assert managed_range is not None
        assert managed_range.pid == process.pid
        assert managed_range in manager.live_ranges()

        native_containment.terminate_process_tree(process)
        await asyncio.wait_for(process.wait(), timeout=3)
        managed_range.mark_stopped()

        assert managed_range.stopped
        assert managed_range not in manager.live_ranges()
    finally:
        if process.returncode is None:
            native_containment.terminate_process_tree(process)
            await asyncio.wait_for(process.wait(), timeout=3)
        if managed_range is not None:
            managed_range.mark_stopped()


@pytest.mark.asyncio
async def test_python_ptc_uses_the_shared_managed_process_launcher(
    tmp_path,
    monkeypatch,
) -> None:
    class EmptyPipe:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _size: int) -> bytes:
            return b""

    class ExitedProcess:
        pid = 123457
        returncode = 0
        stdin = None
        stdout = EmptyPipe()
        stderr = EmptyPipe()

        async def wait(self) -> int:
            return 0

    process = ExitedProcess()
    calls: list[dict[str, object]] = []
    managed_range = Mock()
    wait_calls = 0

    class FakeHandle:
        def __init__(self) -> None:
            self.process = process

        async def wait(self) -> SimpleNamespace:
            nonlocal wait_calls
            wait_calls += 1
            await process.wait()
            managed_range.mark_stopped()
            return SimpleNamespace(exit_code=0, signal=None)

        async def terminate_and_wait(self) -> SimpleNamespace:
            return await self.wait()

    async def spawn(argv: list[str], **kwargs: object) -> FakeHandle:
        calls.append({"argv": argv, **kwargs})
        return FakeHandle()

    monkeypatch.setattr(ptc_runtime, "_interpreter", lambda: sys.executable)
    monkeypatch.setattr(
        ptc_runtime,
        "build_sandbox_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            argv=["confined-python"], sandboxed=True, backend="bwrap"
        ),
    )
    monkeypatch.setattr(ptc_runtime, "spawn_with_stdio", spawn)
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=SimpleNamespace(),
        auth=SimpleNamespace(tenant_id="tenant", user_id="user"),
        sandbox_mode="workspace-write",
        cancel_check=None,
    )

    outcome = await ptc_runtime._run_program(
        context=context,
        program="pass",
        allowed_tools={},
        directory=tmp_path,
        timeout_seconds=1,
        max_tool_calls=0,
    )

    assert outcome.status == "ok"
    assert len(calls) == 1
    assert calls[0]["argv"] == ["confined-python"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["stdio"].stdin == "pipe"
    assert calls[0]["stdio"].stdout == "pipe"
    assert calls[0]["stdio"].stderr == "pipe"
    assert wait_calls == 1
    managed_range.mark_stopped.assert_called_once_with()
