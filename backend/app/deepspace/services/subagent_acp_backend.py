"""ACP child-process backend used by DeepSpace's external subagent provider.

The transport is newline-delimited JSON-RPC, but the requests below follow the
ACP session methods (initialize, session/new, session/prompt). Process shutdown
uses stdin EOF first, then bounded terminate/kill escalation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.deepspace.services.subprocess_stdio import (
    SubprocessHandle,
    SubprocessStdio,
    build_subprocess_environment,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)

PermissionPolicy = Literal["allow", "reject"]


@dataclass
class AcpRunSpec:
    """Resolved spawn settings for one ACP child process."""

    command: str
    args: list[str] = field(default_factory=list)
    cwd: str = "."
    permission: PermissionPolicy = "reject"
    env: dict[str, str] = field(default_factory=dict)
    dispose_eof_grace_ms: int = 5_000
    dispose_grace_ms: int = 3_000
    spawn: Callable[..., Any] | None = None
    on_error: Callable[[Exception, str], None] | None = None


@dataclass
class AcpSubagentResult:
    """Result of one ACP session prompt."""

    status: str = "completed"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stop_reason: str = "completed"


class AcpProtocolError(RuntimeError):
    """The child returned an invalid or failed ACP response."""


class _AcpCancelledError(Exception):
    """The owning run requested cooperative cancellation."""


class AcpProvider:
    """Run one ACP agent session in an owned subprocess."""

    name = "acp"

    def __init__(
        self,
        command: str = "python",
        args: list[str] | None = None,
        permission: PermissionPolicy = "reject",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        dispose_eof_grace_ms: int = 5_000,
        dispose_grace_ms: int = 3_000,
        spawn: Callable[..., Any] | None = None,
        on_error: Callable[[Exception, str], None] | None = None,
    ) -> None:
        if permission not in {"allow", "reject"}:
            raise ValueError("permission must be 'allow' or 'reject'")
        self.command = command
        self.args = list(args or [])
        self.permission = permission
        self.base_env = dict(env or {})
        self.base_cwd = cwd
        self.dispose_eof_grace_ms = max(0, int(dispose_eof_grace_ms))
        self.dispose_grace_ms = max(0, int(dispose_grace_ms))
        self.spawn = spawn
        self.on_error = on_error

    async def run(
        self,
        spec: Any,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> AcpSubagentResult:
        """Initialize ACP, create a session, run one prompt, and always reap it."""
        cwd = os.path.abspath(self.base_cwd or getattr(spec, "cwd", ".") or ".")
        process: asyncio.subprocess.Process | None = None
        managed_handle: SubprocessHandle | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail = bytearray()
        answer_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        session_id: str | None = None
        request_id = 0

        async def send(message: dict[str, Any]) -> None:
            if process is None or process.stdin is None:
                raise AcpProtocolError("ACP child stdin is unavailable")
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()

        async def drain_stderr() -> None:
            if process is None or process.stderr is None:
                return
            while True:
                chunk = await process.stderr.read(2_048)
                if not chunk:
                    return
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 8_192:
                    del stderr_tail[:-8_192]

        async def cleanup_process() -> None:
            if process is None:
                return
            await self.dispose(process, managed_handle=managed_handle)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)

        async def answer_permission(message: dict[str, Any]) -> None:
            request = message.get("id")
            params = message.get("params")
            if request is None or not isinstance(params, dict):
                return
            options = params.get("options")
            options = options if isinstance(options, list) else []
            allow_option = next(
                (
                    item
                    for item in options
                    if isinstance(item, dict)
                    and item.get("kind") in {"allow_once", "allow_always"}
                    and isinstance(item.get("optionId"), str)
                ),
                None,
            )
            if self.permission == "allow" and allow_option is not None:
                result = {"outcome": {"outcome": "selected", "optionId": allow_option["optionId"]}}
            else:
                result = {"outcome": {"outcome": "cancelled"}}
            await send({"jsonrpc": "2.0", "id": request, "result": result})

        async def next_message() -> dict[str, Any] | None:
            if process is None or process.stdout is None:
                raise AcpProtocolError("ACP child stdout is unavailable")
            while True:
                if cancel_check():
                    raise _AcpCancelledError("ACP child cancellation requested")
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                except TimeoutError:
                    if process.returncode is not None:
                        return None
                    continue
                if not line:
                    return None
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.debug("Ignoring malformed ACP child output line")
                    continue
                if isinstance(message, dict):
                    if message.get("method") == "session/request_permission":
                        await answer_permission(message)
                        continue
                    method = message.get("method")
                    params = message.get("params")
                    if method == "session/update" and isinstance(params, dict):
                        update = params.get("update")
                        if isinstance(update, dict):
                            if update.get("sessionUpdate") == "agent_message_chunk":
                                content = update.get("content")
                                if isinstance(content, dict) and content.get("type") == "text":
                                    answer_parts.append(str(content.get("text") or ""))
                                elif isinstance(content, list):
                                    answer_parts.extend(
                                        str(item.get("text") or "")
                                        for item in content
                                        if isinstance(item, dict) and item.get("type") == "text"
                                    )
                            elif update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                                tool_calls.append(dict(update))
                    elif method == "tool/call" and isinstance(params, dict):
                        tool_calls.append(dict(params))
                    return message

        async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal request_id
            request_id += 1
            current_id = request_id
            await send({"jsonrpc": "2.0", "id": current_id, "method": method, "params": params})
            while True:
                message = await next_message()
                if message is None:
                    raise AcpProtocolError(f"ACP child closed before replying to {method}")
                if message.get("id") != current_id:
                    continue
                if "error" in message:
                    error = message.get("error")
                    raise AcpProtocolError(f"ACP {method} failed: {error}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AcpProtocolError(f"ACP {method} returned no object result")
                return result

        try:
            spec_spawn = getattr(spec, "spawn", None)
            if callable(spec_spawn):
                spawn = spec_spawn
            elif self.spawn is not None:
                spawn = self.spawn
            else:
                child_env = {**self.base_env, **(getattr(spec, "env", {}) or {})}
                managed_handle = await spawn_with_stdio(
                    [self.command, *self.args],
                    cwd=cwd,
                    env=child_env,
                    stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
                    grace_ms=self.dispose_grace_ms,
                )
                process = managed_handle.process
                spawn = None
            if process is None and spawn is not None:
                child_env = build_subprocess_environment(
                    {**self.base_env, **(getattr(spec, "env", {}) or {})}
                )
                process = await spawn(
                    self.command,
                    *self.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=child_env,
                )
            stderr_task = asyncio.create_task(drain_stderr())
            await request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "deepspace-subagent", "version": "1.0"},
                },
            )
            created = await request("session/new", {"cwd": cwd, "mcpServers": []})
            raw_session_id = created.get("sessionId")
            if not isinstance(raw_session_id, str) or not raw_session_id:
                raise AcpProtocolError("ACP session/new returned no session id")
            session_id = raw_session_id

            requested_provider = str(getattr(spec, "provider", None) or "").strip()
            if requested_provider:
                raise AcpProtocolError(
                    "This ACP agent does not expose a standard provider selector; "
                    "configure the provider in the ACP agent profile."
                )
            requested_model = str(getattr(spec, "model", None) or "").strip()
            if requested_model:
                raw_options = created.get("configOptions")
                config_options = raw_options if isinstance(raw_options, list) else []
                model_option = next(
                    (
                        option
                        for option in config_options
                        if isinstance(option, dict)
                        and option.get("category") == "model"
                        and option.get("type") == "select"
                    ),
                    None,
                )
                if model_option is None:
                    raise AcpProtocolError(
                        f"ACP agent does not expose a selectable model; requested {requested_model!r}."
                    )
                config_id = model_option.get("configId") or model_option.get("id")
                values: set[str] = set()
                raw_model_values = model_option.get("options")
                if isinstance(raw_model_values, list):
                    for option in raw_model_values:
                        if not isinstance(option, dict):
                            continue
                        nested = option.get("options")
                        candidates = nested if isinstance(nested, list) else [option]
                        values.update(
                            str(candidate["value"])
                            for candidate in candidates
                            if isinstance(candidate, dict)
                            and isinstance(candidate.get("value"), str)
                        )
                if not isinstance(config_id, str) or requested_model not in values:
                    raise AcpProtocolError(
                        f"ACP agent does not offer requested model {requested_model!r}."
                    )
                if model_option.get("currentValue") != requested_model:
                    selected = await request(
                        "session/set_config_option",
                        {
                            "sessionId": session_id,
                            "configId": config_id,
                            "value": requested_model,
                        },
                    )
                    selected_options = selected.get("configOptions")
                    confirmed = (
                        next(
                            (
                                option
                                for option in selected_options
                                if isinstance(option, dict)
                                and (option.get("configId") or option.get("id")) == config_id
                            ),
                            None,
                        )
                        if isinstance(selected_options, list)
                        else None
                    )
                    if (
                        not isinstance(confirmed, dict)
                        or confirmed.get("currentValue") != requested_model
                    ):
                        raise AcpProtocolError(
                            f"ACP agent did not confirm requested model {requested_model!r}."
                        )

            await send(
                {
                    "jsonrpc": "2.0",
                    "id": (request_id := request_id + 1),
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [
                            {
                                "type": "text",
                                "text": (
                                    f"Child instructions (persona):\n{spec.persona}\n\nTask:\n{spec.task}"
                                    if getattr(spec, "persona", None)
                                    else str(getattr(spec, "task", ""))
                                ),
                            }
                        ],
                    },
                }
            )
            prompt_id = request_id
            while True:
                message = await next_message()
                if message is None:
                    raise AcpProtocolError("ACP child closed before session/prompt completed")
                if message.get("id") != prompt_id:
                    continue
                if "error" in message:
                    raise AcpProtocolError(f"ACP session/prompt failed: {message['error']}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AcpProtocolError("ACP session/prompt returned no object result")
                stop_reason = str(result.get("stopReason") or "unknown")
                status = (
                    "completed"
                    if stop_reason == "end_turn"
                    else ("cancelled" if stop_reason == "cancelled" else "failed")
                )
                error = None if status == "completed" else f"ACP prompt ended with {stop_reason}"
                break

            await cleanup_process()
            return AcpSubagentResult(
                status=status,
                answer="".join(answer_parts),
                tool_calls=tool_calls,
                error=error,
                stop_reason=stop_reason,
            )
        except _AcpCancelledError:
            if process is not None:
                if session_id is not None:
                    try:
                        await send(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/cancel",
                                "params": {"sessionId": session_id},
                            }
                        )
                    except Exception:  # noqa: BLE001 - process teardown remains authoritative
                        pass
                await cleanup_process()
            return AcpSubagentResult(
                status="cancelled",
                answer="".join(answer_parts),
                tool_calls=tool_calls,
                stop_reason="cancelled",
            )
        except asyncio.CancelledError:
            if process is not None:
                try:
                    await asyncio.shield(cleanup_process())
                except Exception:  # noqa: BLE001 - preserve task cancellation
                    logger.warning("ACP cleanup after task cancellation failed", exc_info=True)
            raise
        except Exception as exc:  # noqa: BLE001 - external providers are an error boundary
            if process is not None:
                try:
                    await cleanup_process()
                except Exception:  # noqa: BLE001 - retain the protocol/startup failure
                    logger.warning("ACP child cleanup also failed", exc_info=True)
            self._report_error(exc, "error")
            return AcpSubagentResult(
                status="failed",
                answer="".join(answer_parts),
                tool_calls=tool_calls,
                error=str(exc),
                stop_reason="error",
            )

    async def dispose(
        self,
        process: asyncio.subprocess.Process,
        *,
        managed_handle: SubprocessHandle | None = None,
    ) -> None:
        """Allow EOF-driven child cleanup, then terminate and kill within bounds."""
        if process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await asyncio.wait_for(wait_closed(), timeout=self.dispose_eof_grace_ms / 1000)
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.dispose_eof_grace_ms / 1000)
            return
        except TimeoutError:
            pass
        if managed_handle is not None:
            await managed_handle.terminate_and_wait()
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.dispose_grace_ms / 1000)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()
        except ProcessLookupError:
            return

    def _report_error(self, error: Exception, reason: str) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(error, reason)
        except Exception:  # noqa: BLE001 - diagnostics cannot replace child outcome
            logger.warning("ACP error observer failed", exc_info=True)


class AcpSubagentProvider(AcpProvider):
    """Compatibility name used by the existing provider registry."""


__all__ = [
    "AcpRunSpec",
    "AcpSubagentResult",
    "AcpProtocolError",
    "AcpProvider",
    "AcpSubagentProvider",
    "PermissionPolicy",
]
