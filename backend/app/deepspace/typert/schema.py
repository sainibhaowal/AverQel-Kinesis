"""Schema factories, their registry, and JSON Schema projection.

A schema is registered under ``<package>#<name>`` and materialized lazily.
The factory returns a Pydantic model class, a model instance, or an
already-built JSON Schema dictionary.

The projection is deliberately narrower than Pydantic's full output: a tool
schema may use objects, arrays, primitives, enums, ``$ref``/``$defs``, and
``anyOf`` for nullable fields.  Constructs that no tool consumer can render
faithfully (``oneOf``, ``allOf``, conditionals) fail loud instead of being
silently weakened.
"""

from __future__ import annotations

import builtins
import copy
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.deepspace.typert.errors import RemoteError, register_error_code

register_error_code(
    "schema/unsupported",
    "The schema uses a construct the tool-schema projection cannot preserve.",
)

_ALLOWED_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "$defs",
        "$ref",
        "$schema",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)


@dataclass(slots=True)
class TypertSchemaFactory:
    """One lazily materialized schema.

    Attributes:
        key: The registry key, ``<package>#<name>``.
        package: The owning package.
        name: The schema name inside the package.
        factory: A zero-argument callable returning the model or schema.
        type_symbol: The portable type name.
        face: The compiler/consumer face, ``host`` in this port.
    """

    key: str
    package: str
    name: str
    factory: Callable[[], Any]
    type_symbol: str
    face: str = "host"
    _materialized: Any = field(default=None, init=False, repr=False)
    _locked: bool = field(default=False, init=False, repr=False)

    def create(self) -> Any:
        """Materialize and cache the schema value."""
        if self._materialized is None:
            self._materialized = self.factory()
        return self._materialized

    def json_schema(self) -> dict[str, Any]:
        """Return the projected JSON Schema for this factory."""
        return json_schema_for(self.create())

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable description without materializing."""
        return {
            "key": self.key,
            "package": self.package,
            "name": self.name,
            "type_symbol": self.type_symbol,
            "face": self.face,
            "materialized": self._materialized is not None,
        }


class SchemaRegistry:
    """The process's schema catalogue, keyed by ``package#name``."""

    def __init__(self) -> None:
        self._entries: dict[str, TypertSchemaFactory] = {}
        self._lock = threading.RLock()

    def register(
        self,
        package: str,
        name: str,
        factory: Callable[[], Any],
        *,
        type_symbol: str | None = None,
        face: str = "host",
    ) -> TypertSchemaFactory:
        """Register one schema factory.

        Args:
            package: The owning package.
            name: The schema name.
            factory: A zero-argument materializer.
            type_symbol: An optional portable type name.
            face: The consumer face.

        Returns:
            The registered factory.

        Raises:
            ValueError: When the package or name is empty or already taken.
        """
        if not package or not name:
            raise ValueError("A schema needs a package and a name.")
        key = f"{package}#{name}"
        with self._lock:
            if key in self._entries:
                raise ValueError(f"Schema {key!r} is already registered.")
            entry = TypertSchemaFactory(
                key=key,
                package=package,
                name=name,
                factory=factory,
                type_symbol=type_symbol or name,
                face=face,
            )
            self._entries[key] = entry
            return entry

    def get(self, key: str) -> TypertSchemaFactory | None:
        """Return one factory by key, if it exists."""
        return self._entries.get(key)

    def resolve(self, key: str) -> TypertSchemaFactory:
        """Return one factory or raise a typed not-found failure."""
        found = self._entries.get(key)
        if found is None:
            raise RemoteError("gateway/not-found", f"Schema {key!r} is not registered.")
        return found

    def list(
        self,
        *,
        package: str | None = None,
        face: str | None = None,
    ) -> list[TypertSchemaFactory]:
        """Return factories in registration order, optionally filtered."""
        return [
            entry
            for entry in self._entries.values()
            if (package is None or entry.package == package)
            and (face is None or entry.face == face)
        ]

    def keys(self) -> builtins.list[str]:
        """Return every registered key in registration order."""
        return builtins.list(self._entries)

    def remove(self, key: str) -> TypertSchemaFactory | None:
        """Remove one factory, returning it when it existed."""
        with self._lock:
            return self._entries.pop(key, None)

    def snapshot(self) -> dict[str, TypertSchemaFactory]:
        """Return a shallow copy for an atomic contribution rollback."""
        return dict(self._entries)

    def restore(self, snapshot: dict[str, TypertSchemaFactory]) -> None:
        """Restore a snapshot after a failed contribution."""
        with self._lock:
            self._entries = dict(snapshot)


def json_schema_for(value: Any) -> dict[str, Any]:
    """Build the projected JSON Schema for a model, instance, or schema.

    Args:
        value: A Pydantic model class, a model instance, or a schema dict.

    Returns:
        The projected schema.

    Raises:
        RemoteError: With ``schema/unsupported`` when the value cannot be
            represented faithfully.
    """
    if isinstance(value, dict):
        schema = copy.deepcopy(value)
    elif isinstance(value, BaseModel):
        schema = value.__class__.model_json_schema()
    elif isinstance(value, type) and issubclass(value, BaseModel):
        schema = value.model_json_schema()
    else:
        raise RemoteError(
            "schema/unsupported",
            f"{type(value).__name__} is not a Pydantic model or JSON Schema.",
        )
    return project_tool_schema(schema)


def project_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a JSON Schema onto the tool-schema subset.

    Args:
        schema: The source schema.

    Returns:
        A detached projected schema.

    Raises:
        RemoteError: With ``schema/unsupported`` for a construct that
            cannot be preserved.
    """
    if not isinstance(schema, dict):
        raise RemoteError("schema/unsupported", "A JSON Schema must be an object.")
    projected: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            raise RemoteError(
                "schema/unsupported",
                f"The JSON Schema keyword {key!r} is not supported by the tool projection.",
            )
        if key in {"properties", "$defs"}:
            if not isinstance(value, dict):
                raise RemoteError("schema/unsupported", f"{key!r} must be an object.")
            projected[key] = {
                str(name): project_tool_schema(child) for name, child in value.items()
            }
        elif key in {"items", "additionalProperties", "not"}:
            if isinstance(value, bool):
                projected[key] = value
            elif isinstance(value, dict):
                projected[key] = project_tool_schema(value)
            else:
                raise RemoteError("schema/unsupported", f"{key!r} has an invalid value.")
        elif key == "anyOf":
            if not isinstance(value, list):
                raise RemoteError("schema/unsupported", "'anyOf' must be an array.")
            projected[key] = [project_tool_schema(child) for child in value]
        elif key == "enum":
            if not isinstance(value, list) or not value:
                raise RemoteError("schema/unsupported", "'enum' must be a non-empty array.")
            projected[key] = copy.deepcopy(value)
        elif key == "required":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise RemoteError("schema/unsupported", "'required' must be string names.")
            projected[key] = list(value)
        elif key in {"default", "examples", "const"}:
            projected[key] = copy.deepcopy(value)
        else:
            projected[key] = value
    return projected


def aggregate_json_schema(factories: Iterable[TypertSchemaFactory]) -> dict[str, Any]:
    """Project several schema factories into one ``$defs`` document."""
    definitions: dict[str, Any] = {}
    for factory in factories:
        definitions[factory.key] = factory.json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
    }


__all__ = [
    "SchemaRegistry",
    "TypertSchemaFactory",
    "aggregate_json_schema",
    "json_schema_for",
    "project_tool_schema",
]
