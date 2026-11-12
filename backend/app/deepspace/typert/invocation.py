"""One capability invocation and its ambient context.

The invocation carries the caller, the declared resources, the cancellation
token, the deadline, and the values the call owns.  Python passes it
explicitly where that is clearer, and exposes it through
:func:`current_invocation` for a deeply nested helper that would otherwise
need the parameter threaded through every layer.

The ambient value is a ``ContextVar``, so concurrent async calls in the same
process never see each other's invocation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.owned import OwnedValue
from app.deepspace.typert.principal import Principal
from app.deepspace.typert.resources import (
    ResourceAccess,
    dedupe_resources,
    resources_as_dict,
)


class CancellationToken:
    """A cancellation signal shared by a caller and one invocation."""

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        """Whether cancellation was requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """The caller's cancellation reason, when one was given."""
        return self._reason

    def cancel(self, reason: str | None = None) -> None:
        """Request cancellation.  The first reason wins."""
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    async def wait(self) -> None:
        """Wait until cancellation is requested."""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """Raise the shared cancelled failure when cancellation was requested."""
        if self._event.is_set():
            raise RemoteError(
                "gateway/cancelled",
                self._reason or "The capability call was cancelled.",
            )


@dataclass(slots=True)
class RemoteInvocation:
    """The context one capability call runs in.

    Attributes:
        call_id: A stable id for diagnostics and logs.
        principal: The caller identity.
        resources: Every resource the call declares.
        cancellation: The caller's cancellation token.
        deadline: A monotonic deadline, or ``None`` for no deadline.
        metadata: Small caller-supplied context that is not sent on the wire.
    """

    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    principal: Principal = field(default_factory=lambda: Principal(kind="owner", id="deepspace"))
    resources: tuple[ResourceAccess, ...] = ()
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    deadline: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _owned: list[OwnedValue[Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.resources = dedupe_resources(self.resources)
        if self.deadline is not None and self.deadline < 0:
            raise ValueError("A deadline is an absolute monotonic time.")

    @property
    def remaining_seconds(self) -> float | None:
        """Seconds left before the deadline, or ``None`` without one."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def check_active(self) -> None:
        """Raise when the call is cancelled or past its deadline."""
        self.cancellation.raise_if_cancelled()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RemoteError(
                "gateway/unavailable",
                "The capability call exceeded its deadline.",
            )

    def own(self, value: OwnedValue[Any]) -> Any:
        """Track an owned value and return its payload."""
        self._owned.append(value)
        return value.value

    def dispose(self) -> None:
        """Release every owned value, newest first.

        A failing release does not stop the remaining releases; the first
        failure is raised after all of them were attempted.
        """
        failure: BaseException | None = None
        while self._owned:
            owned = self._owned.pop()
            try:
                owned.dispose()
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable, JSON-compatible invocation description."""
        return {
            "call_id": self.call_id,
            "principal": {
                "kind": self.principal.kind,
                "id": self.principal.id,
                "tenant_id": (
                    str(self.principal.tenant_id) if self.principal.tenant_id is not None else None
                ),
            },
            "resources": resources_as_dict(self.resources),
            "cancelled": self.cancellation.cancelled,
            "remaining_seconds": self.remaining_seconds,
        }


_CURRENT_INVOCATION: ContextVar[RemoteInvocation | None] = ContextVar(
    "deepspace_typert_invocation",
    default=None,
)


def current_invocation() -> RemoteInvocation | None:
    """Return the invocation running in this context, if any."""
    return _CURRENT_INVOCATION.get()


def current_principal() -> Principal | None:
    """Return the caller of the current invocation, if any."""
    invocation = _CURRENT_INVOCATION.get()
    return invocation.principal if invocation is not None else None


@contextmanager
def invocation_scope(invocation: RemoteInvocation) -> Iterator[RemoteInvocation]:
    """Bind one invocation to the current context for its duration."""
    token: Token[RemoteInvocation | None] = _CURRENT_INVOCATION.set(invocation)
    try:
        yield invocation
    finally:
        _CURRENT_INVOCATION.reset(token)


__all__ = [
    "CancellationToken",
    "RemoteInvocation",
    "current_invocation",
    "current_principal",
    "invocation_scope",
]
