"""Optional DeepSeek Harness SDK subprocess provider for DeepSpace.

This adapter speaks the checked-in DSH SDK's newline-delimited JSON-RPC
protocol directly. It never fabricates a result: an unavailable executable,
missing explicit DSH_HOME, protocol error, or unsupported per-child tool
restriction is returned as a failed subagent run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.deepspace.services.subprocess_stdio import (
    SubprocessHandle,
    SubprocessStdio,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)


@dataclass
class DshSdkRunSpec:
    """Resolved options for a single SDK-owned child session."""

    task: str
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    tool_allow: list[str] | None = None
    tool_deny: list[str] | None = None


@dataclass
class DshSdkResult:
    """One child run's terminal outcome, as reported by the SDK event log."""

    status: str = "completed"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stop_reason: str = "completed"
    rounds: int = 0


class _DshCancelledError(Exception):
    """The owning DeepSpace child run requested cancellation."""


class DshSdkProvider:
    """Drive one DSH SDK session using its local runtime executable."""

    name = "dsh-sdk"

    def __init__(
        self,
        command: str = "dsh",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        dsh_home: str | None = None,
        profile: str = "sdk",
        patches: list[str] | None = None,
        provider: str = "deepseek-official",
        model: str = "deepseek-v4-flash",
        initialize_timeout_ms: int = 30_000,
        dispose_eof_grace_ms: int = 6_000,
        dispose_grace_ms: int = 3_000,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.base_env = dict(env or {})
        self.base_cwd = cwd
        self.dsh_home = dsh_home
        self.profile = profile
        self.patches = list(patches or [])
        self.provider = provider
        self.model = model
        self.initialize_timeout_ms = max(1, int(initialize_timeout_ms))
        self.dispose_eof_grace_ms = max(0, int(dispose_eof_grace_ms))
        self.dispose_grace_ms = max(0, int(dispose_grace_ms))

    async def run(
        self,
        spec: DshSdkRunSpec | Any,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> DshSdkResult:
        """Initialize the SDK runtime, submit one prompt, and reap the child."""
        if getattr(spec, "tool_allow", None) or getattr(spec, "tool_deny", None):
            return DshSdkResult(
                status="failed",
                error=(
                    "The DSH SDK profile cannot enforce per-child tool_allow/tool_deny. "
                    "Use the in-process backend or configure a restricted SDK profile."
                ),
                stop_reason="error",
            )

        env = dict(self.base_env)
        env.update(getattr(spec, "env", {}) or {})
        dsh_home = str(
            self.dsh_home or env.get("DSH_HOME") or os.environ.get("DSH_HOME") or ""
        ).strip()
        if not dsh_home:
            return DshSdkResult(
                status="failed",
                error="The DSH SDK backend requires an explicit DSH_HOME for isolated runtime state.",
                stop_reason="error",
            )
        env["DSH_HOME"] = str(Path(dsh_home).expanduser().resolve())
        cwd = Path(self.base_cwd or getattr(spec, "cwd", ".") or ".").expanduser().resolve()
        args = [*self.args, "--profile", self.profile]
        for patch in self.patches:
            args.extend(("--patch", str(Path(patch).expanduser().resolve())))

        process: asyncio.subprocess.Process | None = None
        managed_handle: SubprocessHandle | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail = bytearray()
        request_id = 0
        session_id = f"session-{uuid.uuid4().hex}"
        events: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        idle_seen = False
        receipt_seen = False
        final_command: dict[str, Any] | None = None

        async def send(message: dict[str, Any]) -> None:
            if process is None or process.stdin is None:
                raise RuntimeError("DSH SDK runtime stdin is unavailable")
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()

        async def read_message(*, check_cancel: bool = True) -> dict[str, Any] | None:
            if process is None or process.stdout is None:
                raise RuntimeError("DSH SDK runtime stdout is unavailable")
            while True:
                if check_cancel and cancel_check():
                    raise _DshCancelledError
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
                except json.JSONDecodeError:
                    logger.debug("Ignoring malformed DSH SDK output line")
                    continue
                if isinstance(message, dict):
                    handle_notification(message)
                    return message

        def handle_notification(message: dict[str, Any]) -> None:
            nonlocal idle_seen, receipt_seen, final_command
            method = message.get("method")
            params = message.get("params")
            if not isinstance(params, dict):
                return
            if method == "session.event" and params.get("sessionId") == session_id:
                event = params.get("event")
                if isinstance(event, dict):
                    events.append(event)
                    if event.get("type") == "tool/call":
                        data = event.get("data")
                        if isinstance(data, dict):
                            tool_calls.append(data)
            elif method == "session.status" and params.get("sessionId") == session_id:
                idle_seen = params.get("status") == "idle"
            elif method == "session.query.completed" and params.get("sessionId") == session_id:
                final_command = params
                receipt_seen = True
            elif method == "user-question/ask":
                request = message.get("id")
                if request is not None:
                    # The SDK child is not given a user-interaction channel.
                    # Explicit dismissal is safer than leaving its agent parked.
                    asyncio.create_task(
                        send(
                            {
                                "jsonrpc": "2.0",
                                "id": request,
                                "result": {"cancelled": True},
                            }
                        )
                    )

        async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal request_id
            request_id += 1
            current_id = request_id
            await send({"jsonrpc": "2.0", "id": current_id, "method": method, "params": params})
            while True:
                message = await read_message()
                if message is None:
                    raise RuntimeError(f"DSH SDK runtime closed before replying to {method}")
                if message.get("id") != current_id:
                    continue
                if "error" in message:
                    error = message.get("error")
                    raise RuntimeError(f"DSH SDK {method} failed: {error}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"DSH SDK {method} returned no object result")
                return result

        async def close_process(*, send_shutdown: bool) -> None:
            if process is None or process.returncode is not None:
                return
            if send_shutdown:
                try:
                    shutdown_id = request_id + 1
                    await send({"jsonrpc": "2.0", "id": shutdown_id, "method": "shutdown"})
                    async with asyncio.timeout(self.initialize_timeout_ms / 1000):
                        while True:
                            message = await read_message(check_cancel=False)
                            if message is None or message.get("id") == shutdown_id:
                                break
                except Exception:  # noqa: BLE001 - bounded shutdown precedes EOF escalation
                    logger.debug("DSH SDK shutdown exchange did not complete", exc_info=True)
            if process.stdin is not None:
                process.stdin.close()
            try:
                if managed_handle is None:
                    await asyncio.wait_for(process.wait(), timeout=self.dispose_eof_grace_ms / 1000)
                else:
                    await asyncio.wait_for(
                        managed_handle.wait(), timeout=self.dispose_eof_grace_ms / 1000
                    )
                return
            except TimeoutError:
                pass
            if managed_handle is not None:
                await managed_handle.terminate_and_wait()
                return
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=self.dispose_grace_ms / 1000)
            except TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                return

        async def drain_stderr() -> None:
            if process is None or process.stderr is None:
                return
            while True:
                chunk = await process.stderr.read(2048)
                if not chunk:
                    return
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 8_192:
                    del stderr_tail[:-8_192]

        try:
            managed_handle = await spawn_with_stdio(
                [self.command, *args],
                cwd=str(cwd),
                env=env,
                stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
                grace_ms=self.dispose_grace_ms,
            )
            process = managed_handle.process
            stderr_task = asyncio.create_task(drain_stderr())
            initialize: dict[str, Any] = {
                "cwd": str(cwd),
                "provider": getattr(spec, "provider", None) or self.provider,
                "model": getattr(spec, "model", None) or self.model,
            }
            await asyncio.wait_for(
                request("initialize", initialize),
                timeout=self.initialize_timeout_ms / 1000,
            )
            task = str(getattr(spec, "task", ""))
            persona = str(
                getattr(spec, "system_prompt", None) or getattr(spec, "persona", None) or ""
            ).strip()
            prompt = (
                f"Child instructions (persona):\n{persona}\n\nTask:\n{task}" if persona else task
            )
            query_result = await request(
                "session/query",
                {
                    "sessionId": session_id,
                    "contentBlocks": [{"type": "text", "text": prompt}],
                },
            )
            query_kind = query_result.get("kind")
            if query_kind not in {"prompt", "command"}:
                raise RuntimeError("DSH SDK session/query returned an invalid receipt")
            receipt_seen = True
            if query_kind == "command":
                execution = query_result.get("execution")
                if isinstance(execution, dict):
                    final_command = {
                        "finalResponse": execution.get("finalResponse") or "",
                        "finishReason": execution.get("finishReason") or execution.get("status"),
                    }
            while not idle_seen and final_command is None:
                message = await read_message()
                if message is None:
                    raise RuntimeError(
                        "DSH SDK runtime closed before the child session became idle"
                    )
                if message.get("method") == "session.query.completed":
                    final_command = (
                        message.get("params") if isinstance(message.get("params"), dict) else None
                    )
                    receipt_seen = True
                    break
            if not receipt_seen:
                raise RuntimeError("DSH SDK runtime did not accept the child prompt")

            if final_command is not None:
                answer = str(final_command.get("finalResponse") or "")
                stop_reason = str(final_command.get("finishReason") or "completed")
            else:
                answer = _final_response(events)
                stop_reason = _finish_reason(events) or "completed"
            status = (
                "completed"
                if stop_reason == "completed"
                else ("cancelled" if stop_reason in {"aborted", "interrupted"} else "failed")
            )
            error = None if status == "completed" else f"DSH SDK child stopped with {stop_reason}"
            await close_process(send_shutdown=True)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            return DshSdkResult(
                status=status,
                answer=answer,
                tool_calls=tool_calls,
                error=error,
                stop_reason=stop_reason,
                rounds=sum(1 for event in events if event.get("type") == "turn/start"),
            )
        except _DshCancelledError:
            await close_process(send_shutdown=False)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            return DshSdkResult(
                status="cancelled",
                answer=_final_response(events),
                tool_calls=tool_calls,
                stop_reason="cancelled",
                rounds=sum(1 for event in events if event.get("type") == "turn/start"),
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(close_process(send_shutdown=False))
            except Exception:  # noqa: BLE001 - preserve task cancellation
                logger.warning("DSH SDK cleanup after task cancellation failed", exc_info=True)
            raise
        except Exception as exc:  # noqa: BLE001 - external runtime is a failure boundary
            await close_process(send_shutdown=False)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            logger.warning("DSH SDK subagent runtime failed", exc_info=True)
            return DshSdkResult(
                status="failed",
                answer=_final_response(events),
                tool_calls=tool_calls,
                error=str(exc),
                stop_reason="error",
                rounds=sum(1 for event in events if event.get("type") == "turn/start"),
            )


def _final_response(events: list[dict[str, Any]]) -> str:
    """Extract the last assistant text message from a DSH SDK event suffix."""
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        message = data.get("message")
        owner = message if isinstance(message, dict) else data
        content = owner.get("content")
        if isinstance(content, list):
            return "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""


def _finish_reason(events: list[dict[str, Any]]) -> str | None:
    """Read the most recent turn/end reason from the SDK event suffix."""
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        kind = reason.get("kind") if isinstance(reason, dict) else None
        return str(kind) if kind is not None else None
    return None


__all__ = ["DshSdkRunSpec", "DshSdkResult", "DshSdkProvider"]
