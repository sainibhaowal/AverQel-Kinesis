"""Generate the Python-native artifacts a capability consumer needs.

The Harness generator compiles TypeScript into Zod factories, ``.d.ts``
files, remote-client artifacts, and a tsdown build guard.  A Python service
already has runtime types, so this module renders the equivalent artifacts
from the live registry:

* one JSON Schema document per contributed schema;
* one package manifest that lists schemas, endpoints, providers, and
  reflection;
* one Python client module whose methods call the authenticated HTTP/WebSocket
  transport;
* a freshness guard that compares a previously generated manifest with the
  live registry and refuses stale artifacts.

Artifacts are rendered in memory.  Writing them is the caller's decision, so
importing this module cannot change the working tree.
"""

from __future__ import annotations

import json
import keyword
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from app.deepspace.typert.reflection import TypertPackageModel, build_package_model
from app.deepspace.typert.registry import TypertRegistry

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class ArtifactGuardError(ValueError):
    """A generated artifact is stale or names something the registry lost."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("The Typert artifact is stale: " + "; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    """One rendered file.

    Attributes:
        path: The relative path the artifact should be written to.
        content: The text content.
    """

    path: str
    content: str

    def as_dict(self) -> dict[str, str]:
        """Return the inspectable form."""
        return {"path": self.path, "content": self.content}


def render_type_model(module: ModuleType, *, face: str = "host") -> TypertPackageModel:
    """Reflect one module into the portable type model."""
    return build_package_model(module, face=face)


def render_schema_artifacts(
    registry: TypertRegistry,
    *,
    package: str | None = None,
    face: str = "host",
) -> dict[str, str]:
    """Render one JSON Schema file per live schema.

    Args:
        registry: The live registry.
        package: An optional package filter.
        face: The face filter.

    Returns:
        A mapping of relative path to JSON text.
    """
    artifacts: dict[str, str] = {}
    for record in registry.list(package=package, face=face):
        schema = registry.to_json_schema(record.key)
        filename = f"{_safe_filename(record.package)}/{_safe_filename(record.name)}.schema.json"
        artifacts[filename] = json.dumps(
            schema,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    return artifacts


def render_package_manifest(
    registry: TypertRegistry,
    package: str,
    *,
    face: str = "host",
) -> dict[str, Any]:
    """Render the stable manifest of one package face.

    Args:
        registry: The live registry.
        package: The package name.
        face: The face name.

    Returns:
        A JSON-compatible manifest.

    Raises:
        ArtifactGuardError: When the package face is not mounted.
    """
    record = registry.get_package(package, face)
    if record is None:
        raise ArtifactGuardError([f"package {package!r} face {face!r} is not mounted"])
    schemas = [
        {
            "key": item.key,
            "name": item.name,
            "schema": registry.to_json_schema(item.key),
        }
        for item in registry.list(package=package, face=face)
    ]
    endpoints = [descriptor.as_dict() for descriptor in _descriptors_for_package(registry, package)]
    return {
        "format": "deepspace-typert/1",
        "package": package,
        "face": face,
        "schemas": schemas,
        "endpoints": endpoints,
        "model": record.as_dict(),
        "lookups": registry.lookups.as_dict(),
        "contexts": registry.contexts.as_dict(),
        "resources": registry.resources.entries(),
    }


def render_client_source(
    registry: TypertRegistry,
    package: str,
    *,
    class_name: str | None = None,
) -> str:
    """Render a Python client for one package's endpoints.

    Args:
        registry: The live registry.
        package: The package whose endpoints the client calls.
        class_name: The client class name; defaults to a generated name.

    Returns:
        Python source text.
    """
    descriptors = _descriptors_for_package(registry, package)
    class_name = class_name or f"{_pascal_case(package)}Client"
    lines = [
        '"""Generated Typert client. Do not edit by hand."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, Mapping",
        "",
        "from app.deepspace.typert.remote import TypertRemoteClient",
        "",
        "",
        f"class {class_name}:",
        '    """Client methods generated from one mounted Typert package."""',
        "",
        "    def __init__(self, client: TypertRemoteClient) -> None:",
        "        self._client = client",
        "",
    ]
    for descriptor in descriptors:
        method_name = _client_method_name(descriptor.service, descriptor.method)
        lines.extend(
            [
                f"    async def {method_name}(",
                "        self,",
                "        arguments: Mapping[str, Any] | None = None,",
                "    ) -> Any:",
                f'        """{_docstring_text(descriptor.description or descriptor.id)}"""',
                (
                    f"        return await self._client.open_stream({descriptor.id!r}, arguments)"
                    if descriptor.mode == "stream"
                    else f"        return await self._client.invoke({descriptor.id!r}, arguments)"
                ),
                "",
            ]
        )
    if not descriptors:
        lines.extend(["    # This package contributes no invocable endpoints.", ""])
    return "\n".join(lines)


