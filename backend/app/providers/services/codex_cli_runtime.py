from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from app.providers.services.reasoning_capabilities import normalize_reasoning_effort_value

logger = logging.getLogger(__name__)

APP_SERVER_TIMEOUT_SECONDS = 20.0
LOGIN_STATUS_TIMEOUT_SECONDS = 1.5
INFERENCE_TIMEOUT_SECONDS = 300.0
MAX_PROTOCOL_LINE_BYTES = 8_000_000
MAX_CAPTURED_STATUS_BYTES = 8_192
MAX_INFERENCE_BODY_BYTES = 40_000_000
CODEX_CLI_PERMISSION_PROFILE = "averqel_chatgpt_plan_v1"
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


class CodexCliRuntimeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class CodexCliAccountMismatchError(CodexCliRuntimeError):
    """The selected opaque account ID belongs to a different runtime account."""


class CodexCliUnavailableError(CodexCliRuntimeError):
    """No local Codex executable is available to the backend process."""


class _StreamFailure:
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        self.message = message
        self.status_code = status_code


_STREAM_END = object()


def sanitize_codex_process_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if not re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION)", key, re.I)
    }


def resolve_codex_executable(
    *, env: dict[str, str] | None = None, home: str | None = None, platform: str | None = None
) -> str | None:
    """Resolve only a local Codex CLI executable; never search project folders."""
    current_env = env or dict(os.environ)
    current_home = Path(home or Path.home()).expanduser()
    current_platform = platform or os.name
    windows = current_platform in {"nt", "win32"}
    names = ("codex.exe", "codex.cmd", "codex.bat") if windows else ("codex",)
    explicit = current_env.get("CODEX_CLI_PATH", "").strip()
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
                user_profile / "AppData/Local/Programs/Codex",
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


