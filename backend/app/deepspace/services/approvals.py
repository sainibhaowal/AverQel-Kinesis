"""Approval capability seam for DeepSpace.

A tool call that needs human approval first asks every registered answerer.
An answerer that accepts the request decides it in-process; when no answerer
accepts, the turn falls back to the durable approval pause so the decision is
still the user's. Missing answerers fail closed: the service never grants
access on its own.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

ApprovalOutcome = Literal["allowed-once", "rejected", "cancelled", "unavailable"]

APPROVAL_OUTCOMES: frozenset[str] = frozenset(
    {"allowed-once", "rejected", "cancelled", "unavailable"}
)

MAX_AUDIT_ENTRIES = 200


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One action a human must decide before it runs."""

    tool_name: str
    arguments: dict[str, Any]
    conversation_id: str
    call_id: str
    risk_level: str
    permission_mode: str
    warning: str
    tenant_id: str = ""
    user_id: str = ""
    mcp_server: str | None = None
    mcp_tool: str | None = None
    approval_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class ApprovalAuditEntry:
    """One asked/decided pair in the service's bounded audit log."""

    approval_id: str
    tool_name: str
    conversation_id: str
    outcome: ApprovalOutcome
    decided_at: str


Answerer = Callable[[ApprovalRequest], Awaitable[ApprovalOutcome] | ApprovalOutcome]


class ApprovalService:
    """Process-wide answerer registry with a fail-closed default."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._answerers: list[Answerer] = []
        self._audit: deque[ApprovalAuditEntry] = deque(maxlen=MAX_AUDIT_ENTRIES)
        self._lock = asyncio.Lock()

    def register_answerer(self, answerer: Answerer) -> Callable[[], None]:
        """Register one answerer for later approval requests.

        Args:
            answerer: A callable returning an outcome. Registering the same
                callable twice keeps one entry.

        Returns:
            A disposer that removes this registration.
        """
        if answerer not in self._answerers:
            self._answerers.append(answerer)

        def dispose() -> None:
            try:
                self._answerers.remove(answerer)
            except ValueError:
                return

        return dispose

    def answerer_count(self) -> int:
        """Return the number of registrations, for diagnostics."""
        return len(self._answerers)

    def audit_log(self) -> list[ApprovalAuditEntry]:
        """Return the recent decisions, newest last."""
        return list(self._audit)

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Ask the registered answerers to decide one action.

        The first answerer that returns a known, non-``unavailable`` outcome
        owns the decision. An answerer that raises is skipped and its failure
        is logged; a malformed outcome is treated as ``unavailable``.

        Args:
            request: The action awaiting a decision.

        Returns:
            The decision, or ``unavailable`` when no answerer accepted it.
        """
        async with self._lock:
            answerers = tuple(self._answerers)
        for answerer in answerers:
            try:
                result = answerer(request)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001 - one answerer must not break the seam
                logger.warning("DeepSpace approval answerer failed", exc_info=True)
                continue
            outcome = str(result)
            if outcome not in APPROVAL_OUTCOMES or outcome == "unavailable":
                continue
            self._audit.append(
                ApprovalAuditEntry(
                    approval_id=request.approval_id,
                    tool_name=request.tool_name,
                    conversation_id=request.conversation_id,
                    outcome=outcome,  # type: ignore[arg-type]
                    decided_at=datetime.now(UTC).isoformat(),
                )
            )
            return outcome  # type: ignore[return-value]
        return "unavailable"


_APPROVAL_SERVICE: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    """Return the process-wide approval service."""
    global _APPROVAL_SERVICE
    if _APPROVAL_SERVICE is None:
        _APPROVAL_SERVICE = ApprovalService()
    return _APPROVAL_SERVICE


def init_approval_service() -> ApprovalService:
    """Replace the process-wide approval service, for application startup."""
    global _APPROVAL_SERVICE
    _APPROVAL_SERVICE = ApprovalService()
    return _APPROVAL_SERVICE


__all__ = [
    "APPROVAL_OUTCOMES",
    "Answerer",
    "ApprovalAuditEntry",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalService",
    "get_approval_service",
    "init_approval_service",
]