def render_artifact_tree(
    registry: TypertRegistry,
    package: str,
    *,
    face: str = "host",
    root: str = "typert",
) -> list[GeneratedArtifact]:
    """Render every artifact for one package.

    Args:
        registry: The live registry.
        package: The package name.
        face: The face name.
        root: The artifact root directory.

    Returns:
        The rendered artifacts in a stable order.
    """
    safe_package = _safe_filename(package)
    artifacts = [
        GeneratedArtifact(
            path=f"{root}/{safe_package}/manifest.json",
            content=json.dumps(
                render_package_manifest(registry, package, face=face),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        ),
        GeneratedArtifact(
            path=f"{root}/{safe_package}/client.py",
            content=render_client_source(registry, package),
        ),
    ]
    for relative, content in render_schema_artifacts(registry, package=package, face=face).items():
        artifacts.append(
            GeneratedArtifact(
                path=f"{root}/{safe_package}/schemas/{relative.split('/', 1)[1]}",
                content=content,
            )
        )
    return sorted(artifacts, key=lambda item: item.path)


def write_artifacts(artifacts: list[GeneratedArtifact], output_root: str | Path) -> list[Path]:
    """Atomically write rendered artifacts beneath ``output_root``.

    Paths are checked before writing and every replacement is atomic, so a
    failed generation cannot leave a partially written module. Existing
    unrelated files are preserved.
    """
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen: set[Path] = set()
    for artifact in artifacts:
        relative = Path(artifact.path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"Generated artifact path is unsafe: {artifact.path!r}")
        if relative in seen:
            raise ValueError(f"Generated artifact path is duplicated: {artifact.path!r}")
        seen.add(relative)
        parent = root
        for component in relative.parts[:-1]:
            parent = parent / component
            if parent.is_symlink() and not parent.resolve().is_relative_to(root):
                raise ValueError(
                    f"Generated artifact path escapes the output root: {artifact.path!r}"
                )
            parent.mkdir(exist_ok=True)
        parent = parent.resolve()
        if not parent.is_relative_to(root):
            raise ValueError(f"Generated artifact path escapes the output root: {artifact.path!r}")
        target = parent / relative.name
        if target.is_symlink() and not target.resolve().is_relative_to(root):
            raise ValueError(
                f"Generated artifact target escapes the output root: {artifact.path!r}"
            )

        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(artifact.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        written.append(target)
    return written


def check_artifacts(artifacts: list[GeneratedArtifact], output_root: str | Path) -> list[str]:
    """Return missing or stale generated paths without modifying the tree."""
    root = Path(output_root).resolve()
    problems: list[str] = []
    for artifact in artifacts:
        relative = Path(artifact.path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            problems.append(f"unsafe generated path: {artifact.path}")
            continue
        target = root / relative
        if target.is_symlink() and not target.resolve().is_relative_to(root):
            problems.append(f"generated path escapes output root: {artifact.path}")
        elif not target.is_file():
            problems.append(f"missing generated artifact: {artifact.path}")
        else:
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                problems.append(f"unreadable generated artifact: {artifact.path}")
            else:
                if current != artifact.content:
                    problems.append(f"stale generated artifact: {artifact.path}")
    return problems


def check_artifact_freshness(
    registry: TypertRegistry,
    manifest: dict[str, Any],
    *,
    face: str = "host",
) -> list[str]:
    """Compare a previously generated manifest with the live registry.

    Args:
        registry: The live registry.
        manifest: A manifest from :func:`render_package_manifest`.
        face: The face name.

    Returns:
        Every mismatch, in a stable order.  An empty list means the artifact
        matches the registry.
    """
    problems: list[str] = []
    package = manifest.get("package")
    if not isinstance(package, str):
        return ["the manifest has no package name"]
    record = registry.get_package(package, face)
    if record is None:
        return [f"package {package!r} face {face!r} is no longer mounted"]
    if manifest.get("format") != "deepspace-typert/1":
        problems.append("the manifest format is not 'deepspace-typert/1'")
    expected_schemas = {
        item["key"]: item.get("schema")
        for item in manifest.get("schemas", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    live_schemas = {
        item.key: registry.to_json_schema(item.key)
        for item in registry.list(package=package, face=face)
    }
    for missing in sorted(set(expected_schemas) - set(live_schemas)):
        problems.append(f"schema {missing!r} is no longer contributed")
    for added in sorted(set(live_schemas) - set(expected_schemas)):
        problems.append(f"schema {added!r} is not in the artifact")
    for key in sorted(set(expected_schemas) & set(live_schemas)):
        if expected_schemas[key] != live_schemas[key]:
            problems.append(f"schema {key!r} changed")
    expected_endpoints = {
        item["id"]
        for item in manifest.get("endpoints", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    live_endpoints = {descriptor.id for descriptor in _descriptors_for_package(registry, package)}
    for missing in sorted(expected_endpoints - live_endpoints):
        problems.append(f"endpoint {missing!r} is no longer contributed")
    for added in sorted(live_endpoints - expected_endpoints):
        problems.append(f"endpoint {added!r} is not in the artifact")
    return problems


def assert_artifact_fresh(
    registry: TypertRegistry,
    manifest: dict[str, Any],
    *,
    face: str = "host",
) -> None:
    """Raise when a generated artifact no longer matches the registry.

    Raises:
        ArtifactGuardError: With every mismatch.
    """
    problems = check_artifact_freshness(registry, manifest, face=face)
    if problems:
        raise ArtifactGuardError(problems)


def _descriptors_for_package(registry: TypertRegistry, package: str) -> list[Any]:
    return registry.descriptors_for_package(package)


def _safe_filename(value: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value).strip("._")
    return cleaned or "artifact"


def _pascal_case(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Typert"


def _client_method_name(service: str, method: str) -> str:
    name = method if method.isidentifier() else _safe_filename(method).replace("-", "_")
    if keyword.iskeyword(name):
        name = f"{name}_"
    return name


def _docstring_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"').replace("\n", " ")


__all__ = [
    "ArtifactGuardError",
    "GeneratedArtifact",
    "assert_artifact_fresh",
    "check_artifacts",
    "check_artifact_freshness",
    "render_artifact_tree",
    "render_client_source",
    "render_package_manifest",
    "render_schema_artifacts",
    "render_type_model",
    "write_artifacts",
]
