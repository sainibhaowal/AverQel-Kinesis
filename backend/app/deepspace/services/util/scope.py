"""Scope utilities for DeepSpace services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

T = TypeVar("T")
ScopeKey = Any


class ScopeLayer(Generic[T]):
    """A single layer in a scope chain."""

    def __init__(self, value: T) -> None:
        self.value = value


class ScopedLayers(Generic[T]):
    """
    Manages a stack of scoped layers, allowing global and scoped registrations.
    """

    def __init__(
        self,
        factory: Callable[[ScopeKey | None], T],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._factory = factory
        self._on_change = on_change
        self._global = factory(None)
        self._scoped: dict[ScopeKey, T] = {}

    @property
    def global_layer(self) -> T:
        return self._global

    def chain_layers(self, scope_key: ScopeKey | None) -> list[T]:
        layers = [self._global]
        if scope_key is not None and scope_key in self._scoped:
            layers.append(self._scoped[scope_key])
        return layers

    def merge(self, scope_key: ScopeKey | None, accessor: Callable[[T], Any]) -> Any:
        """Merge values from all layers in scope chain."""
        result = {}
        for layer in self.chain_layers(scope_key):
            value = accessor(layer)
            if hasattr(value, "items"):
                result.update(value)
        return result

    def effect(
        self,
        registrar: Any,
        effect_fn: Callable[[T], None],
        label: str = "effect",
    ) -> Callable[[], None]:
        """Register an effect in the appropriate layer."""
        scope_key = getattr(registrar, "scope_key", None)
        if scope_key is None:
            target = self._global
        else:
            if scope_key not in self._scoped:
                self._scoped[scope_key] = self._factory(scope_key)
            target = self._scoped[scope_key]

        effect_fn(target)

        def dispose() -> None:
            if scope_key is None:
                return
            if scope_key in self._scoped:
                del self._scoped[scope_key]

        return dispose


def scope_of(ctx: Any) -> ScopeKey:
    """Extract scope key from a context object."""
    return getattr(ctx, "scope_key", id(ctx))


__all__ = ["ScopedLayers", "ScopeLayer", "scope_of", "ScopeKey"]
