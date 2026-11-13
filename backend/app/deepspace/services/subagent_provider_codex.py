"""Codex subagent backend for DeepSpace.

Python-native port of the AverQel KineSis subagent-codex package.
Spawns Codex CLI as a subagent process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CodexRunSpec:
    """Spec for a Codex subagent run."""

    task: str
    codex_command: str = "codex"
    args: list[str] = field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    ask_for_approval: str = "never"
    sandbox_mode: str = "read-only"


@dataclass
class CodexResult:
    """Result of a Codex subagent run."""

    status: str = "completed"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stop_reason: str = "completed"


class CodexProvider:
    """Codex subagent provider."""

    name = "codex"

    def __init__(
        self,
        command: str = "codex",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.base_env = dict(env or {})
        self.base_cwd = cwd

    async def run(
        self,
        spec: CodexRunSpec | Any,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> CodexResult:
        """Run through the registered JSONL adapter with shared cancellation handling."""
        from app.deepspace.services.subagent_providers import (
            CodexSubagentProvider,
            SubagentRunSpec,
        )

        child_spec = SubagentRunSpec(
            task=str(getattr(spec, "task", spec)),
            persona=getattr(spec, "persona", None),
            model=getattr(spec, "model", None),
            cwd=self.base_cwd or getattr(spec, "cwd", ".") or ".",
            env={**self.base_env, **(getattr(spec, "env", {}) or {})},
        )
        result = await CodexSubagentProvider(
            command=self.command,
            args=self.args or None,
            ask_for_approval=str(getattr(spec, "ask_for_approval", "never")),
            sandbox_mode=str(getattr(spec, "sandbox_mode", "read-only")),
        ).run(child_spec, cancel_check)
        stop_reason = (
            "completed"
            if result.status == "completed"
            else "cancelled" if result.status == "cancelled" else "error"
        )
        return CodexResult(
            status=result.status,
            answer=result.answer,
            tool_calls=result.tool_calls,
            error=result.error,
            stop_reason=stop_reason,
        )


__all__ = ["CodexRunSpec", "CodexResult", "CodexProvider"]
