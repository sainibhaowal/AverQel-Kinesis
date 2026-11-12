"""Resource declarations for capability access control.

A capability call declares every resource it touches, so authorization can
check the complete set before the implementation runs.  A resource is either
the identity-less ``global`` resource or one identity of a declared kind,
such as a workspace, a Library file, a terminal session, or an SSH host.

The kind registry owns the identity extraction rule for one kind.  A kind
is declared once; a second declaration with a different rule is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.deepspace.typert.errors import RemoteError


@dataclass(frozen=True, slots=True)
class GlobalResource:
    """The identity-less resource every caller may name."""

    kind: str = field(default="global", init=False)

    def as_dict(self) -> dict[str, str]:
        """Return the wire form."""
        return {"kind": "global"}


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """One identity of one declared resource kind.

    Attributes:
        kind: The declared kind, such as ``workspace``.
        id: The wire identity.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not self.kind or self.kind == "global":
            raise ValueError("A resource reference needs a declared kind.")
        if not isinstance(self.id, str):
            object.__setattr__(self, "id", str(self.id))
        if not self.id:
            raise ValueError("A resource reference needs a non-empty id.")

    def as_dict(self) -> dict[str, str]:
        """Return the wire form."""
        return {"kind": self.kind, "id": self.id}


ResourceAccess = GlobalResource | ResourceRef
ResourceAuthorizer = Callable[[Any, Any, str], bool]

GLOBAL = GlobalResource()


def resource(kind: str, identity: object) -> ResourceRef:
    """Build one resource reference, normalizing its identity to text."""
    return ResourceRef(kind=kind, id=str(identity))


@dataclass(frozen=True, slots=True)
class ResourceKind:
    """One declared resource kind and its identity rule.

    Attributes:
        kind: The stable kind name.
        id_type: The Python identity type, for documentation and checks.
        extract: An optional callable that reads the identity from a host
            object.
        authorize: A fail-closed ownership check. It receives the request's
            database session, authenticated context, and resource id.
        description: What the resource represents.
    """

    kind: str
    id_type: type[Any] = str
    extract: Callable[[Any], object] | None = None
    authorize: ResourceAuthorizer | None = None
    description: str = ""


class ResourceKindRegistry:
    """The resource kinds a deployment declares."""

    def __init__(self) -> None:
        self._kinds: dict[str, ResourceKind] = {}

    def register(
        self,
        kind: str,
        *,
        id_type: type[Any] = str,
        extract: Callable[[Any], object] | None = None,
        authorize: ResourceAuthorizer | None = None,
        description: str = "",
    ) -> None:
        """Declare one resource kind.

        Args:
            kind: The stable kind name.  ``global`` is reserved.
            id_type: The Python identity type.
            extract: An optional reader for a host object's identity.
            description: What the resource represents.

        Raises:
            ValueError: If the kind is empty, reserved, or already declared.
        """
        if not kind or kind == "global":
            raise ValueError("A resource kind needs a name other than 'global'.")
        if kind in self._kinds:
            raise ValueError(f"Resource kind {kind!r} is already declared.")
        self._kinds[kind] = ResourceKind(
            kind=kind,
            id_type=id_type,
            extract=extract,
            authorize=authorize,
            description=description,
        )

    def get(self, kind: str) -> ResourceKind | None:
        """Return one declared kind, if it exists."""
        return self._kinds.get(kind)

    def require(self, kind: str) -> ResourceKind:
        """Return one declared kind or raise a typed failure."""
        found = self._kinds.get(kind)
        if found is None:
            raise RemoteError(
                "gateway/not-found",
                f"Resource kind {kind!r} is not declared.",
            )
        return found

    def list(self) -> list[ResourceKind]:
        """Return every declared kind in declaration order."""
        return list(self._kinds.values())

    def extract(self, kind: str, host: Any) -> ResourceRef:
        """Read one resource identity from a host object.

        Args:
            kind: The declared kind.
            host: The host object.

        Returns:
            The resource reference.

        Raises:
            RemoteError: When the kind is unknown or has no extractor.
        """
        declared = self.require(kind)
        if declared.extract is None:
            raise RemoteError(
                "gateway/internal",
                f"Resource kind {kind!r} has no identity extractor.",
            )
        return resource(kind, declared.extract(host))

    def validate(self, access: ResourceAccess) -> ResourceAccess:
        """Validate one declared resource reference."""
        if isinstance(access, GlobalResource):
            return access
        if not isinstance(access, ResourceRef):
            raise RemoteError(
                "gateway/bad-request",
                "A resource declaration must be global or a kind/id pair.",
            )
        self.require(access.kind)
        return access

    def validate_all(self, accesses: Iterable[ResourceAccess]) -> tuple[ResourceAccess, ...]:
        """Validate every resource in one declaration, preserving order."""
        return tuple(self.validate(access) for access in accesses)

    def as_dict(self) -> list[dict[str, Any]]:
        """Return the inspectable description of every kind."""
        return [
            {
                "kind": item.kind,
                "id_type": getattr(item.id_type, "__name__", str(item.id_type)),
                "description": item.description,
                "has_extractor": item.extract is not None,
                "has_authorizer": item.authorize is not None,
            }
            for item in self._kinds.values()
        ]


def dedupe_resources(accesses: Iterable[ResourceAccess]) -> tuple[ResourceAccess, ...]:
    """Remove duplicate resource references while preserving order."""
    seen: set[tuple[str, str]] = set()
    result: list[ResourceAccess] = []
    for access in accesses:
        key = ("global", "") if isinstance(access, GlobalResource) else (access.kind, access.id)
        if key in seen:
            continue
        seen.add(key)
        result.append(access)
    return tuple(result)


def resources_as_dict(accesses: Iterable[ResourceAccess]) -> list[dict[str, str]]:
    """Render a resource declaration as JSON-compatible dictionaries."""
    return [access.as_dict() for access in accesses]


def resource_from_mapping(value: Mapping[str, Any]) -> ResourceAccess:
    """Build a resource reference from its wire form.

    Raises:
        RemoteError: When the mapping is malformed.
    """
    kind = value.get("kind")
    if kind == "global":
        return GLOBAL
    if not isinstance(kind, str) or not kind:
        raise RemoteError("gateway/bad-request", "A resource needs a kind.")
    identity = value.get("id")
    if identity is None or str(identity) == "":
        raise RemoteError("gateway/bad-request", "A resource needs an id.")
    return resource(kind, identity)


def uuid_resource(kind: str, identity: uuid.UUID) -> ResourceRef:
    """Build a resource reference from a UUID identity."""
    return resource(kind, identity)


__all__ = [
    "GLOBAL",
    "GlobalResource",
    "ResourceAccess",
    "ResourceAuthorizer",
    "ResourceKind",
    "ResourceKindRegistry",
    "ResourceRef",
    "dedupe_resources",
    "resource",
    "resource_from_mapping",
    "resources_as_dict",
    "uuid_resource",
]
