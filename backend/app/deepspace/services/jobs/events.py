"""Event hub and scoped layers for DeepSpace Jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.deepspace.services.jobs.types import JobEvent, JobEventFilter, JobEventListener
from app.deepspace.services.util.scope import ScopedLayers, scope_of


@dataclass(frozen=True, slots=True)
class JobLayer:
    """Scoped layer for job controllers and subscriptions."""

    controllers: list[Any] = field(default_factory=list)
    subscriptions: list[tuple[JobEventFilter, JobEventListener]] = field(default_factory=list)


class JobEventHub:
    """
    Scoped event hub for job lifecycle events.

    Subscriptions are effects of their registering context.
    `{ owners: 'scope' }` delivers events for owners in the subscriber's scope chain.
    """

    def __init__(
        self,
        layers: ScopedLayers[JobLayer],
        warn: Callable[[str], None],
    ) -> None:
        self._layers = layers
        self._warn = warn

    def subscribe(
        self,
        registrar: Any,
        filter: JobEventFilter,
        listener: JobEventListener,
    ) -> Callable[[], None]:
        """Register a scoped subscription. Returns disposer."""
        return self._layers.effect(
            registrar,
            lambda layer: layer.subscriptions.append((filter, listener)),
            {"label": "jobs.events.subscribe()"},
        )

    def emit(self, event: JobEvent, owner: Any | None) -> None:
        """Emit event to all matching scoped subscriptions."""
        owner_key = scope_of(owner.ctx) if owner is not None else None

        for layer in self._layers.chain_layers(owner_key):
            for filter, listener in layer.subscriptions:
                if self._matches(filter, event, owner, owner_key):
                    try:
                        listener(event)
                    except Exception as e:
                        self._warn(f"jobs: event listener failed: {e}")

    def _matches(
        self, filter: JobEventFilter, event: JobEvent, owner: Any | None, owner_key: Any | None
    ) -> bool:
        event_owner = getattr(event.job, "owner", None)

        if "owner" in filter:
            return event_owner == filter["owner"]
        elif "owners" in filter:
            if filter["owners"] == "all":
                return True
            elif filter["owners"] == "scope":
                return owner_key is not None and event_owner is not None
        return False


__all__ = ["JobLayer", "JobEventHub"]
