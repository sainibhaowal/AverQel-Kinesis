"""Runtime invariant registry for DeepSpace.

Provides declarative invariant checks that can be asserted at runtime.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ──────────────────────────────────────────────────────────────────────
# Invariant Types
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """Result of an invariant check."""

    name: str
    passed: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    severity: Literal[error, warning, info] = "error"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class InvariantContext:
    """Context passed to invariant checks."""

    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID | None = None
    turn: int | None = None
    step: int | None = None
    tool_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Invariant(ABC):
    """Base class for runtime invariants."""

    name: str
    severity: Literal[error, warning, info] = "error"
    description: str = ""
    tags: tuple[str, ...] = ()

    @abstractmethod
    async def check(self, context: InvariantContext) -> InvariantResult:
        """Check the invariant."""
        pass


class CompositeInvariant(Invariant):
    """Combine multiple invariants with AND/OR logic."""

    def __init__(
        self,
        name: str,
        invariants: list[Invariant],
        operator: Literal["and", "or"] = "and",
        severity: Literal[error, warning, info] = "error",
        description: str = "",
    ):
        self.name = name
        self._invariants = invariants
        self._operator = operator
        self.severity = severity
        self.description = description
        self.tags = ("composite",)

    async def check(self, context: InvariantContext) -> InvariantResult:
        results = await asyncio.gather(*[inv.check(context) for inv in self._invariants])
        passed = (
            all(r.passed for r in results)
            if self._operator == "and"
            else any(r.passed for r in results)
        )
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"{self._operator.upper()}: {', '.join(r.name for r in results)}",
            details={"results": [r.__dict__ for r in results]},
            severity=self.severity,
        )


# ──────────────────────────────────────────────────────────────────────
# Built-in Invariants
# ──────────────────────────────────────────────────────────────────────


class ToolCallBudgetInvariant(Invariant):
    """Ensure tool calls don't exceed budget."""

    name = "tool_call_budget"
    severity = "error"
    description = "Tool calls per turn must not exceed configured budget"
    tags = ("budget", "tools")

    def __init__(self, max_calls: int = 50):
        self.max_calls = max_calls

    async def check(self, context: InvariantContext) -> InvariantResult:
        count = context.extra.get("tool_call_count")
        if not isinstance(count, int):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Tool call count was not measured for this turn",
                details={"max_calls": self.max_calls},
                severity="info",
            )
        passed = count <= self.max_calls
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Tool calls: {count} / {self.max_calls}",
            details={"tool_call_count": count, "max_calls": self.max_calls},
            severity=self.severity if not passed else "info",
        )


class ProviderLatencyInvariant(Invariant):
    """Ensure provider calls complete within timeout."""

    name = "provider_latency"
    severity = "warning"
    description = "Provider calls must complete within configured timeout"
    tags = ("latency", "provider")

    def __init__(self, max_latency_ms: int = 120_000):
        self.max_latency_ms = max_latency_ms

    async def check(self, context: InvariantContext) -> InvariantResult:
        latency = context.extra.get("provider_latency_ms")
        if not isinstance(latency, int):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Provider latency was not measured for this turn",
                details={"max_latency_ms": self.max_latency_ms},
                severity="info",
            )
        passed = latency <= self.max_latency_ms
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Provider latency: {latency}ms / {self.max_latency_ms}ms",
            details={"provider_latency_ms": latency, "max_latency_ms": self.max_latency_ms},
            severity=self.severity if not passed else "info",
        )


class ConversationTurnLimitInvariant(Invariant):
    """Ensure conversations don't exceed max turns."""

    name = "conversation_turn_limit"
    severity = "error"
    description = "Conversations must not exceed maximum turn count"
    tags = ("turns", "limits")

    def __init__(self, max_turns: int = 1000):
        self.max_turns = max_turns

    async def check(self, context: InvariantContext) -> InvariantResult:
        turn = context.extra.get("conversation_turn", context.turn)
        if not isinstance(turn, int):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Conversation turn was not measured",
                details={"max_turns": self.max_turns},
                severity="info",
            )
        passed = turn <= self.max_turns
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Conversation turn: {turn} / {self.max_turns}",
            details={"conversation_turn": turn, "max_turns": self.max_turns},
            severity=self.severity if not passed else "info",
        )


