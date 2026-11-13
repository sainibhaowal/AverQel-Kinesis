"""Branch coverage for the mocked SSH runtime paths."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import ssh_runtime as runtime_module
from app.deepspace.services.ssh import SshHost, SshTrust
from app.deepspace.services.ssh_runtime import (
    MAX_WRITE_BYTES,
    RemoteNotFoundError,
    RemotePathRefusedError,
    SshRuntime,
    SshRuntimeError,
    SshStreamRegistry,
    SshTerminalManager,
)

pytestmark = pytest.mark.unit_no_db

HOST_KEY = "build.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial"


def _host(**overrides: object) -> SshHost:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "build",
        "hostname": "build.example.com",
        "port": 22,
        "username": "deploy",
        "known_hosts_entry": HOST_KEY,
        "key_secret_ref": None,
        "remote_workspace": "/srv/app",
        "enabled": True,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return SshHost(**values)  # type: ignore[arg-type]


class _Input:
    def __init__(
        self, *, write_error: Exception | None = None, drain_error: Exception | None = None
    ) -> None:
        self.writes: list[bytes] = []
        self.close_calls = 0
        self.write_error = write_error
        self.drain_error = drain_error

    def write(self, value: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(value)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error

    def close(self) -> None:
        self.close_calls += 1


class _Process:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        terminate_error: Exception | None = None,
        wait_error: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.terminate_error = terminate_error
        self.wait_error = wait_error
        self.terminate_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self.returncode = -15

    async def wait(self) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _Stream:
    def __init__(self, chunks: list[bytes], *, error: Exception | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    async def read(self, _size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        return b""


def _install_spawn(
    monkeypatch: pytest.MonkeyPatch,
    process: _Process,
    *,
    error: OSError | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def spawn(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        if error is not None:
            raise error
        return SimpleNamespace(process=process)

    monkeypatch.setattr(runtime_module, "spawn_with_stdio", spawn)
    return calls


@pytest.mark.parametrize(
    ("exit_code", "error_type", "message"),
    [
        (runtime_module.REMOTE_PATH_REFUSED, RemotePathRefusedError, "outside"),
        (runtime_module.REMOTE_NOT_FOUND, RemoteNotFoundError, "No such path"),
        (runtime_module.REMOTE_WRONG_KIND, SshRuntimeError, "not the kind"),
        (runtime_module.REMOTE_NO_WORKSPACE, SshRuntimeError, "could not be reached"),
        (19, SshRuntimeError, "exit code 19"),
    ],
)
def test_remote_exit_codes_map_to_their_public_errors(
    exit_code: int, error_type: type[Exception], message: str
) -> None:
    with pytest.raises(error_type, match=message):
        runtime_module._raise_for_exit(_host(), SimpleNamespace(exit_code=exit_code))


def test_successful_remote_exit_returns_without_raising() -> None:
    assert runtime_module._raise_for_exit(_host(), SimpleNamespace(exit_code=0)) is None


def test_remote_metadata_and_configuration_helpers_cover_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(deepspace_ssh_key_directory="~/ssh-keys")
    assert runtime_module.key_directory_for(settings) == Path("~/ssh-keys").expanduser()

    in_scope = SimpleNamespace(
        remote_workspace="/srv/app",
        runtime_config=SimpleNamespace(workspace="/srv/app/project"),
    )
    assert runtime_module.remote_workspace_for(in_scope) == "/srv/app/project"
    out_of_scope = SimpleNamespace(
        remote_workspace="/srv/app",
        runtime_config=SimpleNamespace(workspace="/srv/application"),
    )
    with pytest.raises(RemotePathRefusedError, match="must stay inside"):
        runtime_module.remote_workspace_for(out_of_scope)

    assert runtime_module.parse_meta("ordinary diagnostic\nDEEPSPACE_META size=4 path=a=b\n") == {
        "size": "4",
        "path": "a=b",
    }
    assert runtime_module.parse_meta("DEEPSPACE_META =ignored\n") == {}
    assert runtime_module._parse_size("42") == 42
    assert runtime_module._parse_size("not-a-size") == 0

    # The default path is intentionally distinct from the configured path.
    from app.core import averqel_home

    monkeypatch.setattr(averqel_home, "get_averqel_home", lambda: str(tmp_path / "home"))
    assert runtime_module.key_directory_for(SimpleNamespace()) == tmp_path / "home" / "ssh"


def test_helper_source_failure_is_reported_without_leaking_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_file(_path: Path) -> bytes:
        raise OSError("not present")

    monkeypatch.setattr(runtime_module.sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    monkeypatch.setattr(Path, "read_bytes", missing_file)
    with pytest.raises(SshRuntimeError, match="helper is unavailable"):
        SshRuntime._read_helper_source()


@pytest.mark.parametrize(
    ("offset", "max_bytes"),
    [(-1, 1), (0, 0), (0, 180_001)],
)
async def test_read_range_rejects_each_out_of_bounds_input(
    tmp_path: Path, offset: int, max_bytes: int
) -> None:
    with pytest.raises(SshRuntimeError, match="outside its allowed bounds"):
        await SshRuntime(key_directory=tmp_path).read_range(
            _host(), "file.bin", offset=offset, max_bytes=max_bytes
        )


@pytest.mark.parametrize("response", [{}, {"content_base64": "%%%"}])
async def test_read_range_rejects_missing_or_invalid_base64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    runtime = SshRuntime(key_directory=tmp_path)

    async def reply(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return response

    monkeypatch.setattr(runtime, "_filesystem_request", reply)
    with pytest.raises(SshRuntimeError, match="invalid byte range"):
        await runtime.read_range(_host(), "file.bin", offset=0, max_bytes=1)


async def test_generic_rpc_transport_error_becomes_a_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.ssh_rpc import SshRpcError

    class FailedPool:
        async def request(self, **_kwargs: Any) -> dict[str, Any]:
            raise SshRpcError("RPC transport disconnected")

    monkeypatch.setattr(runtime_module, "ssh_rpc_pool", lambda: FailedPool())
    with pytest.raises(SshRuntimeError, match="RPC transport disconnected"):
        await SshRuntime(key_directory=tmp_path)._filesystem_request(_host(), {"operation": "stat"})


async def test_remote_file_operation_guards_reject_the_reviewed_root_and_bad_hashes(
    tmp_path: Path,
) -> None:
    runtime = SshRuntime(key_directory=tmp_path)
    host = _host()
    with pytest.raises(SshRuntimeError, match="at most"):
        await runtime.write_bytes(host, ".", b"")
    with pytest.raises(SshRuntimeError, match="at most"):
        await runtime.write_bytes(host, "large.bin", b"x" * (MAX_WRITE_BYTES + 1))
    with pytest.raises(SshRuntimeError, match="cannot be moved"):
        await runtime.move_file(host, ".", "file")
    with pytest.raises(SshRuntimeError, match="cannot be moved"):
        await runtime.move_file(host, "file", ".")
    with pytest.raises(SshRuntimeError, match="cannot be edited"):
        await runtime.edit_file(host, ".", expected_sha256="a" * 64, old_text="", new_text="")
    with pytest.raises(SshRuntimeError, match="lowercase SHA-256"):
        await runtime.edit_file(host, "file", expected_sha256="short", old_text="", new_text="")
    with pytest.raises(SshRuntimeError, match="lowercase SHA-256"):
        await runtime.edit_file(host, "file", expected_sha256="A" * 64, old_text="", new_text="")
    with pytest.raises(SshRuntimeError, match="cannot be patched"):
        await runtime.patch_file(host, ".", expected_sha256="a" * 64, changes=[])
    with pytest.raises(SshRuntimeError, match="lowercase SHA-256"):
        await runtime.patch_file(host, "file", expected_sha256="short", changes=[])
    with pytest.raises(SshRuntimeError, match="lowercase SHA-256"):
        await runtime.patch_file(host, "file", expected_sha256="A" * 64, changes=[])
    with pytest.raises(SshRuntimeError, match="path is required"):
        await runtime.delete_file(host, ".")


async def test_read_file_covers_empty_stable_and_digest_mismatch_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SshRuntime(key_directory=tmp_path)
    host = _host()

    async def empty_stat(*_args: Any, **_kwargs: Any) -> runtime_module.RemotePathInfo:
        return runtime_module.RemotePathInfo("empty", "file", 0)

    monkeypatch.setattr(runtime, "stat_path", empty_stat)

    async def unexpected_range(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("empty files do not need range reads")

    monkeypatch.setattr(runtime, "read_range", unexpected_range)
    empty = await runtime.read_file(host, "empty")
    assert empty.content == ""
    assert empty.sha256 == hashlib.sha256(b"").hexdigest()

    payload = b"abc"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        runtime,
        "stat_path",
        lambda *_args, **_kwargs: _async_result(runtime_module.RemotePathInfo("file", "file", 3)),
    )
    monkeypatch.setattr(runtime_module, "MAX_RANGE_BYTES", 2)
    ranges = iter((b"ab", b"c"))

    async def stable_range(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"content": next(ranges), "sha256": digest}

    monkeypatch.setattr(runtime, "read_range", stable_range)
    stable = await runtime.read_file(host, "file")
    assert stable.content == "abc"
    assert stable.sha256 == digest

    async def wrong_digest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"content": b"xyz", "sha256": digest}

    monkeypatch.setattr(
        runtime,
        "stat_path",
        lambda *_args, **_kwargs: _async_result(runtime_module.RemotePathInfo("file", "file", 3)),
    )
    monkeypatch.setattr(runtime, "read_range", wrong_digest)
    with pytest.raises(SshRuntimeError, match="changed while it was being read"):
        await runtime.read_file(host, "file")


async def _async_result(value: Any) -> Any:
    return value


async def test_directory_listing_defaults_and_unpaged_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SshRuntime(key_directory=tmp_path)
    host = _host()
    calls: list[dict[str, Any]] = []

    async def minimal_reply(_host: SshHost, request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        return {"path": ".", "entries": []}

    monkeypatch.setattr(runtime, "_filesystem_request", minimal_reply)
    page = await runtime.list_directory_page(host, ".")
    assert page == {
        "path": ".",
        "entries": [],
        "offset": 0,
        "next_offset": None,
        "total_entries": 0,
        "truncated": False,
    }
    directory, entries = await runtime.list_directory(host, ".")
    assert (directory, entries) == (".", [])
    assert calls[-1]["limit"] == 1_000
    with pytest.raises(SshRuntimeError, match="path is required"):
        await runtime.delete_file(host, "./")


class _FakeTrust:
    def __init__(self, directory: Path) -> None:
        self.value = SshTrust(None, directory / "known_hosts", directory)


def _new_terminal_session(
    process: _Process,
    *,
    session_id: str = "sshterm-test",
    conversation_id: uuid.UUID | None = None,
    trust_directory: Path = Path("/tmp/ssh-runtime-test-trust"),
) -> Any:
    owner = uuid.uuid4()
    return runtime_module.SshTerminalSession(
        id=session_id,
        tenant_id=owner,
        user_id=owner,
        conversation_id=conversation_id or uuid.uuid4(),
        host=_host(),
        process=process,
        trust=_FakeTrust(trust_directory).value,
    )


@pytest.mark.parametrize("stream_error", [OSError("closed"), ValueError("invalid stream")])
async def test_drain_stream_handles_missing_streams_wakeups_and_read_errors(
    stream_error: Exception,
) -> None:
    buffer = runtime_module.OutputBuffer()
    await runtime_module._drain_stream(None, buffer)
    wakeup = asyncio.Event()
    await runtime_module._drain_stream(_Stream([b"first", b"second"]), buffer, wakeup)
    assert buffer.read(20) == "firstsecond"
    assert wakeup.is_set()

    quiet_buffer = runtime_module.OutputBuffer()
    await runtime_module._drain_stream(_Stream([b"quiet"]), quiet_buffer)
    assert quiet_buffer.read(20) == "quiet"

    error_buffer = runtime_module.OutputBuffer()
    await runtime_module._drain_stream(_Stream([b"kept"], error=stream_error), error_buffer)
    assert error_buffer.read(20) == "kept"


@pytest.mark.parametrize(
    ("write_error", "drain_error"),
    [(BrokenPipeError("pipe"), None), (None, ConnectionResetError("reset"))],
)
async def test_terminal_session_input_and_signal_error_branches(
    write_error: Exception | None, drain_error: Exception | None
) -> None:
    no_input = _new_terminal_session(_Process(stdin=None))
    with pytest.raises(SshRuntimeError, match="no input stream"):
        await no_input.write("x")
    assert no_input.signal("SIGINT") is False
    assert no_input.signal("unknown") is False
    assert no_input.signal("SIGCONT") is False

    input_stream = _Input(write_error=write_error, drain_error=drain_error)
    session = _new_terminal_session(_Process(returncode=None, stdin=input_stream))
    with pytest.raises(SshRuntimeError, match="input stream is closed"):
        await session.write("x")
    assert session.signal("SIGINT") is (write_error is None)

    broken_input = _Input(write_error=ConnectionResetError("reset"))
    session.process.stdin = broken_input
    assert session.signal("SIGINT") is False
    assert session.signal("SIGTERM") is True
    assert session.alive is False
    assert session.signal("SIGTERM") is False

    failing_process = _Process(returncode=None, terminate_error=ProcessLookupError("gone"))
    assert _new_terminal_session(failing_process).signal("SIGTERM") is False


async def test_terminal_session_successful_io_and_close_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    released: list[SshTrust] = []
    monkeypatch.setattr(runtime_module, "release_trust", released.append)
    input_stream = _Input()
    process = _Process(returncode=None, stdin=input_stream)
    session = _new_terminal_session(process, trust_directory=tmp_path)
    assert session.alive is True
    assert await session.write("é") == 2
    assert input_stream.writes == ["é".encode()]
    assert session.signal("SIGQUIT") is True
    assert input_stream.writes[-1] == b"\x1c"

    task = asyncio.create_task(asyncio.sleep(30))
    session._readers.append(task)
    await session.close()
    await session.close()
    assert task.cancelled()
    assert input_stream.close_calls == 1
    assert process.terminate_calls == 1
    assert len(released) == 1


async def test_terminal_close_suppresses_closed_pipe_termination_and_wait_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_module, "release_trust", lambda _trust: None)
    input_stream = _Input()
    input_stream.close = lambda: (_ for _ in ()).throw(BrokenPipeError("closed"))  # type: ignore[method-assign]
    process = _Process(
        returncode=None,
        stdin=input_stream,
        terminate_error=ProcessLookupError("gone"),
        wait_error=TimeoutError("still running"),
    )
    session = _new_terminal_session(process, trust_directory=tmp_path)
    await session.close()
    assert process.terminate_calls == 1
    assert session._closed is True


async def test_terminal_manager_limits_spawn_failure_and_session_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    process = _Process(returncode=None, stdin=_Input())
    _install_spawn(monkeypatch, process)
    manager = SshTerminalManager(max_sessions_per_conversation=1, max_sessions=2)
    session = await manager.open(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        host=_host(),
        key_directory=tmp_path,
        idle_timeout_seconds=3,
    )
    assert session.idle_timeout_seconds == 3
    with pytest.raises(SshRuntimeError, match="conversation already holds"):
        await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            host=_host(),
            key_directory=tmp_path,
        )
    assert manager.list(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id) == [
        session
    ]
    session.stdout.append(b"abcdef")
    read = await manager.read(
        session.id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        max_bytes=2,
    )
    assert read.output == "ab"
    assert read.truncated is True
    assert read.alive is True
    assert (
        await manager.write(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            data="ok",
        )
        == 2
    )
    assert process.stdin.writes == [b"ok"]
    assert (
        await manager.signal(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name="SIGINT",
        )
        is True
    )
    assert (
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        is True
    )
    assert (
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        is False
    )
    assert (
        await manager.close_all_for_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        == 0
    )


async def test_terminal_manager_global_limit_and_failed_spawn_release_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_process = _Process()
    _install_spawn(monkeypatch, first_process)
    manager = SshTerminalManager(max_sessions_per_conversation=2, max_sessions=1)
    await manager.open(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        host=_host(),
        key_directory=tmp_path,
    )
    with pytest.raises(SshRuntimeError, match="maximum number"):
        await manager.open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            host=_host(),
            key_directory=tmp_path,
        )
    await manager.close_all()

    released: list[SshTrust] = []
    original_release = runtime_module.release_trust

    def record_release(trust: SshTrust) -> None:
        released.append(trust)
        original_release(trust)

    monkeypatch.setattr(runtime_module, "release_trust", record_release)
    _install_spawn(monkeypatch, _Process(), error=OSError("ssh missing"))
    with pytest.raises(SshRuntimeError, match="could not be started"):
        await SshTerminalManager().open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            host=_host(),
            key_directory=tmp_path,
        )
    assert len(released) == 1
    assert not released[0].directory.exists()


async def test_terminal_manager_closes_every_owned_conversation_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SshTerminalManager()
    tenant_id, user_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for _ in range(2):
        _install_spawn(monkeypatch, _Process())
        await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            host=_host(),
            key_directory=tmp_path,
        )

    assert (
        await manager.close_all_for_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        == 2
    )
    assert await manager.close_all() == 0


async def test_stream_registry_reads_waits_enforces_ownership_and_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stdout.feed_data(b"abcdef")
    stdout.feed_eof()
    stderr.feed_data(b"diagnostic")
    stderr.feed_eof()
    process = _Process(returncode=None, stdout=stdout, stderr=stderr)
    _install_spawn(monkeypatch, process)
    registry = SshStreamRegistry(max_sessions=1, idle_timeout_seconds=30)
    owner = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    session = await registry.open(
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
        host=_host(),
        command="echo test",
        key_directory=tmp_path,
    )
    await asyncio.sleep(0)
    with pytest.raises(SshRuntimeError, match="maximum number"):
        await registry.open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            host=_host(),
            command="echo second",
            key_directory=tmp_path,
        )
    with pytest.raises(runtime_module.SshTerminalNotFoundError):
        registry.get(
            session.id,
            tenant_id=uuid.uuid4(),
            user_id=owner[1],
            conversation_id=owner[2],
        )
    first = await registry.read(
        session.id,
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
        max_bytes=2,
    )
    assert first.stdout == "ab"
    assert first.stderr == "di"
    assert first.truncated is True
    process.returncode = 0
    second = await registry.read(
        session.id,
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
    )
    assert second.stdout == "cdef"
    assert second.running is False
    assert (
        await registry.close(
            session.id,
            tenant_id=uuid.uuid4(),
            user_id=owner[1],
            conversation_id=owner[2],
        )
        is False
    )
    assert (
        await registry.close(
            session.id,
            tenant_id=owner[0],
            user_id=owner[1],
            conversation_id=owner[2],
        )
        is True
    )
    assert await registry.close_all() == 0


async def test_stream_registry_wait_timeout_open_failure_and_idle_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = asyncio.StreamReader()
    process = _Process(returncode=None, stdout=stdout)
    _install_spawn(monkeypatch, process)
    registry = SshStreamRegistry(idle_timeout_seconds=10)
    owner = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    session = await registry.open(
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
        host=_host(),
        command="sleep",
        key_directory=tmp_path,
    )
    result = await registry.read(
        session.id,
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
        wait_seconds=0.001,
    )
    assert result.stdout == ""
    assert result.running is True
    session.idle_deadline = 0
    assert await registry.sweep() == [session.id]

    released: list[SshTrust] = []
    original_release = runtime_module.release_trust

    def record_release(trust: SshTrust) -> None:
        released.append(trust)
        original_release(trust)

    monkeypatch.setattr(runtime_module, "release_trust", record_release)
    _install_spawn(monkeypatch, _Process(), error=OSError("ssh missing"))
    with pytest.raises(SshRuntimeError, match="could not be started"):
        await SshStreamRegistry().open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            host=_host(),
            command="echo fail",
            key_directory=tmp_path,
        )
    assert len(released) == 1
    assert not released[0].directory.exists()


async def test_stream_session_closes_stdin_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin = _Input()
    # The close path suppresses a pipe that has already been torn down.
    stdin.close = lambda: (_ for _ in ()).throw(ConnectionResetError("closed"))  # type: ignore[method-assign]
    process = _Process(returncode=0, stdin=stdin)
    _install_spawn(monkeypatch, process)
    registry = SshStreamRegistry()
    owner = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    session = await registry.open(
        tenant_id=owner[0],
        user_id=owner[1],
        conversation_id=owner[2],
        host=_host(),
        command="echo done",
        key_directory=tmp_path,
    )
    assert (
        await registry.close(
            session.id,
            tenant_id=owner[0],
            user_id=owner[1],
            conversation_id=owner[2],
        )
        is True
    )
    await session.close()
    assert session._closed is True


def test_runtime_registries_are_process_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_TERMINAL_MANAGER", None)
    monkeypatch.setattr(runtime_module, "_STREAM_REGISTRY", None)
    terminal_manager = runtime_module.ssh_terminal_manager()
    stream_registry = runtime_module.ssh_stream_registry()
    assert runtime_module.ssh_terminal_manager() is terminal_manager
    assert runtime_module.ssh_stream_registry() is stream_registry
