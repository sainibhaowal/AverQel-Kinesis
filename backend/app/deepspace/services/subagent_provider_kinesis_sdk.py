"""Optional KineSis SDK subprocess provider for DeepSpace.

This adapter speaks the checked-in KineSis SDK's newline-delimited JSON-RPC
protocol directly. It never fabricates a result: an unavailable executable,
missing explicit KINESIS_HOME, protocol error, or unsupported per-child tool
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
from typing import Any

from app.deepspace.services.subprocess_stdio import (
    SubprocessOutcome,
    SubprocessStdio,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)


@dataclass
class KineSisSdkRunSpec:
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
class KineSisSdkResult:
    """One child run's terminal outcome, as reported by the SDK event log."""

    status: str = "completed"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stop_reason: str = "completed"
    rounds: int = 0


class _KineSisCancelledError(Exception):
    """The owning DeepSpace child run requested cancellation."""


class KineSisSdkProvider:
    """Drive one KineSis SDK session using its local runtime executable."""

    name = "kinesis-sdk"

    def __init__(
        self,
        command: str = "kinesis",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        kinesis_home: str | None = None,
        profile: str = "sdk",
        patches: list[str] | None = None,
        provider: str = "averqel-official",
        model: str = "kinesis-v4-flash",
        initialize_timeout_ms: int = 30_000,
        dispose_eof_grace_ms: int = 6_000,
        dispose_grace_ms: int = 3_000,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.base_env = dict(env or {})
        self.base_cwd = cwd
        self.kinesis_home = kinesis_home
        self.profile = profile
        self.patches = list(patches or [])
        self.provider = provider
        self.model = model
        self.initialize_timeout_ms = max(1, int(initialize_timeout_ms))
        self.dispose_eof_grace_ms = max(0, int(dispose_eof_grace_ms))
        self.dispose_grace_ms = max(0, int(dispose_grace_ms))

    async def run(
        self,
        spec: KineSisSdkRunSpec | Any,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> KineSisSdkResult:
        """Initialize the SDK runtime, submit one prompt, and reap the child."""
        if getattr(spec, "tool_allow", None) or getattr(spec, "tool_deny", None):
            return KineSisSdkResult(
                status="failed",
                error=(
                    "KineSis SDK backend does not support per-child tool filtering. "
                    "Use the in-process backend for tool_allow/tool_deny."
                ),
            )

        kinesis_home = (
            self.kinesis_home
            or self.base_env.get("KINESIS_HOME")
            or os.environ.get("KINESIS_HOME")
            or ""
        )
        if not kinesis_home:
            return KineSisSdkResult(
                status="failed",
                error="KINESIS_HOME must be set to use the KineSis SDK backend.",
            )

        env = dict(self.base_env)
        env.update(getattr(spec, "env", {}) or {})
        env["KINESIS_HOME"] = kinesis_home
        if self.profile:
            env["KINESIS_PROFILE"] = self.profile

        cwd = spec.cwd or self.base_cwd or "."
        cmd = [self.command, *self.args, "run"]
        if self.patches:
            for p in self.patches:
                cmd.extend(["--patch", p])
        if self.provider:
            cmd.extend(["--provider", self.provider])
        if self.model:
            cmd.extend(["--model", self.model])

        handle = await spawn_with_stdio(
            cmd,
            cwd=cwd,
            env=env,
            grace_ms=self.dispose_grace_ms,
            stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
        )
        if handle.stdin is None or handle.stdout is None or handle.stderr is None:
            await handle.terminate_and_wait()
            return KineSisSdkResult(
                status="failed",
                error="KineSis SDK subprocess did not provide all required stdio pipes.",
            )

        request = {
            "id": str(uuid.uuid4()),
            "method": "prompt",
            "params": {
                "prompt": spec.task,
                "system_prompt": spec.system_prompt,
            },
        }

        async def write_stdin() -> None:
            try:
                handle.stdin.write((json.dumps(request) + "\n").encode())
                await handle.stdin.drain()
                handle.stdin.close()
            except Exception as exc:
                logger.debug("KineSis SDK stdin write failed: %s", exc)

        async def read_stdout() -> bytes:
            try:
                data = await handle.stdout.read()
                return data if isinstance(data, bytes) else b""
            except Exception as exc:
                logger.debug("KineSis SDK stdout read failed: %s", exc)
                return b""

        async def read_stderr() -> bytes:
            try:
                data = await handle.stderr.read()
                return data if isinstance(data, bytes) else b""
            except Exception as exc:
                logger.debug("KineSis SDK stderr read failed: %s", exc)
                return b""

        try:
            await asyncio.wait_for(write_stdin(), timeout=5.0)
        except TimeoutError:
            await handle.terminate_and_wait()
            return KineSisSdkResult(status="failed", error="KineSis SDK stdin write timed out")
        except asyncio.CancelledError:
            await handle.terminate_and_wait()
            raise

        async def collect_result() -> tuple[bytes, bytes, SubprocessOutcome]:
            stdout_data, stderr_data, outcome = await asyncio.gather(
                read_stdout(), read_stderr(), handle.wait()
            )
            return stdout_data, stderr_data, outcome

        collect_task = asyncio.create_task(collect_result())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.initialize_timeout_ms / 1000
        try:
            while not collect_task.done():
                if cancel_check():
                    await handle.terminate_and_wait()
                    collect_task.cancel()
                    await asyncio.gather(collect_task, return_exceptions=True)
                    return KineSisSdkResult(
                        status="cancelled",
                        error="DeepSpace child run cancelled",
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await handle.terminate_and_wait()
                    collect_task.cancel()
                    await asyncio.gather(collect_task, return_exceptions=True)
                    return KineSisSdkResult(
                        status="failed",
                        error="KineSis SDK response timed out.",
                    )
                await asyncio.wait({collect_task}, timeout=min(0.05, remaining))
        except asyncio.CancelledError:
            await handle.terminate_and_wait()
            collect_task.cancel()
            await asyncio.gather(collect_task, return_exceptions=True)
            raise

        try:
            stdout_data, stderr_data, outcome = collect_task.result()
        except asyncio.CancelledError:
            await handle.terminate_and_wait()
            raise

        if cancel_check():
            return KineSisSdkResult(status="cancelled", error="DeepSpace child run cancelled")

        if outcome.exit_code != 0:
            return KineSisSdkResult(
                status="failed",
                error=(
                    f"KineSis SDK exited with code {outcome.exit_code}: "
                    f"{stderr_data.decode(errors='replace')[:500]}"
                ),
            )

        try:
            response = json.loads(stdout_data.decode())
        except json.JSONDecodeError as exc:
            return KineSisSdkResult(
                status="failed",
                error=f"KineSis SDK returned invalid JSON: {exc}",
            )

        if "error" in response:
            return KineSisSdkResult(
                status="failed",
                error=response["error"].get("message", "Unknown KineSis SDK error"),
            )

        result = response.get("result", {})
        return KineSisSdkResult(
            status="completed",
            answer=result.get("answer", ""),
            tool_calls=result.get("tool_calls", []),
            stop_reason=result.get("stop_reason", "completed"),
            rounds=result.get("rounds", 0),
        )


def create_kinesis_sdk_provider(
    *,
    command: str = "kinesis",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    kinesis_home: str | None = None,
    profile: str = "sdk",
    patches: list[str] | None = None,
    provider: str = "averqel-official",
    model: str = "kinesis-v4-flash",
    initialize_timeout_ms: int = 30_000,
    dispose_eof_grace_ms: int = 6_000,
    dispose_grace_ms: int = 3_000,
) -> KineSisSdkProvider:
    """Factory for the KineSis SDK subagent provider."""
    return KineSisSdkProvider(
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        kinesis_home=kinesis_home,
        profile=profile,
        patches=patches,
        provider=provider,
        model=model,
        initialize_timeout_ms=initialize_timeout_ms,
        dispose_eof_grace_ms=dispose_eof_grace_ms,
        dispose_grace_ms=dispose_grace_ms,
    )


__all__ = [
    "KineSisSdkRunSpec",
    "KineSisSdkResult",
    "KineSisSdkProvider",
    "create_kinesis_sdk_provider",
]
