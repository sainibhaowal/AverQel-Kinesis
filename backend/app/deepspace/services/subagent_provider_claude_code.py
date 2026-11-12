"""Claude Code subagent backend for DeepSpace.

Python-native port of the DeepSeek Harness subagent-claude-code package.
Spawns Claude Code CLI as a subagent process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClaudeCodeRunSpec:
    """Spec for a Claude Code subagent run."""

    task: str
    claude_command: str = "claude"
    args: list[str] = field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    max_turns: int = 10
    permission_mode: str = "dontAsk"


@dataclass
class ClaudeCodeResult:
    """Result of a Claude Code subagent run."""

    status: str = "completed"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stop_reason: str = "completed"


class ClaudeCodeProvider:
    """Claude Code subagent provider."""

    name = "claude-code"

    def __init__(
        self,
        command: str = "claude",
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
        spec: ClaudeCodeRunSpec | Any,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> ClaudeCodeResult:
        """Run through the registered adapter so cancellation and policy stay consistent."""
        from app.deepspace.services.subagent_providers import (
            ClaudeCodeSubagentProvider,
            SubagentRunSpec,
        )

        child_spec = SubagentRunSpec(
            task=str(getattr(spec, "task", spec)),
            persona=getattr(spec, "persona", None),
            model=getattr(spec, "model", None),
            cwd=self.base_cwd or getattr(spec, "cwd", ".") or ".",
            env={**self.base_env, **(getattr(spec, "env", {}) or {})},
        )
        result = await ClaudeCodeSubagentProvider(
            command=self.command,
            args=self.args or None,
            permission_mode=str(getattr(spec, "permission_mode", "dontAsk")),
            max_turns=int(getattr(spec, "max_turns", 10)),
        ).run(child_spec, cancel_check)
        stop_reason = (
            "completed"
            if result.status == "completed"
            else "cancelled" if result.status == "cancelled" else "error"
        )
        return ClaudeCodeResult(
            status=result.status,
            answer=result.answer,
            tool_calls=result.tool_calls,
            error=result.error,
            stop_reason=stop_reason,
        )


__all__ = ["ClaudeCodeRunSpec", "ClaudeCodeResult", "ClaudeCodeProvider"]
