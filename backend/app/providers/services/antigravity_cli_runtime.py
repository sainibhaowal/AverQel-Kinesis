from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.http_proxy import resolve_proxy_policy
from app.core.launch_environment import LaunchEnvironment

logger = logging.getLogger(__name__)

APP_SERVER_TIMEOUT_SECONDS = 20.0
LOGIN_STATUS_TIMEOUT_SECONDS = 1.5
INFERENCE_TIMEOUT_SECONDS = 300.0
MAX_PROTOCOL_LINE_BYTES = 8_000_000
MAX_CAPTURED_STATUS_BYTES = 8_192
MAX_INFERENCE_BODY_BYTES = 40_000_000
ANTIGRAVITY_CLI_PERMISSION_PROFILE = "averqel_antigravity_plan_v1"
SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
API_TO_UI_REASONING_EFFORT = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "very_high",
    "max": "extreme_high",
    "ultra": "ultra",
}


class AntigravityCliRuntimeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AntigravityCliAccountMismatchError(AntigravityCliRuntimeError):
    """The selected opaque account ID belongs to a different runtime account."""


class AntigravityCliUnavailableError(AntigravityCliRuntimeError):
    """No local Antigravity executable is available to the backend process."""


class _StreamFailure:
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        self.message = message
        self.status_code = status_code


_STREAM_END = object()


def sanitize_antigravity_process_env(env: dict[str, str]) -> dict[str, str]:
    safe_environment = {
        key: value
        for key, value in env.items()
        if not re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION)", key, re.I)
    }
    snapshot = LaunchEnvironment(
        process=safe_environment,
        project_env=Path(os.devnull),
        user_env=Path(os.devnull),
    )
    return resolve_proxy_policy(snapshot).child_environment(safe_environment)


def resolve_antigravity_executable(
    *, env: dict[str, str] | None = None, home: str | None = None, platform: str | None = None
) -> str | None:
    """Resolve only a local Antigravity CLI executable; never search project folders."""
    current_env = env or dict(os.environ)
    current_home = Path(home or Path.home()).expanduser()
    current_platform = platform or os.name
    windows = current_platform in {"nt", "win32"}
    names = ("agy.exe", "agy.cmd", "agy.bat") if windows else ("agy",)
    explicit = current_env.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if not explicit_path.is_absolute() or not explicit_path.is_file():
            return None
        if windows or os.access(explicit_path, os.X_OK):
            return str(explicit_path.resolve())
        return None

    path_value = current_env.get("PATH", "")
    candidates: list[Path] = []
    for path_entry in path_value.split(os.pathsep):
        if path_entry:
            candidates.append(Path(path_entry).expanduser())
    if windows:
        user_profile = Path(current_env.get("USERPROFILE") or current_home)
        candidates.extend(
            (
                user_profile / "AppData/Roaming/npm",
                user_profile / "AppData/Local/Programs/Antigravity",
                user_profile / "AppData/Local/Microsoft/WinGet/Links",
            )
        )
    else:
        candidates.extend(
            (
                current_home / ".local/bin",
                current_home / ".local/share/pnpm",
                current_home / ".npm-global/bin",
                current_home / ".volta/bin",
                current_home / ".bun/bin",
                current_home / ".asdf/shims",
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/usr/bin"),
            )
        )
        nvm_versions = current_home / ".nvm/versions/node"
        try:
            candidates.extend(
                entry / "bin"
                for entry in sorted(nvm_versions.iterdir(), reverse=True)[:32]
                if entry.is_dir()
            )
        except OSError:
            pass

    seen: set[str] = set()
    for candidate_directory in candidates:
        for name in names:
            resolved_path = (candidate_directory / name).resolve()
            rendered = str(resolved_path)
            if rendered in seen:
                continue
            seen.add(rendered)
            if resolved_path.is_file() and (windows or os.access(resolved_path, os.X_OK)):
                return rendered
    return None


