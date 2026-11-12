"""Runtime reflection of a Python package's capability surface.

The Harness generator extracts a TypeScript package at build time.  A
Python service already carries the same facts at runtime: classes, methods,
signatures, annotations, dataclass fields, Pydantic models, and docstrings.
This module reads them into a stable, JSON-compatible package model.

Reflection never imports a module on its own.  The loader decides which
packages are trusted and imports them; reflection only reads what is
already loaded.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from app.deepspace.typert.decorators import _REMOTE_ATTRIBUTE, TypertRemoteService


class TypertEvent:
    """Marker base for one package's capability event."""

    __typert_event__ = True


@dataclass(frozen=True, slots=True)
class TypertDocTag:
    """One documentation tag or section."""

    name: str
    text: str

    def as_dict(self) -> dict[str, str]:
        """Return the JSON-compatible form."""
        return {"name": self.name, "text": self.text}


@dataclass(frozen=True, slots=True)
class TypertDocumentation:
    """The documentation of one package or member."""

    summary: str = ""
    description: str = ""
    tags: tuple[TypertDocTag, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "summary": self.summary,
            "description": self.description,
            "tags": [tag.as_dict() for tag in self.tags],
        }


@dataclass(frozen=True, slots=True)
class TypertMemberModel:
    """One member of a service, event, or object."""

    name: str
    kind: str
    signature: str = ""
    type_symbol: str = ""
    documentation: TypertDocumentation = field(default_factory=TypertDocumentation)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "type_symbol": self.type_symbol,
            "documentation": self.documentation.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class TypertServiceModel:
    """One reflected service class."""

    name: str
    documentation: TypertDocumentation = field(default_factory=TypertDocumentation)
    members: tuple[TypertMemberModel, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "name": self.name,
            "documentation": self.documentation.as_dict(),
            "members": [member.as_dict() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class TypertEventModel:
    """One reflected event class."""

    name: str
    documentation: TypertDocumentation = field(default_factory=TypertDocumentation)
    members: tuple[TypertMemberModel, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "name": self.name,
            "documentation": self.documentation.as_dict(),
            "members": [member.as_dict() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class TypertObjectModel:
    """One reflected object or model class."""

    name: str
    documentation: TypertDocumentation = field(default_factory=TypertDocumentation)
    members: tuple[TypertMemberModel, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "name": self.name,
            "documentation": self.documentation.as_dict(),
            "members": [member.as_dict() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class TypertPackageModel:
    """The reflected capability surface of one package and face."""

    package: str
    face: str = "host"
    services: tuple[TypertServiceModel, ...] = ()
    events: tuple[TypertEventModel, ...] = ()
    objects: tuple[TypertObjectModel, ...] = ()
    documentation: TypertDocumentation = field(default_factory=TypertDocumentation)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "package": self.package,
            "face": self.face,
            "services": [service.as_dict() for service in self.services],
            "events": [event.as_dict() for event in self.events],
            "objects": [obj.as_dict() for obj in self.objects],
            "documentation": self.documentation.as_dict(),
        }


def parse_documentation(value: object) -> TypertDocumentation:
    """Parse a docstring into a summary, description, and tags.

    Args:
        value: The object whose ``__doc__`` is read.

    Returns:
        The parsed documentation.
    """
    raw = inspect.getdoc(value) or ""
    if not raw:
        return TypertDocumentation()
    lines = raw.splitlines()
    summary = lines[0].strip() if lines else ""
    description_lines = [line.strip() for line in lines[1:]]
    tags: list[TypertDocTag] = []
    current: str | None = None
    current_lines: list[str] = []
    for line in description_lines:
        match = re.match(r"^:([A-Za-z]+)(?:\s+([^:]+))?:\s*(.*)$", line)
        section = re.match(r"^([A-Z][A-Za-z ]+):$", line)
        if match:
            if current is not None:
                tags.append(TypertDocTag(current, " ".join(current_lines).strip()))
            current = match.group(2).strip() if match.group(2) else match.group(1)
            current_lines = [match.group(3).strip()] if match.group(3).strip() else []
        elif section:
            if current is not None:
                tags.append(TypertDocTag(current, " ".join(current_lines).strip()))
            current = section.group(1).strip()
            current_lines = []
        elif current is not None:
            if line:
                current_lines.append(line)
    if current is not None:
        tags.append(TypertDocTag(current, " ".join(current_lines).strip()))
    description = "\n".join(line for line in description_lines if line and not line.startswith(":"))
    return TypertDocumentation(
        summary=summary,
        description=description,
        tags=tuple(tags),
    )


def build_package_model(
    module: ModuleType,
    *,
    face: str = "host",
    package: str | None = None,
) -> TypertPackageModel:
    """Reflect one loaded module into a package model.

    Args:
        module: The loaded package module.
        face: The consumer face, ``host`` in this port.
        package: The package name; defaults to ``module.__name__``.

    Returns:
        The package model.
    """
    services: list[TypertServiceModel] = []
    events: list[TypertEventModel] = []
    objects: list[TypertObjectModel] = []
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if not inspect.isclass(value) or getattr(value, "__module__", None) != module.__name__:
            continue
        if _is_service(value):
            services.append(
                TypertServiceModel(
                    name=name,
                    documentation=parse_documentation(value),
                    members=_service_members(value),
                )
            )
        elif _is_event(value):
            events.append(
                TypertEventModel(
                    name=name,
                    documentation=parse_documentation(value),
                    members=_data_members(value),
                )
            )
        elif _is_object(value):
            objects.append(
                TypertObjectModel(
                    name=name,
                    documentation=parse_documentation(value),
                    members=_data_members(value),
                )
            )
    return TypertPackageModel(
        package=package or module.__name__,
        face=face,
        services=tuple(services),
        events=tuple(events),
        objects=tuple(objects),
        documentation=parse_documentation(module),
    )


def _is_service(value: type[Any]) -> bool:
    if issubclass(value, TypertRemoteService):
        return True
    if getattr(value, "__deepspace_binding__", None) is not None:
        return True
    return any(
        getattr(member, _REMOTE_ATTRIBUTE, None) is not None
        for _name, member in inspect.getmembers_static(value)
    )


def _is_event(value: type[Any]) -> bool:
    return bool(getattr(value, "__typert_event__", False))


def _is_object(value: type[Any]) -> bool:
    return dataclasses.is_dataclass(value) or (
        isinstance(value, type) and issubclass(value, BaseModel)
    )


def _service_members(value: type[Any]) -> tuple[TypertMemberModel, ...]:
    members: list[TypertMemberModel] = []
    for name, member in inspect.getmembers_static(value):
        if name.startswith("_") or not callable(member):
            continue
        if isinstance(member, staticmethod | classmethod | property):
            continue
        try:
            signature = str(inspect.signature(member))
        except (TypeError, ValueError):
            signature = "()"
        annotation = inspect.signature(member).return_annotation
        members.append(
            TypertMemberModel(
                name=name,
                kind="method",
                signature=signature,
                type_symbol=_type_symbol(annotation),
                documentation=parse_documentation(member),
            )
        )
    return tuple(members)


def _data_members(value: type[Any]) -> tuple[TypertMemberModel, ...]:
    if isinstance(value, type) and issubclass(value, BaseModel):
        return tuple(
            TypertMemberModel(
                name=name,
                kind="field",
                type_symbol=_type_symbol(info.annotation),
                documentation=TypertDocumentation(
                    summary=str(info.description or ""),
                ),
            )
            for name, info in value.model_fields.items()
        )
    if dataclasses.is_dataclass(value):
        return tuple(
            TypertMemberModel(
                name=item.name,
                kind="field",
                type_symbol=_type_symbol(item.type),
            )
            for item in dataclasses.fields(value)
        )
    return ()


def _type_symbol(annotation: object) -> str:
    if annotation is inspect.Signature.empty or annotation is None:
        return ""
    if isinstance(annotation, type):
        return f"{annotation.__module__}.{annotation.__qualname__}"
    return str(annotation).replace("typing.", "")


def package_models_as_dict(models: Iterable[TypertPackageModel]) -> list[dict[str, Any]]:
    """Render several package models as JSON-compatible dictionaries."""
    return [model.as_dict() for model in models]


def documentation_from_mapping(value: Mapping[str, Any]) -> TypertDocumentation:
    """Build documentation from a manifest mapping."""
    tags = value.get("tags")
    return TypertDocumentation(
        summary=str(value.get("summary") or ""),
        description=str(value.get("description") or ""),
        tags=tuple(
            TypertDocTag(
                name=str(item.get("name") or ""),
                text=str(item.get("text") or ""),
            )
            for item in (tags if isinstance(tags, list) else [])
            if isinstance(item, Mapping)
        ),
    )


__all__ = [
    "TypertDocTag",
    "TypertDocumentation",
    "TypertEvent",
    "TypertEventModel",
    "TypertMemberModel",
    "TypertObjectModel",
    "TypertPackageModel",
    "TypertServiceModel",
    "build_package_model",
    "documentation_from_mapping",
    "package_models_as_dict",
    "parse_documentation",
]
