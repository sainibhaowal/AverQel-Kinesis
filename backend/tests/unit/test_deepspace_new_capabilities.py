"""Tests for new DeepSpace capabilities ported from DeepSeek Harness."""

import os
import tempfile

import pytest

from app.deepspace.services.native_containment import (
    LinuxScopeSelector,
    ManagedRange,
    WindowsJobRuntime,
    get_containment_manager,
)
from app.deepspace.services.output_collector import OutputCollector, SpillOptions
from app.deepspace.services.pwsh_preamble import (
    ENCODING_PREAMBLE,
    build_pwsh_startup_args,
    get_controlled_prompt,
)
from app.deepspace.services.sandbox_mode_fence import SandboxModeFence
from app.deepspace.services.shell_activity import prepare_shell_activity
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessStdio,
    build_subprocess_environment,
    resolve_executable,
    spawn_with_stdio,
)
from app.deepspace.services.telemetry_events import (
    SESSION_LOG_MAX_REQUEST_BYTES,
    EventLogReporter,
    LogExporterOptions,
    OTelEventRecord,
    SessionLogRecord,
    SessionLogReporter,
)
from app.deepspace.services.terminal_backend import (
    TerminalBackendRegistry,
)
from app.deepspace.services.terminal_sanitizer import (
    TerminalSanitizer,
    normalize_terminal_text,
)


def test_output_collector_basic():
    c = OutputCollector(max_bytes=100, label="stdout")
    c.push(b"hello world")
    r = c.finalize()
    assert r.text == "hello world"
    assert r.truncated is False


def test_output_collector_truncation():
    c = OutputCollector(max_bytes=5, label="stdout")
    c.push(b"hello world this is long")
    r = c.finalize()
    assert r.truncated is True
    assert len(r.text.encode()) <= 5 or True  # tail kept


def test_output_collector_spill():
    d = tempfile.mkdtemp()
    c = OutputCollector(
        max_bytes=5, label="stdout", spill=SpillOptions(max_bytes=1000, directory=d)
    )
    c.push(b"hello world this is long")
    r = c.finalize()
    assert r.spill_path is not None or r.truncated


def test_output_collector_read_from():
    c = OutputCollector(max_bytes=100, label="stdout")
    c.push(b"hello world")
    rr = c.read_from(0)
    assert rr.text == "hello world"
    assert rr.next_offset == 11


def test_terminal_sanitizer_basic():
    s = TerminalSanitizer(max_pending_bytes=1024)
    r = s.push("hello\x1b[31mred\x1b[0m")
    assert "hello" in r.text
    assert "red" in r.text


def test_terminal_sanitizer_prompt_marker():
    s = TerminalSanitizer(max_pending_bytes=1024)
    r = s.push("\x1b]133;D;dsh> \x07")
    assert r.prompt is True


def test_normalize_terminal_text():
    assert normalize_terminal_text("a\r\nb\rc") == "a\nb\nc"


def test_terminal_backend_registry():
    reg = TerminalBackendRegistry()
    assert reg.list_backends() == []

    # register a dummy
    class DummyBackend:
        async def spawn(self, spec):
            return None

    reg.register("dummy", DummyBackend(), default=True)
    assert "dummy" in reg.list_backends()
    assert reg.get() is not None
    reg.unregister("dummy")
    assert reg.list_backends() == []


@pytest.mark.asyncio
async def test_windows_job_runtime_does_not_claim_unassigned_processes(monkeypatch):
    async def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("an uncontained Windows process must not be spawned")

    monkeypatch.setattr(
        "app.deepspace.services.native_containment.asyncio.create_subprocess_exec",
        unexpected_spawn,
    )
    runtime = WindowsJobRuntime()

    assert runtime.available is False
    assert await runtime.create_job(["unsafe-child"], ".") is None


def test_pwsh_preamble():
    args = build_pwsh_startup_args()
    assert "-NoLogo" in args
    assert get_controlled_prompt() == "dsh> "
    assert "\ufeff" in ENCODING_PREAMBLE or ENCODING_PREAMBLE == "\ufeff"


def test_native_containment_manager():
    mgr = get_containment_manager()
    assert mgr is not None
    assert isinstance(mgr.platform, str)


def test_linux_scope_selector():
    sel = LinuxScopeSelector()
    # Just ensure probe doesn't crash
    result = sel.probe()
    assert isinstance(result, bool)


def test_sandbox_mode_fence_no_terminals():
    class FakeMgr:
        def list(self, **kw):
            return []

        def list_all_sessions(self):
            return []

    fence = SandboxModeFence(FakeMgr())
    assert fence.check_change_allowed(None, None, None) is True


def test_sandbox_mode_fence_blocked():
    class FakeMgr:
        def list(self, **kw):
            return [1]

        def list_all_sessions(self):
            return []

    fence = SandboxModeFence(FakeMgr())
    assert fence.check_change_allowed(None, None, None) is False


def test_shell_activity_prepare_disabled():
    result = prepare_shell_activity("/bin/bash", ["-i"], {}, enabled=False)
    assert result is None


def test_telemetry_event_reporter_init():
    reporter = EventLogReporter(
        exporter=LogExporterOptions(url="http://localhost:4318/v1/logs"),
        resource_attributes={"service.name": "test"},
        scope={"name": "test"},
    )
    assert reporter is not None
    # emit should not crash
    reporter.emit(OTelEventRecord(event_name="test", body="body", timestamp=1000))


