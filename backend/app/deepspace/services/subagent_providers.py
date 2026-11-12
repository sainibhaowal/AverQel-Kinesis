"""Out-of-process subagent backends for DeepSpace.

Provides configured ACP, Claude Code, Codex, and DSH SDK child runtimes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from app.deepspace.services.subprocess_stdio import (
    SubprocessHandle,
    SubprocessStdio,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)


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


async def _communicate_with_cancellation(
    handle: SubprocessHandle,
    cancel_check: Callable[[], bool],
    *,
    input_data: bytes | None = None,
    terminate_grace_seconds: float = 3.0,
) -> tuple[bytes, bytes, bool]:
    """Drain a CLI child while polling cancellation and always reap it."""
    process = handle.process
    communication = asyncio.create_task(process.communicate(input=input_data))

    async def stop() -> tuple[bytes, bytes]:
        if process.returncode is None:
            try:
                handle.terminate()
            except ProcessLookupError:
                pass
        try:
            return await asyncio.wait_for(
                asyncio.shield(communication), timeout=terminate_grace_seconds
            )
        except TimeoutError:
            await handle.terminate_and_wait()
            return await communication

    try:
        while True:
            done, _ = await asyncio.wait({communication}, timeout=0.1)
            if done:
                stdout, stderr = await communication
                return stdout, stderr, False
            if cancel_check():
                stdout, stderr = await stop()
                return stdout, stderr, True
    except asyncio.CancelledError:
        try:
            await asyncio.shield(stop())
        except Exception:  # noqa: BLE001 - preserve caller cancellation
            logger.warning("Subagent CLI cleanup after task cancellation failed", exc_info=True)
        raise


def _prompt_with_persona(spec: SubagentRunSpec) -> str:
    """Keep child persona instructions attached to the task for CLI backends."""
    persona = str(spec.persona or "").strip()
    if not persona:
        return spec.task
    return f"Child instructions (persona):\n{persona}\n\nTask:\n{spec.task}"


def _parse_codex_jsonl(output: str) -> tuple[str, list[dict[str, Any]], str | None]:
    """Extract a completed Codex answer and activity from ``codex exec --json``."""
    answers: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    turn_completed = False
    failure: str | None = None

    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if event_type == "turn.completed":
            turn_completed = True
        elif event_type in {"turn.failed", "error"}:
            failure = str(event.get("message") or event.get("error") or event)

        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or item.get("item_type")
        if event_type == "item.completed" and item_type in {
            "agent_message",
            "assistant_message",
        }:
            message = item.get("text")
            if isinstance(message, str) and message.strip():
                answers.append(message.strip())
        elif event_type == "item.completed" and item_type:
            activity = {"type": str(item_type), "item": item}
            activity["name"] = str(item.get("name") or item.get("tool_name") or item_type)
            tool_calls.append(activity)

    answer = "\n".join(answers).strip()
    if failure:
        return answer, tool_calls, f"Codex reported a failed turn: {failure}"
    if not turn_completed:
        return answer, tool_calls, "Codex JSONL output ended without a turn.completed event"
    if not answer:
        return answer, tool_calls, "Codex completed without a final agent message"
    return answer, tool_calls, None


class AcpSubagentProvider(SubagentProvider):
    """ACP (Agent Client Protocol) subagent provider.

    Spawns a child ACP agent process via stdio JSON-RPC.
    """

    name = "acp"

    def __init__(
        self,
        command: str = "dsh",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        permission: str = "reject",
        dispose_eof_grace_ms: int = 5_000,
        dispose_grace_ms: int = 3_000,
    ):
        self.command = command
        self.args = list(args or ["--profile", "acp"])
        self.base_env = dict(env or {})
        self.base_cwd = cwd
        self.permission = permission
        self.dispose_eof_grace_ms = dispose_eof_grace_ms
        self.dispose_grace_ms = dispose_grace_ms

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        try:
            from app.deepspace.services.subagent_acp_backend import AcpProvider

            provider = AcpProvider(
                command=self.command,
                args=self.args,
                permission=self.permission,  # type: ignore[arg-type]
                env=self.base_env,
                cwd=self.base_cwd,
                dispose_eof_grace_ms=self.dispose_eof_grace_ms,
                dispose_grace_ms=self.dispose_grace_ms,
            )
            outcome = await provider.run(spec, cancel_check)
            return SubagentResult(
                status=outcome.status,  # type: ignore[arg-type]
                answer=outcome.answer,
                tool_calls=outcome.tool_calls,
                error=outcome.error,
            )
        except Exception as exc:  # noqa: BLE001 - provider failure is a result
            return SubagentResult(status="failed", answer="", error=str(exc))


class ClaudeCodeSubagentProvider(SubagentProvider):
    """Claude Code subagent provider.

    Spawns Claude Code in print mode using the configured permission mode.
    """

    name = "claude-code"

    def __init__(
        self,
        command: str = "claude",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        permission_mode: str = "dontAsk",
        max_turns: int = 10,
    ):
        max_turns = int(max_turns)
        if max_turns < 1:
            raise ValueError("max_turns must be at least one")
        self.command = command
        self.base_args = list(args or ["-p"])
        self.base_env = dict(env or {})
        self.permission_mode = permission_mode
        self.max_turns = max_turns

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        env = {**self.base_env, **spec.env}

        cwd = spec.cwd

        # Build command
        args = list(self.base_args)
        if self.permission_mode:
            args.extend(["--permission-mode", self.permission_mode])
        args.extend(["--max-turns", str(self.max_turns)])
        if spec.model:
            args.extend(["--model", spec.model])
        args.append(_prompt_with_persona(spec))

        try:
            handle = await spawn_with_stdio(
                [self.command, *args],
                cwd=cwd,
                env=env,
                stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
                grace_ms=3_000,
            )

            process = handle.process
            stdout, stderr, cancelled = await _communicate_with_cancellation(handle, cancel_check)
            if cancelled:
                return SubagentResult(
                    status="cancelled",
                    answer=stdout.decode("utf-8", errors="replace").strip(),
                    error="Cancelled by parent",
                )

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

    Spawns Codex's non-interactive JSONL command and validates the terminal event.
    """

    name = "codex"

    def __init__(
        self,
        command: str = "codex",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        ask_for_approval: str = "never",
        sandbox_mode: str = "read-only",
    ):
        if ask_for_approval not in {"never", "on-request"}:
            raise ValueError("ask_for_approval must be 'never' or 'on-request'")
        if sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("unsupported Codex sandbox_mode")
        self.command = command
        self.base_args = list(args or ["exec", "--json"])
        self.base_env = dict(env or {})
        self.ask_for_approval = ask_for_approval
        self.sandbox_mode = sandbox_mode

    async def run(
        self,
        spec: SubagentRunSpec,
        cancel_check: Callable[[], bool],
    ) -> SubagentResult:
        env = {**self.base_env, **spec.env}

        cwd = spec.cwd

        # Codex CLI's noninteractive interface is `exec --json`; it does not
        # implement the ACP `--stdio` or Claude's `--permission-mode` flags.
        args = list(self.base_args)
        if not any(
            arg == "-s" or arg == "--sandbox" or arg.startswith("--sandbox=") for arg in args
        ):
            args.extend(["--sandbox", self.sandbox_mode])
        if not any(
            arg == "-a" or arg == "--ask-for-approval" or arg.startswith("--ask-for-approval=")
            for arg in args
        ):
            args.extend(["--ask-for-approval", self.ask_for_approval])
        if spec.model and not any(
            arg == "-m" or arg == "--model" or arg.startswith("--model=") for arg in args
        ):
            args.extend(["--model", spec.model])
        if not any(arg in {"--json", "--experimental-json"} for arg in args):
            args.append("--json")
        args.append("-")

        try:
            handle = await spawn_with_stdio(
                [self.command, *args],
                cwd=cwd,
                env=env,
                stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
                grace_ms=3_000,
            )

            process = handle.process
            stdout, stderr, cancelled = await _communicate_with_cancellation(
                handle,
                cancel_check,
                input_data=_prompt_with_persona(spec).encode("utf-8"),
            )
            if cancelled:
                output = stdout.decode("utf-8", errors="replace")
                answer, tool_calls, _ = _parse_codex_jsonl(output)
                return SubagentResult(
                    status="cancelled",
                    answer=answer,
                    tool_calls=tool_calls,
                    error="Cancelled by parent",
                )

            output = stdout.decode("utf-8", errors="replace")
            answer, tool_calls, parse_error = _parse_codex_jsonl(output)
            if process.returncode != 0:
                error = stderr.decode("utf-8", errors="replace").strip()
                return SubagentResult(
                    status="failed",
                    answer=answer,
                    tool_calls=tool_calls,
                    error=error or f"Codex exited with status {process.returncode}",
                )
            if parse_error:
                return SubagentResult(
                    status="failed",
                    answer=answer,
                    tool_calls=tool_calls,
                    error=parse_error,
                )

            return SubagentResult(
                status="completed",
                answer=answer,
                tool_calls=tool_calls,
            )

        except Exception as e:
            return SubagentResult(status="failed", answer="", error=str(e))