def build_codex_invocation(
    executable: str, args: list[str], *, env: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    """Build a no-shell invocation, including the fixed Windows cmd shim case."""
    current_env = env or dict(os.environ)
    if os.name != "nt" or not executable.lower().endswith((".cmd", ".bat")):
        return executable, args
    if any(character in executable for character in "\r\n%!\x00"):
        raise CodexCliRuntimeError(
            "Codex CLI is installed in a path that cannot be launched safely."
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


def create_restricted_codex_cli_app_server_args() -> list[str]:
    permissions = CODEX_CLI_PERMISSION_PROFILE
    return [
        "app-server",
        "--listen",
        "stdio://",
        "-c",
        f"default_permissions='{permissions}'",
        "-c",
        f"permissions.{permissions}.filesystem={{':root'='deny',':minimal'='read'}}",
        "-c",
        "features.shell_tool=false",
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        "features.hooks=false",
        "-c",
        "plugins={}",
        "-c",
        "mcp_servers={}",
        "-c",
        "web_search='disabled'",
        "-c",
        "tools.web_search=false",
        "-c",
        "tools.view_image=false",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "history.persistence='none'",
        "-c",
        "skills.config=[]",
    ]


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    captured = bytearray()
    while chunk := await stream.read(1024):
        if len(captured) < limit:
            captured.extend(chunk[: limit - len(captured)])
    return bytes(captured)


async def run_codex_login_status(
    executable: str,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: float = LOGIN_STATUS_TIMEOUT_SECONDS,
) -> str:
    """Return a coarse auth method without returning CLI output to API clients."""
    current_env = sanitize_codex_process_env(env or dict(os.environ))
    invocation_env = env or dict(os.environ)
    command, args = build_codex_invocation(executable, ["login", "status"], env=invocation_env)
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
    if re.search(r"logged in using chatgpt", output, re.I):
        return "chatgpt"
    if re.search(r"logged in using (?:an? )?api[- ]?key", output, re.I):
        return "api_key"
    if re.search(r"logged in using", output, re.I):
        return "other_authenticated_method"
    return "unavailable"


def _account_id(executable: str, email: str, *, env: dict[str, str], home: str) -> str:
    codex_home = Path(env.get("CODEX_HOME") or (Path(home) / ".codex")).expanduser()
    payload = (
        f"codex-cli\0{Path(executable).resolve()}\0{codex_home.resolve()}\0{email.strip().lower()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def local_codex_cli_backend_enabled(redis_url: str) -> bool:
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


class CodexAppServerClient:
    """Minimal bounded JSON-RPC transport for the Codex CLI app-server."""

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
        self.env = sanitize_codex_process_env(env or dict(os.environ))
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.on_server_request_complete = on_server_request_complete
        self.on_closed = on_closed
        self.request_timeout_seconds = request_timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.next_request_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.write_lock = asyncio.Lock()
        self.closed = False

    async def start(self) -> None:
        if self.process is not None:
            return
        invocation_env = dict(os.environ)
        invocation_env.update(self.env)
        command, args = build_codex_invocation(
            self.executable,
            create_restricted_codex_cli_app_server_args(),
            env=invocation_env,
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                command,
                *args,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_PROTOCOL_LINE_BYTES + 1,
            )
        except (OSError, ValueError) as exc:
            raise CodexCliRuntimeError(
                "Codex CLI app-server could not start.", status_code=503
            ) from exc
        self.reader_task = asyncio.create_task(self._read_stdout())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "averqel", "title": "AverQel", "version": "0.1.6"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_request_id
        self.next_request_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)
        except TimeoutError as exc:
            raise CodexCliRuntimeError(
                "Codex CLI app-server did not respond in time.", status_code=504
            ) from exc
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, packet: dict[str, Any]) -> None:
        process = self.process
        if (
            self.closed
            or process is None
            or process.stdin is None
            or process.returncode is not None
        ):
            raise CodexCliRuntimeError("Codex CLI app-server connection closed.", status_code=503)
        encoded = (
            json.dumps(packet, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        )
        async with self.write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise CodexCliRuntimeError(
                    "Codex CLI app-server connection closed.", status_code=503
                ) from exc

    async def _read_stdout(self) -> None:
        reason = "Codex CLI app-server closed unexpectedly."
        try:
            assert self.process is not None and self.process.stdout is not None
            while not self.closed:
                try:
                    line = await self.process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    reason = "Codex CLI app-server returned an oversized message."
                    break
                if not line:
                    break
                if len(line) > MAX_PROTOCOL_LINE_BYTES:
                    reason = "Codex CLI app-server returned an oversized message."
                    break
                try:
                    packet = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    reason = "Codex CLI app-server returned an invalid protocol message."
                    break
                if not isinstance(packet, dict):
                    continue
                request_id = packet.get("id")
                if isinstance(request_id, int) and request_id in self.pending:
                    future = self.pending[request_id]
                    if not future.done():
                        if packet.get("error"):
                            future.set_exception(
                                CodexCliRuntimeError("Codex CLI app-server rejected a request.")
                            )
                        else:
                            result = packet.get("result")
                            future.set_result(result if isinstance(result, dict) else {})
                    continue
                method = packet.get("method")
                if not isinstance(method, str):
                    continue
                params = packet.get("params")
                params = params if isinstance(params, dict) else {}
                if "id" in packet:
                    await self._handle_server_request(packet["id"], method, params)
                elif self.on_notification is not None:
                    result = self.on_notification(method, params)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Codex CLI app-server transport ended", exc_info=True)
        finally:
            self._fail_pending(CodexCliRuntimeError(reason, status_code=503))
            if not self.closed and self.on_closed is not None:
                result = self.on_closed(reason)
                if asyncio.iscoroutine(result):
                    await result

    async def _handle_server_request(
        self, request_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        result: dict[str, Any] = {}
        if self.on_server_request is not None:
            try:
                response = self.on_server_request(method, params)
                if asyncio.iscoroutine(response):
                    response = await response
                if isinstance(response, dict):
                    result = response
            except Exception:
                result = {}
        try:
            await self._write({"id": request_id, "result": result})
        except CodexCliRuntimeError:
            return
        if self.on_server_request_complete is not None:
            completed = self.on_server_request_complete(method, params)
            if asyncio.iscoroutine(completed):
                await completed

    def _fail_pending(self, error: CodexCliRuntimeError) -> None:
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._fail_pending(
            CodexCliRuntimeError("Codex CLI app-server connection closed.", status_code=503)
        )
        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self.reader_task is not None and self.reader_task is not asyncio.current_task():
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)


class CodexCliRuntime:
    def __init__(
        self,
        *,
        executable_resolver: Callable[..., str | None] = resolve_codex_executable,
        app_server_factory: Callable[..., CodexAppServerClient] = CodexAppServerClient,
        login_status_runner: Callable[..., Awaitable[str]] = run_codex_login_status,
        env: dict[str, str] | None = None,
        home: str | None = None,
    ) -> None:
        self.executable_resolver = executable_resolver
        self.app_server_factory = app_server_factory
        self.login_status_runner = login_status_runner
        self.env = dict(env or os.environ)
        self.home = str(Path(home or Path.home()).expanduser())
        self.active_streams = 0
        self._last_account_id: str | None = None
        self._status_cache: dict[str, Any] | None = None
        self._status_cache_checked_at = 0.0
        self._status_cache_lock = threading.Lock()

    def executable(self) -> str | None:
        return self.executable_resolver(env=self.env, home=self.home)

    def matches_current_cli_account(self, account_id: str) -> bool:
        if not account_id:
            return False
        with self._status_cache_lock:
            cache_fresh = time.monotonic() - self._status_cache_checked_at < 15.0
            cached_account_id = self._last_account_id if cache_fresh else None
        if cached_account_id is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    asyncio.run(self.status())
                except Exception:
                    return False
            else:
                return False
            with self._status_cache_lock:
                cached_account_id = self._last_account_id
        return cached_account_id == account_id

    async def _read_account(self, client: CodexAppServerClient, cwd: str) -> dict[str, Any]:
        profiles = await client.request("permissionProfile/list", {"cwd": cwd})
        profile_data = profiles.get("data")
        if not isinstance(profile_data, list) or not any(
            isinstance(profile, dict)
            and profile.get("id") == CODEX_CLI_PERMISSION_PROFILE
            and profile.get("allowed") is True
            for profile in profile_data
        ):
            raise CodexCliRuntimeError(
                "The Codex CLI does not support AverQel's restricted inference profile.",
                status_code=503,
            )
        result = await client.request("account/read", {"refreshToken": False})
        account = result.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise CodexCliAccountMismatchError(
                "The configured Codex account is not the local Codex CLI ChatGPT login.",
                status_code=409,
            )
        return account

    @staticmethod
    def _reasoning_capabilities(model: dict[str, Any]) -> tuple[list[str], dict[str, str]] | None:
        advertised = model.get("supportedReasoningEfforts")
        if not isinstance(advertised, list):
            return None
        efforts: list[str] = []
        values: dict[str, str] = {}
        for entry in advertised[:32]:
            raw = (
                entry
                if isinstance(entry, str)
                else (entry.get("reasoningEffort") if isinstance(entry, dict) else None)
            )
            if not isinstance(raw, str):
                continue
            native = raw.strip()
            canonical = API_TO_UI_REASONING_EFFORT.get(
                native.lower()
            ) or normalize_reasoning_effort_value(native)
            if (
                canonical
                and canonical not in efforts
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,19}", native)
            ):
                efforts.append(canonical)
                values[canonical] = native
        return efforts, values

    async def _list_models(self, client: CodexAppServerClient) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _page in range(10):
            result = await client.request(
                "model/list", {"cursor": cursor, "includeHidden": False, "limit": 100}
            )
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexCliRuntimeError("Codex CLI returned an invalid model catalog.")
            for item in data[:500]:
                if (
                    not isinstance(item, dict)
                    or item.get("hidden") is not False
                    or not isinstance(item.get("model"), str)
                    or not item["model"].strip()
                    or len(item["model"]) > 255
                    or item["model"] in seen
                ):
                    continue
                model_id = item["model"]
                seen.add(model_id)
                display_name = item.get("displayName")
                model: dict[str, Any] = {
                    "id": model_id,
                    "name": (
                        display_name.strip()[:255]
                        if isinstance(display_name, str) and display_name.strip()
                        else model_id
                    ),
                    "kind": "chat",
                }
                reasoning = self._reasoning_capabilities(item)
                if reasoning is not None:
                    model["supportedReasoningEfforts"] = reasoning[0]
                    model["reasoningEffortValues"] = reasoning[1]
                models.append(model)
            next_cursor = result.get("nextCursor")
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if not cursor:
                break
        if not models:
            raise CodexCliRuntimeError("Codex CLI did not return any visible ChatGPT models.")
        return models

    async def status(self) -> dict[str, Any]:
        with self._status_cache_lock:
            if (
                self._status_cache is not None
                and time.monotonic() - self._status_cache_checked_at < 15.0
            ):
                return copy.deepcopy(self._status_cache)
        result = await self._load_status()
        with self._status_cache_lock:
            self._status_cache = copy.deepcopy(result)
            self._status_cache_checked_at = time.monotonic()
        return result

    async def _load_status(self) -> dict[str, Any]:
        self._last_account_id = None
        executable = self.executable()
        if not executable:
            return {
                "available": False,
                "chatgptSignInAvailable": False,
                "accounts": [],
                "models": [],
                "message": "Codex CLI is not installed for the local Kinesis backend user.",
            }
        method = await self.login_status_runner(executable, env=self.env)
        if method != "chatgpt":
            self._last_account_id = None
            messages = {
                "api_key": "Codex CLI is signed in with an API key. ChatGPT plan access requires ChatGPT sign-in.",
                "not_authenticated": "Codex CLI is installed but is not signed in to ChatGPT. Run 'codex login' in your terminal.",
                "other_authenticated_method": "Codex CLI is signed in with another method. ChatGPT sign-in is required for this provider.",
                "unavailable": "Codex CLI sign-in status could not be checked.",
            }
            return {
                "available": False,
                "chatgptSignInAvailable": False,
                "accounts": [],
                "models": [],
                "message": messages.get(method, "Codex CLI is not signed in to ChatGPT."),
                "cliMessage": messages.get(method, "Codex CLI is not signed in to ChatGPT."),
            }

        client: CodexAppServerClient | None = None
        try:
            client = self.app_server_factory(
                executable,
                cwd=tempfile.gettempdir(),
                env=self.env,
            )
            await client.start()
            account = await self._read_account(client, tempfile.gettempdir())
            email = account.get("email")
            if not isinstance(email, str) or not email.strip():
                raise CodexCliRuntimeError(
                    "Codex CLI account identity is unavailable.", status_code=503
                )
            account_id = _account_id(executable, email, env=self.env, home=self.home)
            models = await self._list_models(client)
            self._last_account_id = account_id
            return {
                "available": True,
                "chatgptSignInAvailable": False,
                "accounts": [
                    {
                        "id": account_id,
                        "email": None,
                        "name": "Codex CLI ChatGPT login",
                        "connected": True,
                        "source": "codex_cli",
                        "planType": (
                            account.get("planType")
                            if isinstance(account.get("planType"), str)
                            else None
                        ),
                    }
                ],
                "models": models,
                "cliMessage": "Logged in using ChatGPT",
            }
        except CodexCliRuntimeError as exc:
            self._last_account_id = None
            return {
                "available": False,
                "chatgptSignInAvailable": False,
                "accounts": [],
                "models": [],
                "message": str(exc),
                "cliMessage": str(exc),
            }
        finally:
            if client is not None:
                await client.close()

    async def stream_responses(
        self, *, account_id: str, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        if self.active_streams >= 4:
            raise CodexCliRuntimeError(
                "Too many Codex requests are already running for this local account.",
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
            raise CodexCliUnavailableError(
                "Codex CLI is not installed for the local Kinesis backend user.",
                status_code=503,
            )
        if (
            not isinstance(payload, dict)
            or len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            > MAX_INFERENCE_BODY_BYTES
        ):
            raise CodexCliRuntimeError(
                "Codex request exceeds the maximum supported size.", status_code=413
            )
        model = payload.get("model")
        items = payload.get("input")
        if not isinstance(model, str) or not re.fullmatch(r"[\w./:-]{1,255}", model):
            raise CodexCliRuntimeError("Codex request has an invalid model.", status_code=400)
        if not isinstance(items, list) or not 1 <= len(items) <= 256:
            raise CodexCliRuntimeError(
                "Codex request has invalid conversation input.", status_code=400
            )

        text_input, images = codex_cli_input(items)
        dynamic_tools = (
            []
            if payload.get("tool_choice") == "none"
            else codex_cli_dynamic_tools(payload.get("tools"))
        )
        allowed_tool_names = {
            tool["name"] for namespace in dynamic_tools for tool in namespace["tools"]
        }
        scratch_directory = tempfile.mkdtemp(prefix="averqel-codex-")
        try:
            os.chmod(scratch_directory, 0o700)
        except OSError:
            pass
        queue: asyncio.Queue[Any] = asyncio.Queue()
        reasoning_sources: dict[str, str] = {}
        delegated_tool_call = False
        terminal_queued = False

        async def on_notification(method: str, params: dict[str, Any]) -> None:
            nonlocal terminal_queued
            if terminal_queued:
                return
            if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
                queue.put_nowait(
                    {
                        "type": "response.output_text.delta",
                        "delta": params["delta"],
                        "output_index": 0,
                    }
                )
            elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
                item_id, delta = params.get("itemId"), params.get("delta")
                source = "summary" if method == "item/reasoning/summaryTextDelta" else "text"
                if isinstance(item_id, str) and item_id and isinstance(delta, str) and delta:
                    current_source = reasoning_sources.get(item_id)
                    if not current_source or current_source == source:
                        reasoning_sources[item_id] = source
                        queue.put_nowait(
                            {
                                "type": (
                                    "response.reasoning_summary_text.delta"
                                    if source == "summary"
                                    else "response.reasoning.delta"
                                ),
                                "delta": delta,
                                "output_index": 0,
                            }
                        )
            elif method == "item/reasoning/summaryPartAdded":
                item_id = params.get("itemId")
                if (
                    isinstance(item_id, str)
                    and reasoning_sources.get(item_id) == "summary"
                    and isinstance(params.get("summaryIndex"), int)
                    and params["summaryIndex"] > 0
                ):
                    queue.put_nowait(
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "delta": "\n\n",
                            "output_index": 0,
                        }
                    )
            elif method in {
                "item/reasoning/delta",
                "item/thought/delta",
                "item/agentMessage/reasoningDelta",
            } and isinstance(params.get("delta"), str):
                queue.put_nowait(
                    {
                        "type": "response.reasoning.delta",
                        "delta": params["delta"],
                        "output_index": 0,
                    }
                )
            elif method == "turn/completed":
                terminal_queued = True
                turn = params.get("turn")
                if (
                    isinstance(turn, dict)
                    and turn.get("status") == "completed"
                    and not (
                        payload.get("tool_choice") == "required"
                        and dynamic_tools
                        and not delegated_tool_call
                    )
                ):
                    queue.put_nowait({"type": "response.completed"})
                    queue.put_nowait(_STREAM_END)
                else:
                    queue.put_nowait(
                        _StreamFailure(
                            "Codex CLI could not complete this model request.", status_code=502
                        )
                    )
                    queue.put_nowait(_STREAM_END)

        async def on_server_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal delegated_tool_call
            if method == "item/tool/call":
                name = params.get("tool")
                if not isinstance(name, str) or name not in allowed_tool_names:
                    return {
                        "success": False,
                        "contentItems": [
                            {"type": "inputText", "text": "This tool is not available to AverQel."}
                        ],
                    }
                delegated_tool_call = True
                return {
                    "success": False,
                    "contentItems": [
                        {"type": "inputText", "text": "AverQel will handle this function call."}
                    ],
                }
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                return {"decision": "decline"}
            return {}

        async def on_server_request_complete(method: str, params: dict[str, Any]) -> None:
            nonlocal terminal_queued
            if method != "item/tool/call" or terminal_queued:
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

        client: CodexAppServerClient | None = None
        try:
            client = self.app_server_factory(
                executable,
                cwd=scratch_directory,
                env=self.env,
                on_notification=on_notification,
                on_server_request=on_server_request,
                on_server_request_complete=on_server_request_complete,
                on_closed=on_closed,
            )
            await client.start()
            account = await self._read_account(client, scratch_directory)
            email = account.get("email")
            if (
                not isinstance(email, str)
                or _account_id(executable, email, env=self.env, home=self.home) != account_id
            ):
                raise CodexCliAccountMismatchError(
                    "The configured Codex account is not the local Codex CLI login.",
                    status_code=409,
                )
            developer_instructions = [
                "This AverQel request is for model inference. Do not use local files, shell commands, network tools, MCP servers, or other Codex runtime tools. Use only the supplied AverQel function tools when a function is needed.",
                "The Codex process working directory is a disposable inference scratch directory. It is not the conversation workspace and does not describe the user's file permissions. Follow the effective sandbox mode and workspace stated in the AverQel conversation instructions. For file work, call the supplied AverQel tools and use their results; do not refuse or claim a path is inaccessible based only on this process directory.",
            ]
            if payload.get("tool_choice") == "required" and dynamic_tools:
                developer_instructions.append(
                    "You must call one of the supplied AverQel function tools before returning a final answer."
                )
            instructions = payload.get("instructions")
            if isinstance(instructions, str) and instructions:
                developer_instructions.append(instructions[:32_000])
            thread = await client.request(
                "thread/start",
                {
                    "model": model,
                    "modelProvider": "openai",
                    "cwd": scratch_directory,
                    "permissions": CODEX_CLI_PERMISSION_PROFILE,
                    "approvalPolicy": "on-request",
                    "ephemeral": True,
                    "allowProviderModelFallback": False,
                    "developerInstructions": "\n\n".join(developer_instructions),
                    "dynamicTools": dynamic_tools,
                },
            )
            if thread.get("activePermissionProfile", {}).get("id") != CODEX_CLI_PERMISSION_PROFILE:
                raise CodexCliRuntimeError(
                    "Codex CLI did not activate AverQel's restricted inference profile.",
                    status_code=503,
                )
            thread_id = thread.get("thread", {}).get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexCliRuntimeError(
                    "Codex CLI did not start a model thread.", status_code=502
                )
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "model": model,
                "cwd": scratch_directory,
                "permissions": CODEX_CLI_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [scratch_directory],
                "approvalPolicy": "on-request",
                "input": [
                    {"type": "text", "text": text_input},
                    *({"type": "image", "url": image} for image in images),
                ],
                "summary": "auto",
            }
            reasoning = payload.get("reasoning")
            effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
            if isinstance(effort, str) and effort != "none":
                turn_params["effort"] = effort
            await client.request("turn/start", turn_params)
            deadline = asyncio.get_running_loop().time() + INFERENCE_TIMEOUT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CodexCliRuntimeError("ChatGPT model response timed out.", status_code=504)
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError as exc:
                    raise CodexCliRuntimeError(
                        "ChatGPT model response timed out.", status_code=504
                    ) from exc
                if item is _STREAM_END:
                    break
                if isinstance(item, _StreamFailure):
                    raise CodexCliRuntimeError(item.message, status_code=item.status_code)
                if isinstance(item, dict):
                    yield item
        finally:
            if client is not None:
                await client.close()
            shutil.rmtree(scratch_directory, ignore_errors=True)


def codex_cli_input(items: list[Any]) -> tuple[str, list[str]]:
    transcript: list[str] = []
    images: list[str] = []

    def text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "input_text", "output_text"} and isinstance(
                part.get("text"), str
            ):
                text_parts.append(part["text"])
            elif part.get("type") == "input_image":
                url = part.get("image_url")
                image_url = (
                    url
                    if isinstance(url, str)
                    else url.get("url") if isinstance(url, dict) else None
                )
                if (
                    isinstance(image_url, str)
                    and (image_url.startswith("https://") or image_url.startswith("data:image/"))
                    and len(images) < 10
                ):
                    images.append(image_url)
                text_parts.append("[Image attached]")
        return "\n".join(text_parts)

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            transcript.append(
                f"ASSISTANT TOOL CALL ({str(item.get('name') or 'unknown')}):\n{str(item.get('arguments') or '{}')[:16_000]}"
            )
        elif item_type == "function_call_output":
            transcript.append(
                f"TOOL RESULT ({str(item.get('call_id') or 'unknown')}):\n{str(item.get('output') or '')[:100_000]}"
            )
        elif item.get("role") or item_type == "message":
            raw_role = item.get("role")
            role = (
                raw_role.upper()
                if raw_role in {"assistant", "user", "system", "developer"}
                else "USER"
            )
            content = text_of(item.get("content"))
            if content:
                transcript.append(f"{role}:\n{content}")
    return "\n\n".join(transcript)[:1_000_000], images


def codex_cli_dynamic_tools(tools: Any) -> list[dict[str, Any]]:
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
                        "description": description[:1_000] if isinstance(description, str) else "",
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


codex_cli_runtime = CodexCliRuntime()