class SessionEventSequenceInvariant(Invariant):
    """Ensure session event sequences are monotonic."""

    name = "session_event_sequence"
    severity = "error"
    description = "Session event sequence numbers must be strictly increasing"
    tags = ("events", "integrity")

    async def check(self, context: InvariantContext) -> InvariantResult:
        sequences = context.extra.get("session_event_sequences")
        if not isinstance(sequences, (list, tuple)):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Session event sequences were not measured",
                severity="info",
            )
        ordered = all(
            isinstance(left, int) and isinstance(right, int) and left < right
            for left, right in zip(sequences, sequences[1:], strict=False)
        )
        return InvariantResult(
            name=self.name,
            passed=ordered,
            message=(
                "Session event sequence is strictly increasing"
                if ordered
                else "Session event sequence is not strictly increasing"
            ),
            details={"sequences": list(sequences)},
            severity=self.severity if not ordered else "info",
        )


class SubagentDepthInvariant(Invariant):
    """Ensure subagent delegation depth doesn't exceed limit."""

    name = "subagent_depth"
    severity = "error"
    description = "Subagent delegation depth must not exceed configured maximum"
    tags = ("subagents", "delegation")

    def __init__(self, max_depth: int = 16):
        self.max_depth = max_depth

    async def check(self, context: InvariantContext) -> InvariantResult:
        depth = context.extra.get("subagent_depth")
        if not isinstance(depth, int):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Subagent depth was not measured for this turn",
                details={"max_depth": self.max_depth},
                severity="info",
            )
        passed = depth <= self.max_depth
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Subagent depth: {depth} / {self.max_depth}",
            details={"subagent_depth": depth, "max_depth": self.max_depth},
            severity=self.severity if not passed else "info",
        )


class WorkspacePathInvariant(Invariant):
    """Ensure all file operations stay within workspace."""

    name = "workspace_path"
    severity = "error"
    description = "All file operations must resolve within the workspace root"
    tags = ("workspace", "security")

    async def check(self, context: InvariantContext) -> InvariantResult:
        violations = context.extra.get("workspace_violations")
        if not isinstance(violations, (list, tuple)):
            return InvariantResult(
                name=self.name,
                passed=True,
                message="Workspace path containment was not measured",
                severity="info",
            )
        passed = not violations
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=(
                "Workspace path containment verified"
                if passed
                else f"{len(violations)} path(s) escaped the workspace root"
            ),
            details={"violations": [str(item) for item in violations[:20]]},
            severity=self.severity if not passed else "info",
        )


class MemoryUsageInvariant(Invariant):
    """Ensure memory usage stays within bounds."""

    name = "memory_usage"
    severity = "warning"
    description = "Process memory usage must stay within bounds"
    tags = ("memory", "resources")

    def __init__(self, max_mb: int = 2048):
        self.max_mb = max_mb

    async def check(self, context: InvariantContext) -> InvariantResult:
        import psutil

        process = psutil.Process()
        mem_mb = process.memory_info().rss / (1024 * 1024)
        passed = mem_mb <= self.max_mb
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Memory usage: {mem_mb:.1f}MB / {self.max_mb}MB",
            details={"current_mb": mem_mb, "max_mb": self.max_mb},
            severity=self.severity if not passed else "info",
        )


class DiskSpaceInvariant(Invariant):
    """Ensure sufficient disk space."""

    name = "disk_space"
    severity = "error"
    description = "Sufficient disk space must be available"
    tags = ("disk", "resources")

    def __init__(self, min_gb: float = 1.0, path: str = "/"):
        self.min_gb = min_gb
        self.path = path

    async def check(self, context: InvariantContext) -> InvariantResult:
        import shutil

        free_gb = shutil.disk_usage(self.path).free / (1024**3)
        passed = free_gb >= self.min_gb
        return InvariantResult(
            name=self.name,
            passed=passed,
            message=f"Disk space: {free_gb:.2f}GB free (min {self.min_gb}GB)",
            details={"free_gb": free_gb, "min_gb": self.min_gb, "path": self.path},
            severity=self.severity if not passed else "info",
        )


# ──────────────────────────────────────────────────────────────────────
# Invariant Registry
# ──────────────────────────────────────────────────────────────────────