# Provider registry
_PROVIDERS: dict[str, SubagentProvider] = {}
_SETTINGS_PROVIDER_NAMES: set[str] = set()


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
    for name in _SETTINGS_PROVIDER_NAMES:
        _PROVIDERS.pop(name, None)
    _SETTINGS_PROVIDER_NAMES.clear()
    providers_config = getattr(settings, "deepspace_subagent_providers", {})
    default_backend = getattr(settings, "deepspace_subagent_default_backend", "in-process")

    for name, config in providers_config.items():
        try:
            if name == "neosis-sdk":
                # Kinesis is a standalone product and must not launch the
                # separate NeoSIS runtime. Keep stale configuration inert.
                import logging

                logging.getLogger(__name__).warning(
                    "Ignoring unsupported NeoSIS subagent runtime configuration"
                )
                continue
            if name == "acp":
                provider = AcpSubagentProvider(
                    command=config.get("command", "dsh"),
                    args=config.get("args", ["--profile", "acp"]),
                    env=config.get("env", {}),
                    cwd=config.get("cwd"),
                    permission=config.get("permission", "reject"),
                    dispose_eof_grace_ms=config.get("dispose_eof_grace_ms", 5_000),
                    dispose_grace_ms=config.get("dispose_grace_ms", 3_000),
                )
            elif name == "claude-code":
                provider = ClaudeCodeSubagentProvider(
                    command=config.get("command", "claude"),
                    args=config.get("args", ["-p"]),
                    env=config.get("env", {}),
                    permission_mode=config.get("permission_mode", "dontAsk"),
                    max_turns=config.get("max_turns", 10),
                )
            elif name == "codex":
                provider = CodexSubagentProvider(
                    command=config.get("command", "codex"),
                    args=config.get("args", ["exec"]),
                    env=config.get("env", {}),
                    ask_for_approval=config.get("ask_for_approval", "never"),
                    sandbox_mode=config.get("sandbox_mode", "read-only"),
                )
            elif name == "dsh-sdk":
                from app.deepspace.services.subagent_provider_dsh_sdk import DshSdkProvider

                provider = DshSdkProvider(
                    command=config.get("command", "dsh"),
                    args=config.get("args", []),
                    env=config.get("env", {}),
                    cwd=config.get("cwd"),
                    dsh_home=config.get("dsh_home"),
                    profile=config.get("profile", "sdk"),
                    patches=config.get("patches", []),
                    provider=config.get("provider", "deepseek-official"),
                    model=config.get("model", "deepseek-v4-flash"),
                    initialize_timeout_ms=config.get("initialize_timeout_ms", 30_000),
                    dispose_eof_grace_ms=config.get("dispose_eof_grace_ms", 6_000),
                    dispose_grace_ms=config.get("dispose_grace_ms", 3_000),
                )
            else:
                continue

            register_provider(provider)
            _SETTINGS_PROVIDER_NAMES.add(name)
        except Exception as e:
            # Log but don't fail initialization
            import logging

            logging.getLogger(__name__).warning(
                f"Failed to initialize subagent provider '{name}': {e}"
            )

    if default_backend not in {"in-process", *_PROVIDERS}:
        logger.warning(
            "Configured DeepSpace subagent default backend '%s' is unavailable; "
            "subagent calls will fail closed until that backend is configured",
            default_backend,
        )
