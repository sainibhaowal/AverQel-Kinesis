"""Out-of-process subagent backends for DeepSpace.

Ported from NeoSIS @averqel/neosis-subagent-acp, -claude-code, -codex, -neosis-sdk.
Provides provider backends for spawning child agents in separate processes.
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SubagentRunSpec:
    """Specification for a subagent run."""

    task: str
    persona: str | None = None
    model: str | None = None
    provider: str | None = None
    tool_allow: list[str] | None = None
    tool_deny: list[str] | None = None
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    depth: int = 0
    parent_run_id: UUID | None = None
    conversation_id: UUID | None = None
    tenant_id: UUID | None = None
    user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SubagentResult:
    """Result of a subagent run."""

    status: Literal["completed", "failed", "cancelled"]
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None


class SubagentProvider(ABC):
    """Abstract base class for subagent providers."""

    name: str

    @abstractmethod
    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        """Run a subagent and return its result."""
        ...


class InProcessSubagentProvider(SubagentProvider):
    """In-process subagent provider (existing implementation)."""

    name = "in-process"

    def __init__(self, chat_service: Any):
        self.chat_service = chat_service

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        # This would delegate to the existing subagent_runner.py
        # For now, return a placeholder
        return SubagentResult(
            status="failed",
            answer="",
            error="In-process provider not yet wired",
        )


class AcpSubagentProvider(SubagentProvider):
    """ACP (Agent Client Protocol) subagent provider.

    Spawns a child ACP agent process via stdio JSON-RPC.
    """

    name = "acp"

    def __init__(
        self,
        command: str = "neosis-acp-child",
        args: list[str] = field(default_factory=list),
        env: dict[str, str] = field(default_factory=dict),
        cwd: str | None = None,
    ):
        self.command = command
        self.args = args
        self.base_env = env
        self.base_cwd = cwd

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        # Prepare environment
        env = os.environ.copy()
        env.update(self.base_env)
        env.update(spec.env)

        # Prepare working directory
        cwd = self.base_cwd or spec.cwd

        # Build the ACP initialization request
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "capabilities": {},
                "clientInfo": {"name": "deepspace-acp-parent", "version": "1.0"},
            },
        }

        # Build the task request
        task_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "prompt",
            "params": {
                "prompt": spec.task,
                "model": spec.model,
                "systemPrompt": spec.persona,
            },
        }

        try:
            # Spawn the ACP child process
            process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            # Send initialize
            process.stdin.write((json.dumps(init_request) + "\n").encode())
            await process.stdin.drain()

            # Read initialize response
            init_response_line = await process.stdout.readline()
            if not init_response_line:
                stderr = await process.stderr.read()
                return SubagentResult(
                    status="failed",
                    answer="",
                    error=f"ACP child failed to initialize: {stderr.decode()}",
                )

            # Send task
            process.stdin.write((json.dumps(task_request) + "\n").encode())
            await process.stdin.drain()

            # Read responses until completion
            answer_parts = []
            tool_calls = []

            while True:
                if cancel_check():
                    process.terminate()
                    await process.wait()
                    return SubagentResult(status="cancelled", answer="", tool_calls=tool_calls)

                line = await process.stdout.readline()
                if not line:
                    break

                try:
                    response = json.loads(line.decode())
                    if response.get("method") == "prompt/update":
                        # Streaming update
                        update = response.get("params", {})
                        if "text" in update:
                            answer_parts.append(update["text"])
                    elif response.get("method") == "tool/call":
                        tool_calls.append(response.get("params", {}))
                    elif "result" in response and response.get("id") == 2:
                        # Final result
                        result = response["result"]
                        if isinstance(result, dict) and "answer" in result:
                            answer_parts.append(result["answer"])
                        break
                except json.JSONDecodeError:
                    continue

            await process.wait()

            return SubagentResult(
                status="completed" if process.returncode == 0 else "failed",
                answer="".join(answer_parts),
                tool_calls=tool_calls,
                error=(
                    None
                    if process.returncode == 0
                    else f"Process exited with code {process.returncode}"
                ),
            )

        except Exception as e:
            return SubagentResult(status="failed", answer="", error=str(e))


class ClaudeCodeSubagentProvider(SubagentProvider):
    """Claude Code subagent provider.

    Spawns `claude --dangerously-skip-permissions -p <task>` via subprocess.
    """

    name = "claude-code"

    def __init__(
        self,
        command: str = "claude",
        args: list[str] = field(default_factory=lambda: ["--dangerously-skip-permissions", "-p"]),
        env: dict[str, str] = field(default_factory=dict),
        permission_mode: str = "dontAsk",
    ):
        self.command = command
        self.base_args = args
        self.base_env = env
        self.permission_mode = permission_mode

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        env = os.environ.copy()
        env.update(self.base_env)
        env.update(spec.env)

        cwd = spec.cwd

        # Build command
        args = list(self.base_args)
        if self.permission_mode:
            args.extend(["--permission-mode", self.permission_mode])
        args.append(spec.task)

        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            # Read output
            stdout, stderr = await process.communicate()

            if cancel_check():
                return SubagentResult(status="cancelled", answer="", error="Cancelled by parent")

            answer = stdout.decode("utf-8", errors="replace").strip()

            return SubagentResult(
                status="completed" if process.returncode == 0 else "failed",
                answer=answer,
                error=None if process.returncode == 0 else stderr.decode("utf-8", errors="replace"),
            )

        except Exception as e:
            return SubagentResult(status="failed", answer="", error=str(e))


class CodexSubagentProvider(SubagentProvider):
    """Codex subagent provider.

    Spawns `codex exec --stdio` via subprocess.
    """

    name = "codex"

    def __init__(
        self,
        command: str = "codex",
        args: list[str] = field(default_factory=lambda: ["exec", "--stdio"]),
        env: dict[str, str] = field(default_factory=dict),
        permission_mode: str = "auto",
    ):
        self.command = command
        self.base_args = args
        self.base_env = env
        self.permission_mode = permission_mode

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        env = os.environ.copy()
        env.update(self.base_env)
        env.update(spec.env)

        cwd = spec.cwd

        # Build command
        args = list(self.base_args)
        if self.permission_mode:
            args.extend(["--permission-mode", self.permission_mode])
        args.append(spec.task)

        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            stdout, stderr = await process.communicate()

            if cancel_check():
                return SubagentResult(status="cancelled", answer="", error="Cancelled by parent")

            answer = stdout.decode("utf-8", errors="replace").strip()

            return SubagentResult(
                status="completed" if process.returncode == 0 else "failed",
                answer=answer,
                error=None if process.returncode == 0 else stderr.decode("utf-8", errors="replace"),
            )

        except Exception as e:
            return SubagentResult(status="failed", answer="", error=str(e))


class NeosisSdkSubagentProvider(SubagentProvider):
    """NeoSIS SDK subagent provider.

    Spawns `neosis headless --profile sdk-minimal` via stdio JSON-RPC.
    """

    name = "neosis-sdk"

    def __init__(
        self,
        command: str = "neosis",
        args: list[str] = field(default_factory=lambda: ["headless", "--profile", "sdk-minimal"]),
        env: dict[str, str] = field(default_factory=dict),
    ):
        self.command = command
        self.base_args = args
        self.base_env = env

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        env = os.environ.copy()
        env.update(self.base_env)
        env.update(spec.env)

        cwd = spec.cwd

        # Build SDK request
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "capabilities": {},
            },
        }

        turn_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "turn",
            "params": {
                "prompt": spec.task,
                "model": spec.model,
                "systemPrompt": spec.persona,
            },
        }

        try:
            child_args = list(self.base_args)
            if spec.tenant_id is not None:
                child_args.extend(["--tenant", str(spec.tenant_id)])
            if spec.user_id is not None:
                child_args.extend(["--user", str(spec.user_id)])
            process = await asyncio.create_subprocess_exec(
                self.command,
                *child_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            # Send initialize
            process.stdin.write((json.dumps(init_request) + "\n").encode())
            await process.stdin.drain()

            # Read initialize response
            init_response_line = await process.stdout.readline()
            if not init_response_line:
                stderr = await process.stderr.read()
                return SubagentResult(
                    status="failed",
                    answer="",
                    error=f"SDK child failed to initialize: {stderr.decode()}",
                )

            # Send turn request
            process.stdin.write((json.dumps(turn_request) + "\n").encode())
            await process.stdin.drain()

            # Read responses
            answer_parts = []
            tool_calls = []
            saw_update = False
            final_result: dict[str, Any] | None = None

            while True:
                if cancel_check():
                    process.terminate()
                    await process.wait()
                    return SubagentResult(status="cancelled", answer="", tool_calls=tool_calls)

                line = await process.stdout.readline()
                if not line:
                    break

                try:
                    response = json.loads(line.decode())
                    if response.get("method") == "turn/update":
                        update = response.get("params", {})
                        if "text" in update:
                            answer_parts.append(update["text"])
                            saw_update = True
                    elif response.get("method") == "tool/call":
                        tool_calls.append(response.get("params", {}))
                    elif "result" in response and response.get("id") == 2:
                        result = response["result"]
                        if isinstance(result, dict):
                            final_result = result
                        if isinstance(result, dict) and "answer" in result and not saw_update:
                            answer_parts.append(result["answer"])
                        break
                except json.JSONDecodeError:
                    continue

            await process.wait()

            return SubagentResult(
                status=(
                    "completed"
                    if process.returncode == 0
                    and (final_result or {}).get("status", "completed") == "completed"
                    else "failed"
                ),
                answer="".join(answer_parts),
                tool_calls=tool_calls,
                error=(final_result or {}).get("error")
                or (
                    None
                    if process.returncode == 0
                    else f"Process exited with code {process.returncode}"
                ),
            )

        except Exception as e:
            return SubagentResult(status="failed", answer="", error=str(e))


# Provider registry
_PROVIDERS: dict[str, SubagentProvider] = {}


def register_provider(provider: SubagentProvider) -> None:
    """Register a subagent provider."""
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> SubagentProvider | None:
    """Get a registered subagent provider."""
    return _PROVIDERS.get(name)


def list_providers() -> list[str]:
    """List registered provider names."""
    return list(_PROVIDERS.keys())


def initialize_providers_from_settings(settings: Any) -> None:
    """Initialize providers from settings configuration."""
    providers_config = getattr(settings, "deepspace_subagent_providers", {})
    default_backend = getattr(settings, "deepspace_subagent_default_backend", "in-process")

    for name, config in providers_config.items():
        try:
            if name == "acp":
                provider = AcpSubagentProvider(
                    command=config.get("command", "neosis-acp-child"),
                    args=config.get("args", []),
                    env=config.get("env", {}),
                    cwd=config.get("cwd"),
                )
            elif name == "claude-code":
                provider = ClaudeCodeSubagentProvider(
                    command=config.get("command", "claude"),
                    args=config.get("args", ["--dangerously-skip-permissions", "-p"]),
                    env=config.get("env", {}),
                    permission_mode=config.get("permission_mode", "dontAsk"),
                )
            elif name == "codex":
                provider = CodexSubagentProvider(
                    command=config.get("command", "codex"),
                    args=config.get("args", ["exec", "--stdio"]),
                    env=config.get("env", {}),
                    permission_mode=config.get("permission_mode", "auto"),
                )
            elif name == "neosis-sdk":
                provider = NeosisSdkSubagentProvider(
                    command=config.get("command", "neosis"),
                    args=config.get("args", ["headless", "--profile", "sdk-minimal"]),
                    env=config.get("env", {}),
                )
            else:
                continue

            register_provider(provider)
        except Exception as e:
            # Log but don't fail initialization
            import logging

            logging.getLogger(__name__).warning(
                f"Failed to initialize subagent provider '{name}': {e}"
            )

    # Ensure default backend is registered
    if default_backend not in _PROVIDERS and default_backend != "in-process":
        # Try to create a default instance
        pass