class InvariantRegistry:
    """Registry of runtime invariants with execution engine."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._invariants: dict[str, Invariant] = {}
        self._enabled: set[str] = set()
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in invariants from settings."""
        # Tool call budget
        budget = getattr(self.settings, "deepspace_invariant_tool_budget", 50)
        self.register(ToolCallBudgetInvariant(max_calls=budget))

        # Provider latency
        latency = getattr(self.settings, "deepspace_invariant_provider_latency_ms", 120_000)
        self.register(ProviderLatencyInvariant(max_latency_ms=latency))

        # Turn limit
        turns = getattr(self.settings, "deepspace_invariant_max_turns", 1000)
        self.register(ConversationTurnLimitInvariant(max_turns=turns))

        # Event sequence
        self.register(SessionEventSequenceInvariant())

        # Subagent depth
        depth = getattr(self.settings, "deepspace_invariant_subagent_depth", 16)
        self.register(SubagentDepthInvariant(max_depth=depth))

        # Workspace path
        self.register(WorkspacePathInvariant())

        # Memory
        mem = getattr(self.settings, "deepspace_invariant_max_memory_mb", 2048)
        self.register(MemoryUsageInvariant(max_mb=mem))

        # Disk
        disk = getattr(self.settings, "deepspace_invariant_min_disk_gb", 1.0)
        self.register(DiskSpaceInvariant(min_gb=disk))

        # Enable all by default
        self._enabled = set(self._invariants.keys())

    def register(self, invariant: Invariant) -> None:
        """Register an invariant."""
        self._invariants[invariant.name] = invariant

    def unregister(self, name: str) -> bool:
        """Unregister an invariant."""
        if name in self._invariants:
            del self._invariants[name]
            self._enabled.discard(name)
            return True
        return False

    def enable(self, name: str) -> bool:
        """Enable an invariant."""
        if name in self._invariants:
            self._enabled.add(name)
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable an invariant."""
        if name in self._enabled:
            self._enabled.remove(name)
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        """Check if invariant is enabled."""
        return name in self._enabled

    def get(self, name: str) -> Invariant | None:
        """Get an invariant by name."""
        return self._invariants.get(name)

    def list_all(self) -> list[Invariant]:
        """List all registered invariants."""
        return list(self._invariants.values())

    def list_enabled(self) -> list[Invariant]:
        """List enabled invariants."""
        return [self._invariants[name] for name in self._enabled if name in self._invariants]

    async def check_all(
        self,
        context: InvariantContext,
        *,
        tags: tuple[str, ...] | None = None,
    ) -> list[InvariantResult]:
        """Run all enabled invariants matching tags."""
        invariants = self.list_enabled()
        if tags:
            invariants = [inv for inv in invariants if any(t in inv.tags for t in tags)]
        return await asyncio.gather(*[inv.check(context) for inv in invariants])

    async def check_one(self, name: str, context: InvariantContext) -> InvariantResult | None:
        """Run a single invariant by name."""
        invariant = self._invariants.get(name)
        if invariant is None or not self.is_enabled(name):
            return None
        return await invariant.check(context)

    def summary(self) -> dict[str, Any]:
        """Get registry summary."""
        return {
            "total": len(self._invariants),
            "enabled": len(self._enabled),
            "invariants": [
                {
                    "name": inv.name,
                    "enabled": inv.name in self._enabled,
                    "severity": inv.severity,
                    "description": inv.description,
                    "tags": inv.tags,
                }
                for inv in self._invariants.values()
            ],
        }


# Global registry
_REGISTRY: InvariantRegistry | None = None


def get_invariant_registry(settings: Settings | None = None) -> InvariantRegistry:
    """Get or create the global invariant registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = InvariantRegistry(settings or get_settings())
    return _REGISTRY


def init_invariant_registry(settings: Settings) -> InvariantRegistry:
    """Initialize the invariant registry."""
    global _REGISTRY
    _REGISTRY = InvariantRegistry(settings)
    return _REGISTRY


__all__ = [
    "Invariant",
    "InvariantResult",
    "InvariantContext",
    "CompositeInvariant",
    "InvariantRegistry",
    "ToolCallBudgetInvariant",
    "ProviderLatencyInvariant",
    "ConversationTurnLimitInvariant",
    "SessionEventSequenceInvariant",
    "SubagentDepthInvariant",
    "WorkspacePathInvariant",
    "MemoryUsageInvariant",
    "DiskSpaceInvariant",
    "get_invariant_registry",
    "init_invariant_registry",
]