def build_antigravity_invocation(
    executable: str, args: list[str], *, env: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    """Build a no-shell invocation, including the fixed Windows cmd shim case."""
    current_env = env or dict(os.environ)
    if os.name != "nt" or not executable.lower().endswith((".cmd", ".bat")):
        return executable, args
    if any(character in executable for character in "\r\n%!\x00"):
        raise AntigravityCliRuntimeError(
            "Antigravity CLI is installed in a path that cannot be launched safely."
        )

    def quote(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    command = " ".join([quote(executable), *(quote(value) for value in args)])
    return current_env.get("ComSpec") or current_env.get("COMSPEC") or "cmd.exe", [
        "/d",
        "/s",
        "/c",
        f'"{command}"',
    ]


def create_restricted_antigravity_cli_acp_args() -> list[str]:
    return [
        "acp",
        "--stdio",
        "--permission-profile",
        ANTIGRAVITY_CLI_PERMISSION_PROFILE,
    ]


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    captured = bytearray()
    while chunk := await stream.read(1024):
        if len(captured) < limit:
            captured.extend(chunk[: limit - len(captured)])
    return bytes(captured)


async def run_antigravity_login_status(
    executable: str,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: float = LOGIN_STATUS_TIMEOUT_SECONDS,
) -> str:
    """Return a coarse auth method without returning CLI output to API clients."""
    current_env = sanitize_antigravity_process_env(env or dict(os.environ))
    invocation_env = env or dict(os.environ)
    command, args = build_antigravity_invocation(executable, ["auth", "list"], env=invocation_env)
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=tempfile.gettempdir(),
            env=current_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        return "unavailable"
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, MAX_CAPTURED_STATUS_BYTES))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_CAPTURED_STATUS_BYTES))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except TimeoutError:
        process.kill()
        await process.wait()
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return "unavailable"
    output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    if process.returncode != 0:
        return (
            "not_authenticated"
            if re.search(
                r"not\s+(?:currently\s+)?logged\s+in|not\s+authenticated|no\s+active\s+session",
                output,
                re.I,
            )
            else "unavailable"
        )
    if re.search(r"stored|connected|active", output, re.I):
        return "authenticated"
    return "not_authenticated"


def _account_id(executable: str, email: str, *, env: dict[str, str], home: str) -> str:
    import hashlib

    antigravity_home = Path(
        env.get("ANTIGRAVITY_HOME") or (Path(home) / ".antigravity")
    ).expanduser()
    payload = f"antigravity-cli\0{Path(executable).resolve()}\0{antigravity_home.resolve()}\0{email.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def local_antigravity_cli_backend_enabled(redis_url: str) -> bool:
    """Limit direct CLI access to the standalone, embedded-Redis Kinesis backend."""
    local_desktop_runtime = (
        os.environ.get("AVERQEL_DESKTOP_PACKAGED", "").strip() == "1"
        or os.environ.get("AVERQEL_DESKTOP_LOCAL_RUNTIME", "").strip() == "1"
    )
    return (
        local_desktop_runtime
        and bool(os.environ.get("AVERQEL_HOME", "").strip())
        and redis_url.startswith("memory://")
    )


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]
ServerRequestCompleteHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ClosedHandler = Callable[[str], Awaitable[None] | None]


class AntigravityAcpClient:
    """JSON-RPC client for the Antigravity CLI ACP stdio server."""

    def __init__(
        self,
        executable: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_server_request_complete: ServerRequestCompleteHandler | None = None,
        on_closed: ClosedHandler | None = None,
        request_timeout_seconds: float = APP_SERVER_TIMEOUT_SECONDS,
    ) -> None:
        self.executable = executable
        self.cwd = cwd
        self.env = env or dict(os.environ)
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.on_server_request_complete = on_server_request_complete
        self.on_closed = on_closed
        self.request_timeout_seconds = request_timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._buffer = ""

    async def start(self) -> None:
        current_env = sanitize_antigravity_process_env(self.env)
        invocation_env = self.env or dict(os.environ)
        command, args = build_antigravity_invocation(
            self.executable,
            create_restricted_antigravity_cli_acp_args(),
            env=invocation_env,
        )
        self._process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=self.cwd,
            env=current_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        buffer = ""
        while True:
            chunk = await self._process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse Antigravity ACP message: %s", line[:200])
                    continue
                await self._handle_message(message)
        if self.on_closed:
            result = self.on_closed("server closed")
            if asyncio.iscoroutine(result):
                await result

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "method" in message and "params" in message and "id" not in message:
            # Notification from server
            if self.on_notification:
                result = self.on_notification(message["method"], message["params"])
                if asyncio.iscoroutine(result):
                    await result
        elif "id" in message and ("result" in message or "error" in message):
            # Response to our request
            request_id = message["id"]
            future = self._pending_requests.pop(request_id, None)
            if future and not future.done():
                if "error" in message:
                    future.set_exception(
                        AntigravityCliRuntimeError(
                            message["error"].get("message", "Unknown error"),
                            status_code=message["error"].get("code", 502),
                        )
                    )
                else:
                    future.set_result(message["result"])
        elif "method" in message and "id" in message:
            # Server request (e.g., tool call approval)
            request_id = message["id"]
            method = message["method"]
            params = message.get("params", {})
            if self.on_server_request:
                try:
                    callback_result = self.on_server_request(method, params)
                    if asyncio.iscoroutine(callback_result):
                        callback_result = await callback_result
                    await self._send_response(request_id, callback_result)
                except Exception as e:
                    await self._send_response(request_id, None, error=str(e))
            else:
                await self._send_response(request_id, None, error="No handler")
        if (
            self.on_server_request_complete
            and "method" in message
            and "params" in message
            and "id" not in message
        ):
            result = self.on_server_request_complete(message["method"], message["params"])
            if asyncio.iscoroutine(result):
                await result

    async def _send_response(self, request_id: int, result: Any, error: str | None = None) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        response = {"jsonrpc": "2.0", "id": request_id}
        if error:
            response["error"] = {"code": -32603, "message": error}
        else:
            response["result"] = result
        data = json.dumps(response, separators=(",", ":")) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._process is not None
        request_id = self._request_id
        self._request_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        data = json.dumps(request, separators=(",", ":")) + "\n"
        assert self._process.stdin is not None
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)
        except TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise AntigravityCliRuntimeError(
                f"Antigravity ACP request {method} timed out", status_code=504
            ) from None

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()