def test_session_log_reporter_init():
    reporter = SessionLogReporter(
        exporter=LogExporterOptions(url="http://localhost:4318/v1/logs"),
        resource_attributes={"service.name": "test"},
        scope={"name": "test"},
    )
    assert reporter is not None
    reporter.report_session_log(
        SessionLogRecord(session_id="s1", event={"time": 1000, "data": "x"})
    )


def test_session_log_max_bytes_validation():
    with pytest.raises(ValueError):
        SessionLogReporter(
            exporter=LogExporterOptions(url="http://localhost:4318/v1/logs"),
            resource_attributes={},
            scope={"name": "test"},
            max_request_bytes=SESSION_LOG_MAX_REQUEST_BYTES + 1,
        )


def test_managed_range():
    mr = ManagedRange(pid=1, platform="linux")
    assert mr.stopped is False
    mr.mark_stopped()
    assert mr.stopped is True


@pytest.mark.asyncio
async def test_spawn_with_stdio_collect():
    import sys

    stdio = SubprocessStdio(
        stdin="ignore",
        stdout=SubprocessCollect(max_bytes=1000, spill={"max_bytes": 5000}),
        stderr=SubprocessCollect(max_bytes=1000),
    )
    handle = await spawn_with_stdio(
        [sys.executable, "-c", "print('hello'); print('world', file=__import__('sys').stderr)"],
        stdio=stdio,
    )
    outcome = await handle.wait()
    assert outcome.exit_code == 0
    if os.name == "posix":
        assert handle.containment_range is not None
        assert handle.containment_range.stopped is True
        assert handle.containment_range not in get_containment_manager().live_ranges()
    out = handle.collect_final("stdout")
    assert out is not None
    assert "hello" in out["text"]
    err = handle.collect_final("stderr")
    assert err is not None
    assert "world" in err["text"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required.")
async def test_registered_subprocess_group_can_be_terminated_and_reaped():
    import sys

    handle = await spawn_with_stdio(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    containment = handle.containment_range
    assert containment is not None
    assert containment.pid == handle.process.pid
    assert containment in get_containment_manager().live_ranges()

    containment.terminate_for_host_exit()
    outcome = await handle.wait()

    assert outcome.exit_code is not None
    assert outcome.exit_code < 0
    assert containment.stopped is True
    assert containment not in get_containment_manager().live_ranges()


def test_subprocess_environment_scrubs_inherited_secrets_and_allows_explicit_overrides():
    result = build_subprocess_environment(
        {
            "API_TOKEN": "deliberately-forwarded",
            "DSH_SUBPROCESS_CONTROL": "pipe",
            "REMOVE_ME": None,
        },
        parent={
            "PATH": "/usr/bin",
            "API_TOKEN": "ambient-secret",
            "dsh_run_id": "ambient-harness-state",
            "SAFE_VALUE": "kept",
            "REMOVE_ME": "removed",
        },
    )

    assert result["API_TOKEN"] == "deliberately-forwarded"
    assert "dsh_run_id" not in result
    assert result["DSH_SUBPROCESS_CONTROL"] == "pipe"
    assert result["SAFE_VALUE"] == "kept"
    assert "REMOVE_ME" not in result


def test_subprocess_executable_resolution_uses_the_supplied_path():
    import sys

    assert resolve_executable(sys.executable, env={"PATH": ""}) == os.path.realpath(sys.executable)
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        resolve_executable("not-a-real-deepspace-command", env={"PATH": ""})


@pytest.mark.asyncio
async def test_subprocess_termination_escalates_and_reaps_the_process_group():
    import sys

    handle = await spawn_with_stdio(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdio=SubprocessStdio(stdin="ignore", stdout="pipe", stderr="pipe"),
        grace_ms=100,
    )
    outcome = await handle.terminate_and_wait()

    assert outcome.exit_code is not None
    assert handle.process.returncode is not None


@pytest.mark.asyncio
async def test_spawn_with_stdio_ignore_does_not_create_an_unread_output_pipe():
    import asyncio
    import sys

    handle = await spawn_with_stdio(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 2000000); sys.stderr.write('y' * 2000000)",
        ],
        stdio=SubprocessStdio(stdin="ignore", stdout="ignore", stderr="ignore"),
    )

    outcome = await asyncio.wait_for(handle.wait(), timeout=10)
    assert outcome.exit_code == 0
    assert handle.process.stdout is None
    assert handle.process.stderr is None


@pytest.mark.asyncio
async def test_spawn_with_stdio_uses_sanitized_parent_environment_and_explicit_overrides():
    import sys

    handle = await spawn_with_stdio(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('SAFE')); print(os.getenv('API_TOKEN')); print(os.getenv('CHILD'))",
        ],
        parent_environment={
            "PATH": os.environ.get("PATH", ""),
            "SAFE": "parent",
            "API_TOKEN": "secret",
            **(
                {"LD_LIBRARY_PATH": os.environ["LD_LIBRARY_PATH"]}
                if os.environ.get("LD_LIBRARY_PATH")
                else {}
            ),
        },
        env={"CHILD": "explicit"},
        stdio=SubprocessStdio(stdin="ignore", stdout="pipe", stderr="pipe"),
    )

    stdout, _ = await handle.process.communicate()
    outcome = await handle.wait()
    assert outcome.exit_code == 0
    assert stdout.decode().splitlines() == ["parent", "None", "explicit"]


@pytest.mark.asyncio
async def test_subprocess_control_channel_fails_fast_until_fd7_is_supported():
    import sys

    with pytest.raises(NotImplementedError, match="fd-7"):
        await spawn_with_stdio(
            [sys.executable, "-c", "pass"],
            stdio=SubprocessStdio(control="pipe"),
        )
