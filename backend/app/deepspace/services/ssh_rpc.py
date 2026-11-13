"""Leased, framed JSON-RPC sessions over the reviewed OpenSSH transport."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from app.deepspace.services.ssh import (
    SshError,
    SshHost,
    build_ssh_argv,
    build_ssh_control_argv,
    prepare_trust,
    release_trust,
    remote_environment,
)
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessStdio,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)
PROTOCOL_VERSION = 1
DEFAULT_MAX_FRAME_BYTES = 2_000_000
DEFAULT_MAX_PENDING = 16
DEFAULT_REQUEST_TIMEOUT_MS = 30_000
DEFAULT_LEASE_MS = 90_000
DEFAULT_HEARTBEAT_MS = 15_000


class SshRpcError(SshError):
    """A request could not be completed over the remote helper protocol."""


class SshRpcPeer:
    """Own one authenticated helper process until lease expiry or disposal."""

    def __init__(
        self,
        *,
        host: SshHost,
        key_directory: Path,
        command: str,
        helper_sha256: str,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_pending: int = DEFAULT_MAX_PENDING,
        request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
        lease_ms: int = DEFAULT_LEASE_MS,
        heartbeat_ms: int = DEFAULT_HEARTBEAT_MS,
    ) -> None:
        self.host = host
        self.key_directory = key_directory
        self.command = command
        self.helper_sha256 = helper_sha256
        self.max_frame_bytes = max(1_024, min(int(max_frame_bytes), 8_000_000))
        self.max_pending = max(1, min(int(max_pending), 128))
        self.request_timeout_ms = max(100, min(int(request_timeout_ms), 3_600_000))
        self.lease_ms = max(1_000, min(int(lease_ms), 86_400_000))
        self.heartbeat_ms = max(250, min(int(heartbeat_ms), self.lease_ms // 2))
        self.process: asyncio.subprocess.Process | None = None
        self._handle: Any = None
        self._trust: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_semaphore = asyncio.Semaphore(self.max_pending)
        self._write_lock = asyncio.Lock()
        self._dispose_lock = asyncio.Lock()
        self._closed = False
        self._started = False
        self._last_client_activity = time.monotonic()
        self._stderr_tail = bytearray()
        self._control_path: Path | None = None
        self._exit_cleanup_registered = False

    @property
    def active(self) -> bool:
        return (
            self._started
            and not self._closed
            and self.process is not None
            and self.process.returncode is None
        )

    async def start(self) -> None:
        """Start SSH, validate helper protocol, then begin lease heartbeats."""
        if self._started:
            return
        self._trust = prepare_trust(self.host, key_directory=self.key_directory)
        self._control_path = self._trust.directory / "control.sock"
        argv = build_ssh_argv(
            self.host,
            command=self.command,
            key_path=self._trust.key_path,
            known_hosts_path=self._trust.known_hosts_path,
            control_path=self._control_path,
        )
        try:
            self._handle = await spawn_with_stdio(
                argv,
                parent_environment=remote_environment(),
                stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
                stream_limit=self.max_frame_bytes + 1,
            )
            self.process = self._handle.process
            if (
                self.process.stdin is None
                or self.process.stdout is None
                or self.process.stderr is None
            ):
                raise SshRpcError("The SSH helper process did not expose its protocol streams.")
            self._reader_task = asyncio.create_task(
                self._read_responses(), name="deepspace-ssh-rpc-reader"
            )
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(), name="deepspace-ssh-rpc-stderr"
            )
            atexit.register(self._emergency_cleanup)
            self._exit_cleanup_registered = True
            handshake = await self.request(
                "initialize",
                {"protocol_version": PROTOCOL_VERSION, "helper_sha256": self.helper_sha256},
                _starting=True,
            )
            if handshake.get("protocol_version") != PROTOCOL_VERSION:
                raise SshRpcError("The SSH helper uses an incompatible protocol version.")
            if handshake.get("helper_sha256") != self.helper_sha256:
                raise SshRpcError(
                    "The SSH helper bootstrap hash did not match the reviewed runtime."
                )
            self._started = True
            self._last_client_activity = time.monotonic()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(), name="deepspace-ssh-rpc-heartbeat"
            )
        except Exception:
            await self.dispose()
            raise

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        _starting: bool = False,
    ) -> dict[str, Any]:
        """Send one bounded request and await only its matching response id."""
        if self._closed or (not self._started and not _starting):
            raise SshRpcError("The SSH helper connection is not active.")
        await self._pending_semaphore.acquire()
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = (
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(frame) > self.max_frame_bytes:
            self._pending.pop(request_id, None)
            self._pending_semaphore.release()
            raise SshRpcError("The SSH helper request exceeds the configured frame limit.")
        process = self.process
        if process is None or process.stdin is None:
            self._pending.pop(request_id, None)
            self._pending_semaphore.release()
            raise SshRpcError("The SSH helper process is unavailable.")
        try:
            async with self._write_lock:
                process.stdin.write(frame)
                await process.stdin.drain()
            if method not in {"ping", "initialize"}:
                self._last_client_activity = time.monotonic()
            return await asyncio.wait_for(future, timeout=self.request_timeout_ms / 1000)
        except TimeoutError:
            await self.dispose()
            raise SshRpcError(f"The SSH helper request on {self.host.name!r} timed out.") from None
        except (BrokenPipeError, ConnectionError) as exc:
            await self.dispose()
            raise SshRpcError("The SSH helper connection closed while sending a request.") from exc
        finally:
            self._pending.pop(request_id, None)
            self._pending_semaphore.release()

    async def _read_responses(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                frame = await process.stdout.readline()
                if not frame:
                    raise self._connection_closed_error()
                if len(frame) > self.max_frame_bytes or not frame.endswith(b"\n"):
                    raise SshRpcError(
                        "The SSH helper sent an oversized or unterminated response frame."
                    )
                try:
                    response = json.loads(frame)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SshRpcError("The SSH helper sent invalid JSON.") from exc
                if not isinstance(response, dict) or not isinstance(response.get("id"), str):
                    raise SshRpcError("The SSH helper response did not include a request id.")
                future = self._pending.get(response["id"])
                if future is None or future.done():
                    continue
                error = response.get("error")
                if error:
                    message = str(error.get("message") if isinstance(error, dict) else error)
                    future.set_exception(SshRpcError(message[:2_000]))
                else:
                    result = response.get("result")
                    if not isinstance(result, dict):
                        future.set_exception(
                            SshRpcError("The SSH helper returned an invalid result.")
                        )
                    else:
                        future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every in-flight request gets a bounded failure.
            failure = (
                exc
                if isinstance(exc, SshRpcError)
                else SshRpcError("The SSH helper response stream failed.")
            )
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            if not self._closed:
                logger.info("SSH RPC reader closed for %s: %s", self.host.name, type(exc).__name__)

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(8_192)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > 8_192:
                del self._stderr_tail[: len(self._stderr_tail) - 8_192]

    def _connection_closed_error(self) -> SshRpcError:
        if self.process is not None and self.process.returncode == 255:
            stderr = bytes(self._stderr_tail).lower()
            if b"host key" in stderr or b"remote host identification" in stderr:
                return SshRpcError(
                    f"The host key for {self.host.name!r} does not match the reviewed key."
                )
        return SshRpcError("The SSH helper connection closed unexpectedly.")

    async def _heartbeat(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.heartbeat_ms / 1000)
                if time.monotonic() - self._last_client_activity >= self.lease_ms / 1000:
                    await self.dispose()
                    return
                await self.request("ping", {}, _starting=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.dispose()

    async def forward_tcp(
        self,
        *,
        local_port: int,
        remote_host: str,
        remote_port: int,
        bind_address: str = "127.0.0.1",
    ) -> None:
        """Ask this authenticated master to forward one loopback TCP port."""
        await self._control_forward("forward", local_port, remote_host, remote_port, bind_address)

    async def cancel_forward(
        self,
        *,
        local_port: int,
        remote_host: str,
        remote_port: int,
        bind_address: str = "127.0.0.1",
    ) -> None:
        """Cancel one forwarding rule created by this authenticated master."""
        await self._control_forward("cancel", local_port, remote_host, remote_port, bind_address)

    async def _control_forward(
        self,
        operation: str,
        local_port: int,
        remote_host: str,
        remote_port: int,
        bind_address: str,
    ) -> None:
        if not self.active or self._trust is None or self._control_path is None:
            raise SshRpcError("The SSH master connection is not active.")
        if not (1 <= int(local_port) <= 65_535 and 1 <= int(remote_port) <= 65_535):
            raise SshRpcError("SSH forwarding ports must be between 1 and 65535.")
        if bind_address not in {"127.0.0.1", "localhost", "::1"}:
            raise SshRpcError("SSH forwarding may bind only to the local loopback interface.")
        if (
            not remote_host
            or len(remote_host) > 255
            or not re.fullmatch(r"[A-Za-z0-9._:%\[\]-]+", remote_host)
        ):
            raise SshRpcError("The forwarded remote hostname is invalid.")
        spec = f"{bind_address}:{int(local_port)}:{remote_host}:{int(remote_port)}"
        argv = build_ssh_control_argv(
            self.host,
            key_path=self._trust.key_path,
            known_hosts_path=self._trust.known_hosts_path,
            control_path=self._control_path,
            operation=operation,
            forward_spec=spec,
        )
        handle = await spawn_with_stdio(
            argv,
            parent_environment=remote_environment(),
            stdio=SubprocessStdio(
                stdin="ignore",
                stdout=SubprocessCollect(max_bytes=8_000),
                stderr=SubprocessCollect(max_bytes=8_000),
            ),
        )
        try:
            result = await asyncio.wait_for(handle.wait(), timeout=10)
        except TimeoutError:
            await handle.terminate_and_wait()
            raise SshRpcError("The SSH port-forward control request timed out.") from None
        if result.exit_code != 0:
            raise SshRpcError(f"OpenSSH could not {operation} the requested loopback forward.")
        self._last_client_activity = time.monotonic()

    async def dispose(self) -> None:
        """Close streams, stop SSH with escalation, release host trust files."""
        async with self._dispose_lock:
            if self._closed:
                return
            self._closed = True
            if self._exit_cleanup_registered:
                with contextlib.suppress(Exception):
                    atexit.unregister(self._emergency_cleanup)
                self._exit_cleanup_registered = False
            current = asyncio.current_task()
            tasks = [
                task
                for task in (self._heartbeat_task, self._reader_task, self._stderr_task)
                if task is not None and task is not current
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(SshRpcError("The SSH helper connection was disposed."))
            process = self.process
            if process is not None and process.returncode is None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    if self._handle is not None:
                        await self._handle.terminate_and_wait()
                    else:
                        process.kill()
                        await process.wait()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._trust is not None:
                release_trust(self._trust)
                self._trust = None

    def _emergency_cleanup(self) -> None:
        """Best-effort process and trust cleanup during interpreter shutdown."""
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
        if self._trust is not None:
            release_trust(self._trust)
            self._trust = None


class SshRpcPool:
    """Share connections by reviewed host and event loop, with automatic leases."""

    def __init__(self) -> None:
        self._peers: dict[tuple[int, str], SshRpcPeer] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def request(
        self,
        *,
        host: SshHost,
        key_directory: Path,
        command: str,
        helper_sha256: str,
        method: str,
        params: dict[str, Any],
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_pending: int = DEFAULT_MAX_PENDING,
        request_timeout_ms: int | None = None,
        lease_ms: int = DEFAULT_LEASE_MS,
        heartbeat_ms: int = DEFAULT_HEARTBEAT_MS,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        key = (loop_id, f"{host.tenant_id}:{host.user_id}:{host.id}:{helper_sha256}:{command}")
        lock = self._locks.setdefault(loop_id, asyncio.Lock())
        async with lock:
            peer = self._peers.get(key)
            if peer is not None and not peer.active:
                await peer.dispose()
                self._peers.pop(key, None)
                peer = None
            if peer is None:
                peer = SshRpcPeer(
                    host=host,
                    key_directory=key_directory,
                    command=command,
                    helper_sha256=helper_sha256,
                    max_frame_bytes=max_frame_bytes,
                    max_pending=max_pending,
                    request_timeout_ms=request_timeout_ms
                    or max(1_000, host.timeout_seconds * 1_000),
                    lease_ms=lease_ms,
                    heartbeat_ms=heartbeat_ms,
                )
                self._peers[key] = peer
                try:
                    await peer.start()
                except Exception:
                    self._peers.pop(key, None)
                    raise
        try:
            return await peer.request(method, params)
        except Exception:
            await peer.dispose()
            async with lock:
                if self._peers.get(key) is peer:
                    self._peers.pop(key, None)
            raise

    async def close_all(self) -> None:
        """Dispose all helper sessions owned by this process."""
        peers = tuple(self._peers.values())
        self._peers.clear()
        await asyncio.gather(*(peer.dispose() for peer in peers), return_exceptions=True)


_SSH_RPC_POOL = SshRpcPool()


def ssh_rpc_pool() -> SshRpcPool:
    """Return the process-wide SSH RPC connection pool."""
    return _SSH_RPC_POOL


__all__ = ["SshRpcError", "SshRpcPeer", "SshRpcPool", "ssh_rpc_pool"]