class AntigravityCliRuntime:
    """Manages a local Antigravity CLI ACP server for model inference."""

    def __init__(self) -> None:
        self._executable: str | None = None
        self._env: dict[str, str] | None = None
        self._home: str | None = None
        self._last_account_id: str | None = None
        self.active_streams = 0
        self.app_server_factory = AntigravityAcpClient

    def executable(self) -> str | None:
        if self._executable is None:
            self._executable = resolve_antigravity_executable(env=self._env, home=self._home)
        return self._executable

    async def get_account_info(self) -> dict[str, Any]:
        executable = self.executable()
        if not executable:
            return {
                "available": False,
                "message": "Antigravity CLI is not installed for the local Kinesis backend user.",
                "cliMessage": "Antigravity CLI not found in PATH",
            }
        current_env = self._env or dict(os.environ)
        status = await run_antigravity_login_status(executable, env=current_env)
        if status == "unavailable":
            return {
                "available": False,
                "message": "Antigravity CLI is not available.",
                "cliMessage": "Antigravity CLI login status unavailable",
            }
        if status == "not_authenticated":
            return {
                "available": False,
                "message": "Antigravity CLI is not authenticated. Run 'agy auth login' first.",
                "cliMessage": "Not logged in",
            }
        return {
            "available": True,
            "message": "Antigravity CLI is ready",
            "cliMessage": "Authenticated",
        }

    async def stream_responses(
        self, *, account_id: str, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        if self.active_streams >= 4:
            raise AntigravityCliRuntimeError(
                "Too many Antigravity requests are already running for this local account.",
                status_code=429,
            )
        self.active_streams += 1
        try:
            async for event in self._stream_responses(account_id=account_id, payload=payload):
                yield event
        finally:
            self.active_streams -= 1

    async def _stream_responses(
        self, *, account_id: str, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        executable = self.executable()
        if not executable:
            raise AntigravityCliUnavailableError(
                "Antigravity CLI is not installed for the local Kinesis backend user.",
                status_code=503,
            )
        if (
            not isinstance(payload, dict)
            or len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            > MAX_INFERENCE_BODY_BYTES
        ):
            raise AntigravityCliRuntimeError(
                "Antigravity request exceeds the maximum supported size.", status_code=413
            )
        model = payload.get("model")
        items = payload.get("input")
        if not isinstance(model, str) or not re.fullmatch(r"[\w./:-]{1,255}", model):
            raise AntigravityCliRuntimeError(
                "Antigravity request has an invalid model.", status_code=400
            )
        if not isinstance(items, list) or not 1 <= len(items) <= 256:
            raise AntigravityCliRuntimeError(
                "Antigravity request has invalid conversation input.", status_code=400
            )

        text_input, images = self._acp_input(items)
        dynamic_tools = (
            []
            if payload.get("tool_choice") == "none"
            else self._acp_dynamic_tools(payload.get("tools"))
        )
        allowed_tool_names = {
            tool["name"] for namespace in dynamic_tools for tool in namespace["tools"]
        }
        scratch_directory = tempfile.mkdtemp(prefix="averqel-antigravity-")
        try:
            os.chmod(scratch_directory, 0o700)
        except OSError:
            pass
        queue: asyncio.Queue[Any] = asyncio.Queue()
        delegated_tool_call = False
        terminal_queued = False

        async def on_notification(method: str, params: dict[str, Any]) -> None:
            nonlocal terminal_queued
            if terminal_queued:
                return
            # ACP notifications: prompt/partial, session/update, etc.
            if method == "prompt/partial" and isinstance(params.get("delta"), str):
                queue.put_nowait(
                    {
                        "type": "response.output_text.delta",
                        "delta": params["delta"],
                        "output_index": 0,
                    }
                )
            elif method == "session/update" and isinstance(params.get("status"), str):
                if params["status"] == "completed":
                    terminal_queued = True
                    if not (
                        payload.get("tool_choice") == "required"
                        and dynamic_tools
                        and not delegated_tool_call
                    ):
                        queue.put_nowait({"type": "response.completed"})
                        queue.put_nowait(_STREAM_END)
                    else:
                        queue.put_nowait(
                            _StreamFailure(
                                "Antigravity ACP could not complete this model request.",
                                status_code=502,
                            )
                        )
                        queue.put_nowait(_STREAM_END)
                elif params["status"] == "failed":
                    terminal_queued = True
                    queue.put_nowait(
                        _StreamFailure(
                            params.get("error", "Antigravity session failed"), status_code=502
                        )
                    )
                    queue.put_nowait(_STREAM_END)
            elif method == "session/tool_call" and isinstance(params.get("name"), str):
                # Tool call notification
                pass

        async def on_server_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal delegated_tool_call
            if method == "tool/call":
                name = params.get("tool")
                if not isinstance(name, str) or name not in allowed_tool_names:
                    return {
                        "success": False,
                        "content": [
                            {"type": "text", "text": "This tool is not available to AverQel."}
                        ],
                    }
                delegated_tool_call = True
                return {
                    "success": False,
                    "content": [
                        {"type": "text", "text": "AverQel will handle this function call."}
                    ],
                }
            if method in {"tool/approval", "file/approval"}:
                return {"decision": "deny"}
            return {}

        async def on_server_request_complete(method: str, params: dict[str, Any]) -> None:
            nonlocal terminal_queued
            if method != "tool/call" or terminal_queued:
                return
            name = params.get("tool")
            if not isinstance(name, str) or name not in allowed_tool_names:
                return
            raw_arguments = params.get("arguments")
            arguments = (
                raw_arguments
                if isinstance(raw_arguments, str)
                else json.dumps(
                    raw_arguments if raw_arguments is not None else {}, separators=(",", ":")
                )
            )
            call_id = params.get("callId")
            call_id = (
                call_id
                if isinstance(call_id, str) and SAFE_REQUEST_ID.fullmatch(call_id)
                else os.urandom(16).hex()
            )
            terminal_queued = True
            queue.put_nowait(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                    },
                }
            )
            queue.put_nowait(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 0,
                    "arguments": arguments[:100_000],
                }
            )
            queue.put_nowait({"type": "response.completed"})
            queue.put_nowait(_STREAM_END)

        async def on_closed(reason: str) -> None:
            if not terminal_queued:
                queue.put_nowait(_StreamFailure(reason, status_code=503))

        client: AntigravityAcpClient | None = None
        try:
            client = self.app_server_factory(
                executable,
                cwd=scratch_directory,
                env=self._env,
                on_notification=on_notification,
                on_server_request=on_server_request,
                on_server_request_complete=on_server_request_complete,
                on_closed=on_closed,
            )
            await client.start()
            # Initialize ACP session
            init_result = await client.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "capabilities": {},
                    "clientInfo": {"name": "averqel", "version": "1.0"},
                },
            )
            logger.debug("Antigravity ACP initialized: %s", init_result)

            # Create session
            session_result = await client.request(
                "session/new",
                {
                    "cwd": scratch_directory,
                    "model": model,
                },
            )
            session_id = session_result.get("sessionId") or session_result.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise AntigravityCliRuntimeError(
                    "Antigravity ACP did not create a session.", status_code=502
                )

            # Send prompt
            await client.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": {
                        "parts": [{"type": "text", "text": text_input}]
                        + [{"type": "image", "url": img} for img in images],
                    },
                },
            )
            deadline = asyncio.get_running_loop().time() + INFERENCE_TIMEOUT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise AntigravityCliRuntimeError(
                        "Antigravity model response timed out.", status_code=504
                    )
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError as exc:
                    raise AntigravityCliRuntimeError(
                        "Antigravity model response timed out.", status_code=504
                    ) from exc
                if item is _STREAM_END:
                    break
                if isinstance(item, _StreamFailure):
                    raise AntigravityCliRuntimeError(item.message, status_code=item.status_code)
                if isinstance(item, dict):
                    yield item
        finally:
            if client is not None:
                await client.close()
            shutil.rmtree(scratch_directory, ignore_errors=True)

    def _acp_input(self, items: list[Any]) -> tuple[str, list[str]]:
        transcript: list[str] = []
        images: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role == "user":
                if isinstance(content, str):
                    transcript.append(f"User: {content}")
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") in {"text", "input_text"}:
                                transcript.append(f"User: {part.get('text', '')}")
                            elif part.get("type") in {"image_url", "input_image"}:
                                image_url = part.get("image_url")
                                if isinstance(image_url, dict):
                                    images.append(image_url.get("url", ""))
                                elif isinstance(image_url, str):
                                    images.append(image_url)
            elif role == "assistant":
                if isinstance(content, str):
                    transcript.append(f"Assistant: {content}")
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") in {"text", "input_text"}:
                                transcript.append(f"Assistant: {part.get('text', '')}")
            elif role in {"system", "developer"}:
                if isinstance(content, str):
                    transcript.append(f"System: {content}")
        return "\n".join(transcript), images

    def _acp_dynamic_tools(self, tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        namespaces: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        used_names: set[str] = set()
        for entry in tools[:128]:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "namespace" and isinstance(entry.get("name"), str):
                namespace_name = entry["name"].strip()[:64]
                if not SAFE_TOOL_NAME.fullmatch(namespace_name):
                    continue
                namespace_description = entry.get("description")
                namespace = by_name.get(namespace_name)
                if namespace is None:
                    namespace = {
                        "type": "namespace",
                        "name": namespace_name,
                        "description": (
                            namespace_description[:1_000]
                            if isinstance(namespace_description, str)
                            else ""
                        ),
                        "tools": [],
                    }
                    by_name[namespace_name] = namespace
                    namespaces.append(namespace)
                functions = entry.get("tools")
                if not isinstance(functions, list):
                    continue
                for tool in functions:
                    if (
                        not isinstance(tool, dict)
                        or tool.get("type") != "function"
                        or not isinstance(tool.get("name"), str)
                        or not SAFE_TOOL_NAME.fullmatch(tool["name"])
                        or tool["name"] in used_names
                    ):
                        continue
                    used_names.add(tool["name"])
                    description = tool.get("description")
                    parameters = tool.get("parameters")
                    namespace["tools"].append(
                        {
                            "type": "function",
                            "name": tool["name"],
                            "description": (
                                description[:1_000] if isinstance(description, str) else ""
                            ),
                            "inputSchema": (
                                parameters
                                if isinstance(parameters, dict)
                                else {"type": "object", "properties": {}}
                            ),
                        }
                    )
            elif (
                entry.get("type") == "function"
                and isinstance(entry.get("name"), str)
                and SAFE_TOOL_NAME.fullmatch(entry["name"])
                and entry["name"] not in used_names
            ):
                used_names.add(entry["name"])
                namespace = by_name.get("averqel")
                if namespace is None:
                    namespace = {
                        "type": "namespace",
                        "name": "averqel",
                        "description": "Tools available to the AverQel agent.",
                        "tools": [],
                    }
                    by_name["averqel"] = namespace
                    namespaces.append(namespace)
                description = entry.get("description")
                parameters = entry.get("parameters")
                namespace["tools"].append(
                    {
                        "type": "function",
                        "name": entry["name"],
                        "description": description[:1_000] if isinstance(description, str) else "",
                        "inputSchema": (
                            parameters
                            if isinstance(parameters, dict)
                            else {"type": "object", "properties": {}}
                        ),
                    }
                )
        return [namespace for namespace in namespaces if namespace["tools"]]


antigravity_cli_runtime = AntigravityCliRuntime()
