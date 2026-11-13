"""Branch-focused, offline tests for the SSH JSON-RPC transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import ssh_rpc

pytestmark = pytest.mark.unit_no_db


class _Stdin:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.frames: list[bytes] = []
        self.failure = failure
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.failure is not None:
            raise self.failure
        self.frames.append(data)

    async def drain(self) -> None:
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed = True


class _BlockingReader:
    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


class _BlockingStderr:
    async def read(self, _: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class _Process:
    def __init__(
        self,
        *,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        returncode: int | None = 0,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _Handle:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.terminated = False

    async def terminate_and_wait(self) -> None:
        self.terminated = True
        self.process.returncode = -15


def _peer(tmp_path: Path, **kwargs: Any) -> ssh_rpc.SshRpcPeer:
    return ssh_rpc.SshRpcPeer(
        host=SimpleNamespace(name="rpc-test"),
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
        **kwargs,
    )


def _active_peer(tmp_path: Path) -> ssh_rpc.SshRpcPeer:
    peer = ssh_rpc.SshRpcPeer(
        host=_host(),
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
    )
    peer._started = True
    peer._trust = SimpleNamespace(key_path=tmp_path / "key", known_hosts_path=tmp_path / "known")
    peer._control_path = tmp_path / "control.sock"
    peer.process = _Process(returncode=None)
    return peer


def _host() -> SimpleNamespace:
    return SimpleNamespace(
        name="pool-test",
        tenant_id="tenant",
        user_id="user",
        id="host",
        timeout_seconds=5,
        destination="user@example.invalid",
        port=22,
    )


@pytest.mark.asyncio
async def test_start_is_idempotent_and_rejects_missing_protocol_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path)
    peer._started = True
    await peer.start()

    trust = SimpleNamespace(directory=tmp_path, key_path=None, known_hosts_path=tmp_path / "known")
    released: list[object] = []
    monkeypatch.setattr(ssh_rpc, "prepare_trust", lambda *args, **kwargs: trust)
    monkeypatch.setattr(ssh_rpc, "release_trust", released.append)
    monkeypatch.setattr(ssh_rpc, "build_ssh_argv", lambda *args, **kwargs: ["ssh"])
    monkeypatch.setattr(ssh_rpc.atexit, "register", lambda _: None)
    monkeypatch.setattr(ssh_rpc.atexit, "unregister", lambda _: None)

    async def spawn(*args: Any, **kwargs: Any) -> _Handle:
        del args, kwargs
        return _Handle(_Process(stdin=None))

    monkeypatch.setattr(ssh_rpc, "spawn_with_stdio", spawn)
    with pytest.raises(ssh_rpc.SshRpcError, match="protocol streams"):
        await _peer(tmp_path).start()
    assert released == [trust]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handshake", "message"),
    [
        ({"protocol_version": 99, "helper_sha256": "a" * 64}, "incompatible protocol"),
        ({"protocol_version": 1, "helper_sha256": "b" * 64}, "bootstrap hash"),
    ],
)
async def test_start_releases_trust_when_handshake_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handshake: dict[str, Any],
    message: str,
) -> None:
    trust = SimpleNamespace(directory=tmp_path, key_path=None, known_hosts_path=tmp_path / "known")
    released: list[object] = []
    process = _Process(stdin=_Stdin(), stdout=_BlockingReader(), stderr=_BlockingStderr())
    monkeypatch.setattr(ssh_rpc, "prepare_trust", lambda *args, **kwargs: trust)
    monkeypatch.setattr(ssh_rpc, "release_trust", released.append)
    monkeypatch.setattr(ssh_rpc, "build_ssh_argv", lambda *args, **kwargs: ["ssh"])
    monkeypatch.setattr(ssh_rpc, "remote_environment", lambda: {})

    async def spawn(*args: Any, **kwargs: Any) -> _Handle:
        del args, kwargs
        return _Handle(process)

    monkeypatch.setattr(ssh_rpc, "spawn_with_stdio", spawn)
    monkeypatch.setattr(ssh_rpc.atexit, "register", lambda _: None)
    monkeypatch.setattr(ssh_rpc.atexit, "unregister", lambda _: None)
    peer = _peer(tmp_path)

    async def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return handshake

    monkeypatch.setattr(peer, "request", rejected)
    with pytest.raises(ssh_rpc.SshRpcError, match=message):
        await peer.start()
    assert peer._closed is True
    assert released == [trust]


@pytest.mark.asyncio
async def test_start_cleans_up_when_spawning_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust = SimpleNamespace(directory=tmp_path, key_path=None, known_hosts_path=tmp_path / "known")
    released: list[object] = []
    monkeypatch.setattr(ssh_rpc, "prepare_trust", lambda *args, **kwargs: trust)
    monkeypatch.setattr(ssh_rpc, "release_trust", released.append)
    monkeypatch.setattr(ssh_rpc, "build_ssh_argv", lambda *args, **kwargs: ["ssh"])
    monkeypatch.setattr(ssh_rpc, "remote_environment", lambda: {})

    async def fail_spawn(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("spawn denied")

    monkeypatch.setattr(ssh_rpc, "spawn_with_stdio", fail_spawn)
    with pytest.raises(OSError, match="spawn denied"):
        await _peer(tmp_path).start()
    assert released == [trust]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["process", "stdin"])
async def test_request_releases_capacity_if_process_or_stdin_is_missing(
    tmp_path: Path, missing: str
) -> None:
    peer = _peer(tmp_path, max_pending=2)
    peer._started = True
    peer.process = None if missing == "process" else _Process(stdin=None)
    with pytest.raises(ssh_rpc.SshRpcError, match="process is unavailable"):
        await peer.request("fs", {}, _starting=True)
    assert peer._pending == {}
    assert peer._pending_semaphore._value == 2


@pytest.mark.asyncio
async def test_request_timeout_and_broken_pipe_dispose_the_peer(tmp_path: Path) -> None:
    timeout_peer = _peer(tmp_path, request_timeout_ms=100)
    timeout_peer.process = _Process(stdin=_Stdin())
    timeout_peer._started = True
    with pytest.raises(ssh_rpc.SshRpcError, match="timed out"):
        await timeout_peer.request("slow", {})
    assert timeout_peer._closed is True

    broken_peer = _peer(tmp_path)
    broken_peer.process = _Process(stdin=_Stdin(failure=BrokenPipeError("closed")))
    broken_peer._started = True
    with pytest.raises(ssh_rpc.SshRpcError, match="connection closed while sending") as error:
        await broken_peer.request("write", {})
    assert isinstance(error.value.__cause__, BrokenPipeError)
    assert broken_peer._closed is True


@pytest.mark.asyncio
async def test_request_none_params_are_encoded_as_empty_object(tmp_path: Path) -> None:
    peer = _peer(tmp_path)
    stdin = _Stdin()
    peer.process = _Process(stdin=stdin)
    peer._started = True

    async def finish(future: asyncio.Future[dict[str, Any]], timeout: float) -> dict[str, Any]:
        del timeout
        frame = json.loads(stdin.frames[-1])
        assert frame["params"] == {}
        future.set_result({"accepted": True})
        return await future

    original_wait_for = ssh_rpc.asyncio.wait_for

    async def wait_for(awaitable: Any, timeout: float) -> Any:
        if isinstance(awaitable, asyncio.Future):
            return await finish(awaitable, timeout)
        return await original_wait_for(awaitable, timeout)

    # asyncio is shared by the module; restore it before the test exits.
    ssh_rpc.asyncio.wait_for = wait_for
    try:
        assert await peer.request("ping", None) == {"accepted": True}
    finally:
        ssh_rpc.asyncio.wait_for = original_wait_for


@pytest.mark.asyncio
async def test_reader_skips_unknown_and_already_completed_ids_and_accepts_result(
    tmp_path: Path,
) -> None:
    peer = _peer(tmp_path)
    future = asyncio.get_running_loop().create_future()
    future.set_result({"already": True})
    target = asyncio.get_running_loop().create_future()
    peer._pending.update({"done": future, "target": target})

    class Reader:
        def __init__(self) -> None:
            self.frames = [
                b'{"id":"unknown","result":{}}\n',
                b'{"id":"done","result":{}}\n',
                b'{"id":"target","result":{"ok":true}}\n',
                b"",
            ]

        async def readline(self) -> bytes:
            return self.frames.pop(0)

    peer.process = _Process(stdout=Reader())
    await peer._read_responses()
    assert await target == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b"[]\n", b'{"id":4,"result":{}}\n'])
async def test_reader_rejects_non_object_or_non_string_ids(tmp_path: Path, frame: bytes) -> None:
    peer = _peer(tmp_path)

    class Reader:
        async def readline(self) -> bytes:
            return frame

    peer.process = _Process(stdout=Reader())
    await peer._read_responses()


@pytest.mark.asyncio
async def test_reader_maps_generic_stream_failure_and_string_remote_errors(tmp_path: Path) -> None:
    peer = _peer(tmp_path)
    future = asyncio.get_running_loop().create_future()

    class BrokenReader:
        async def readline(self) -> bytes:
            raise RuntimeError("private transport detail")

    peer._pending["id"] = future
    peer.process = _Process(stdout=BrokenReader())
    await peer._read_responses()
    with pytest.raises(ssh_rpc.SshRpcError, match="response stream failed"):
        await future

    peer = _peer(tmp_path)
    future = asyncio.get_running_loop().create_future()
    peer._pending["id"] = future

    class ErrorReader:
        def __init__(self) -> None:
            self.frames = [b'{"id":"id","error":"denied"}\n', b""]

        async def readline(self) -> bytes:
            return self.frames.pop(0)

    peer.process = _Process(stdout=ErrorReader())
    await peer._read_responses()
    with pytest.raises(ssh_rpc.SshRpcError, match="denied"):
        await future


@pytest.mark.asyncio
async def test_reader_passes_cancellation_and_connection_close_variants(tmp_path: Path) -> None:
    peer = _peer(tmp_path)

    class CancelledReader:
        async def readline(self) -> bytes:
            raise asyncio.CancelledError

    peer.process = _Process(stdout=CancelledReader())
    with pytest.raises(asyncio.CancelledError):
        await peer._read_responses()

    assert "unexpectedly" in str(peer._connection_closed_error())
    peer.process = _Process(returncode=255)
    assert "unexpectedly" in str(peer._connection_closed_error())
    peer._stderr_tail.extend(b"REMOTE HOST IDENTIFICATION HAS CHANGED")
    assert "does not match" in str(peer._connection_closed_error())


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", [None, "missing-process"])
async def test_reader_returns_when_process_or_stdout_is_missing(
    tmp_path: Path, stdout: Any
) -> None:
    peer = _peer(tmp_path)
    if stdout != "missing-process":
        peer.process = _Process(stdout=stdout)
    await peer._read_responses()


@pytest.mark.asyncio
async def test_reader_does_not_log_after_disposal(tmp_path: Path) -> None:
    peer = _peer(tmp_path)
    peer._closed = True

    class BrokenReader:
        async def readline(self) -> bytes:
            raise RuntimeError("expected close")

    peer.process = _Process(stdout=BrokenReader())
    await peer._read_responses()


@pytest.mark.asyncio
async def test_stderr_drain_is_optional_and_retains_only_last_8192_bytes(tmp_path: Path) -> None:
    peer = _peer(tmp_path)
    await peer._drain_stderr()
    peer.process = _Process(stderr=None)
    await peer._drain_stderr()

    class Stderr:
        def __init__(self) -> None:
            self.chunks = [b"small", b"a" * 9_000, b""]

        async def read(self, _: int) -> bytes:
            return self.chunks.pop(0)

    peer.process = _Process(stderr=Stderr())
    await peer._drain_stderr()
    assert len(peer._stderr_tail) == 8_192
    assert peer._stderr_tail == b"a" * 8_192


@pytest.mark.asyncio
async def test_heartbeat_disposes_on_request_failure_and_propagates_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path)
    disposed: list[bool] = []

    async def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise ssh_rpc.SshRpcError("ping failed")

    async def dispose() -> None:
        disposed.append(True)
        peer._closed = True

    monkeypatch.setattr(peer, "request", request)
    monkeypatch.setattr(peer, "dispose", dispose)
    monkeypatch.setattr(ssh_rpc.asyncio, "sleep", lambda _: _done())
    await peer._heartbeat()
    assert disposed == [True]

    peer = _peer(tmp_path)

    async def cancelled_sleep(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(ssh_rpc.asyncio, "sleep", cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await peer._heartbeat()


async def _done() -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"_started": False},
        {"_trust": None},
        {"_control_path": None},
    ],
)
async def test_forward_requires_active_master_and_trust(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    peer = _active_peer(tmp_path)
    for name, value in changes.items():
        setattr(peer, name, value)
    with pytest.raises(ssh_rpc.SshRpcError, match="master connection is not active"):
        await peer.forward_tcp(local_port=1234, remote_host="localhost", remote_port=4321)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_port", "remote_host", "remote_port", "bind_address", "message"),
    [
        (0, "localhost", 80, "127.0.0.1", "ports"),
        (1234, "localhost", 65_536, "127.0.0.1", "ports"),
        (1234, "localhost", 80, "0.0.0.0", "loopback"),
        (1234, "", 80, "127.0.0.1", "hostname"),
        (1234, "h" * 256, 80, "127.0.0.1", "hostname"),
        (1234, "bad host", 80, "127.0.0.1", "hostname"),
    ],
)
async def test_forward_rejects_invalid_ports_addresses_and_hostnames(
    tmp_path: Path,
    local_port: int,
    remote_host: str,
    remote_port: int,
    bind_address: str,
    message: str,
) -> None:
    peer = _active_peer(tmp_path)
    with pytest.raises(ssh_rpc.SshRpcError, match=message):
        await peer.forward_tcp(
            local_port=local_port,
            remote_host=remote_host,
            remote_port=remote_port,
            bind_address=bind_address,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exit"])
async def test_forward_control_timeout_and_nonzero_exit_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    peer = _active_peer(tmp_path)

    class Handle:
        terminated = False

        async def wait(self) -> Any:
            if failure == "timeout":
                raise TimeoutError
            return SimpleNamespace(exit_code=7)

        async def terminate_and_wait(self) -> None:
            self.terminated = True

    handle = Handle()

    async def spawn(*args: Any, **kwargs: Any) -> Handle:
        del args, kwargs
        return handle

    monkeypatch.setattr(ssh_rpc, "spawn_with_stdio", spawn)
    monkeypatch.setattr(ssh_rpc, "remote_environment", lambda: {})
    expected = "timed out" if failure == "timeout" else "could not forward"
    with pytest.raises(ssh_rpc.SshRpcError, match=expected):
        await peer.forward_tcp(local_port=1234, remote_host="localhost", remote_port=4321)
    assert handle.terminated is (failure == "timeout")


@pytest.mark.asyncio
async def test_dispose_is_idempotent_cancels_tasks_and_fails_pending_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path)
    trust = object()
    released: list[object] = []
    monkeypatch.setattr(ssh_rpc, "release_trust", released.append)
    peer._trust = trust
    peer._exit_cleanup_registered = True
    monkeypatch.setattr(ssh_rpc.atexit, "unregister", lambda _: None)
    done_task = asyncio.create_task(_done())
    await done_task
    waiting = asyncio.create_task(asyncio.Event().wait())
    peer._reader_task = done_task
    peer._stderr_task = waiting
    pending = asyncio.get_running_loop().create_future()
    completed = asyncio.get_running_loop().create_future()
    completed.set_result({"ok": True})
    peer._pending.update({"pending": pending, "done": completed})
    peer.process = _Process(returncode=None)

    await peer.dispose()
    assert waiting.cancelled()
    with pytest.raises(ssh_rpc.SshRpcError, match="disposed"):
        await pending
    assert released == [trust]
    await peer.dispose()


@pytest.mark.asyncio
async def test_dispose_waits_for_process_without_stdin(tmp_path: Path) -> None:
    peer = _peer(tmp_path)
    process = _Process(stdin=None, returncode=None)
    peer.process = process
    await peer.dispose()
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("escalation", ["handle", "kill"])
async def test_dispose_escalates_when_child_does_not_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escalation: str
) -> None:
    peer = _peer(tmp_path)
    process = _Process(stdin=_Stdin(), returncode=None)
    handle = _Handle(process)
    peer.process = process
    peer._handle = handle if escalation == "handle" else None

    async def timeout(awaitable: Any, *, timeout: float) -> Any:
        del timeout
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(ssh_rpc.asyncio, "wait_for", timeout)
    await peer.dispose()
    assert process.stdin.closed
    assert handle.terminated is (escalation == "handle")
    assert process.killed is (escalation == "kill")


def test_emergency_cleanup_kills_process_and_releases_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path)
    trust = object()
    released: list[object] = []
    killed: list[tuple[int, int]] = []
    process = _Process(returncode=None)
    peer.process = process
    peer._trust = trust
    monkeypatch.setattr(ssh_rpc, "release_trust", released.append)
    monkeypatch.setattr(ssh_rpc.os, "name", "posix")
    monkeypatch.setattr(ssh_rpc.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    peer._emergency_cleanup()
    assert killed == [(process.pid, ssh_rpc.signal.SIGTERM)]
    assert released == [trust]
    assert peer._trust is None

    process.returncode = 0
    peer._emergency_cleanup()
    assert released == [trust]
    second_trust = object()
    peer.process = _Process(returncode=None)
    peer._trust = second_trust
    monkeypatch.setattr(ssh_rpc.os, "name", "nt")
    peer._emergency_cleanup()
    assert released == [trust, second_trust]
    assert peer.process.killed is True
    assert peer._trust is None


@pytest.mark.asyncio
async def test_pool_evicts_inactive_peer_and_removes_peer_when_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Any] = []

    class Peer:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.active = False
            self.started = False
            self.disposed = 0

        async def start(self) -> None:
            self.started = True
            if fail_start:
                raise ssh_rpc.SshRpcError("startup rejected")
            self.active = True

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del method, params
            return {"ok": True}

        async def dispose(self) -> None:
            self.disposed += 1
            self.active = False

    fail_start = False

    def make_peer(**kwargs: Any) -> Peer:
        peer = Peer(**kwargs)
        created.append(peer)
        return peer

    monkeypatch.setattr(ssh_rpc, "SshRpcPeer", make_peer)
    pool = ssh_rpc.SshRpcPool()
    kwargs = dict(
        host=_host(),
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
        method="fs",
        params={},
        request_timeout_ms=2500,
    )
    key = (id(asyncio.get_running_loop()), "tenant:user:host:" + "a" * 64 + ":helper")
    stale = Peer()
    pool._peers[key] = stale  # type: ignore[assignment]
    assert await pool.request(**kwargs) == {"ok": True}
    assert stale.disposed == 1
    assert len(created) == 1

    fail_start = True
    kwargs["command"] = "other-helper"
    with pytest.raises(ssh_rpc.SshRpcError, match="startup rejected"):
        await pool.request(**kwargs)
    failed_key = (key[0], "tenant:user:host:" + "a" * 64 + ":other-helper")
    assert failed_key not in pool._peers


@pytest.mark.asyncio
async def test_pool_keeps_replacement_peer_and_accepts_empty_close_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = ssh_rpc.SshRpcPool()
    host = _host()
    key = (id(asyncio.get_running_loop()), "tenant:user:host:" + "a" * 64 + ":helper")

    class FailingPeer:
        active = True

        async def start(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del method, params
            pool._peers[key] = replacement  # type: ignore[assignment]
            raise ssh_rpc.SshRpcError("request failed")

        async def dispose(self) -> None:
            return None

    class Replacement:
        active = True

        async def dispose(self) -> None:
            return None

    replacement = Replacement()
    monkeypatch.setattr(ssh_rpc, "SshRpcPeer", lambda **kwargs: FailingPeer())
    with pytest.raises(ssh_rpc.SshRpcError, match="request failed"):
        await pool.request(
            host=host,
            key_directory=tmp_path,
            command="helper",
            helper_sha256="a" * 64,
            method="fs",
            params={},
        )
    assert pool._peers[key] is replacement
    pool._peers.clear()
    await pool.close_all()


def test_global_pool_accessor_is_stable() -> None:
    assert ssh_rpc.ssh_rpc_pool() is ssh_rpc.ssh_rpc_pool()
