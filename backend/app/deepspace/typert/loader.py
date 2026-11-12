"""Discover, validate, and mount Typert package contributions.

The Harness resolves ``package.json#exports["./typert"]`` and mounts the
generated artifact for the lifetime of a plugin fiber.  A Python deployment
has no compiled ``./typert`` artifact, so this loader uses the equivalent
Python entry points:

* an installed distribution declares the ``deepspace.typert`` entry-point
  group and returns a contribution (or a module that exports ``TYPERT``);
* an explicitly configured dotted module exports ``TYPERT``;
* a directory plugin carries a ``typert.json`` manifest.

Discovery is quiet about packages that simply do not declare Typert.
Explicitly configured packages are fail-loud: a configured package that
cannot be imported, has no manifest, or contributes an invalid artifact stops
the load so a deployment cannot silently lose part of its surface.

A mounted package is withdrawn exactly when it is unmounted.  The loader
keeps the disposer the registry returned, so unmounting a package cannot
remove another package's contribution.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from app.deepspace.typert.codecs import JsonCodec
from app.deepspace.typert.descriptors import (
    RemoteContribution,
    RemoteDescriptor,
    RemoteParameter,
)
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.registry import Disposer, TypertRegistry
from app.deepspace.typert.resources import GLOBAL, resource_from_mapping

logger = logging.getLogger(__name__)

#: The entry-point group an installed package uses to declare its host face.
TYPERT_ENTRY_POINT_GROUP = "deepspace.typert"

#: The manifest file a directory plugin carries.
TYPERT_MANIFEST_FILE = "typert.json"

#: The module attribute that exports a contribution or its factory.
TYPERT_ATTRIBUTE = "TYPERT"

#: The optional module attribute that names the contribution's package.
TYPERT_PACKAGE_ATTRIBUTE = "TYPERT_PACKAGE"


class TypertLoaderError(RuntimeError):
    """A configured package could not be loaded or validated."""

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


@dataclass(slots=True)
class LoadedPackage:
    """One mounted contribution.

    Attributes:
        name: The contribution package name.
        contribution: The mounted contribution.
        disposer: The withdrawal the registry returned.
        source: Where the package came from, for diagnostics.
        module: The imported module, when the package was a module.
    """

    name: str
    contribution: RemoteContribution
    disposer: Disposer
    source: str
    module: ModuleType | None = None


@dataclass(slots=True)
class TypertLoader:
    """The process's package loader.

    Attributes:
        registry: The registry contributions are mounted into.
        discovery_errors: Problems seen while quietly discovering packages.
    """

    registry: TypertRegistry
    discovery_errors: list[str] = field(default_factory=list)
    _mounted: dict[str, list[LoadedPackage]] = field(default_factory=dict, repr=False)
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import threading

        self._lock = threading.RLock()

    # -- mounting -------------------------------------------------------------

    def load_contribution(
        self,
        contribution: RemoteContribution,
        *,
        source: str = "manual",
        required: bool = True,
        module: ModuleType | None = None,
    ) -> LoadedPackage | None:
        """Validate and mount one contribution.

        Args:
            contribution: The contribution to mount.
            source: Where it came from, for diagnostics.
            required: Whether a failure is raised instead of recorded.
            module: The module that exported the contribution.

        Returns:
            The mounted package, or ``None`` when an optional package failed.
        """
        name = contribution.package
        try:
            contribution.validate()
            if contribution.face != "host":
                raise TypertLoaderError(
                    f"{name}: a host loader cannot mount face {contribution.face!r}.",
                )
            with self._lock:
                if name in self._mounted:
                    raise TypertLoaderError(
                        f"{name}: the package is already mounted.",
                    )
            disposer = self.registry.register(contribution)
            loaded = LoadedPackage(
                name=name,
                contribution=contribution,
                disposer=disposer,
                source=source,
                module=module,
            )
            with self._lock:
                self._mounted.setdefault(name, []).append(loaded)
            return loaded
        except Exception as exc:
            message = f"{source}: {exc}"
            if required:
                raise TypertLoaderError(
                    f"Typert package {name!r} could not be mounted.",
                    errors=[message],
                ) from exc
            self.discovery_errors.append(message)
            logger.warning("Skipping optional Typert package: %s", message)
            return None

    def load_module(
        self,
        module_path: str,
        *,
        required: bool = True,
        package: str | None = None,
    ) -> LoadedPackage | None:
        """Import a dotted module and mount its ``TYPERT`` contribution.

        Args:
            module_path: The dotted module path.
            required: Whether a failure is raised instead of recorded.
            package: The expected contribution package; defaults to the
                module's ``TYPERT_PACKAGE`` or the contribution's own name.

        Returns:
            The mounted package, or ``None`` when an optional package failed.
        """
        try:
            module = importlib.import_module(module_path)
            exported = getattr(module, TYPERT_ATTRIBUTE, None)
            if exported is None:
                raise TypertLoaderError(
                    f"{module_path} does not export {TYPERT_ATTRIBUTE!r}.",
                )
            expected = package or getattr(module, TYPERT_PACKAGE_ATTRIBUTE, None)
            contribution = validate_typert_manifest(
                expected,
                exported,
                subject=module_path,
            )
            return self.load_contribution(
                contribution,
                source=f"module:{module_path}",
                required=required,
                module=module,
            )
        except Exception as exc:
            message = f"module:{module_path}: {exc}"
            if required:
                raise TypertLoaderError(
                    f"Typert module {module_path!r} could not be loaded.",
                    errors=[message],
                ) from exc
            self.discovery_errors.append(message)
            logger.warning("Skipping optional Typert module %s: %s", module_path, exc)
            return None

    def load_manifest_mapping(
        self,
        manifest: Mapping[str, Any],
        *,
        source: str = "manifest",
        required: bool = True,
    ) -> LoadedPackage | None:
        """Validate a JSON manifest and mount the contribution it describes."""
        try:
            contribution = validate_typert_manifest(
                str(manifest.get("package") or "") or None,
                manifest,
                subject=source,
            )
        except Exception as exc:
            if required:
                raise TypertLoaderError(
                    f"Typert manifest {source!r} is invalid.",
                    errors=[str(exc)],
                ) from exc
            self.discovery_errors.append(f"{source}: {exc}")
            logger.warning("Skipping optional Typert manifest %s: %s", source, exc)
            return None
        return self.load_contribution(
            contribution,
            source=source,
            required=required,
        )

    def load_configured(self, packages: Iterable[str]) -> list[LoadedPackage]:
        """Mount every explicitly configured package, fail-loud.

        Args:
            packages: Dotted module paths.

        Returns:
            The mounted packages.

        Raises:
            TypertLoaderError: When one configured package fails; the error
                aggregates every failure so an operator can fix them in one
                pass.
        """
        loaded: list[LoadedPackage] = []
        errors: list[str] = []
        for module_path in packages:
            try:
                package = self.load_module(str(module_path), required=True)
            except TypertLoaderError as exc:
                errors.extend(exc.errors or [str(exc)])
                continue
            if package is not None:
                loaded.append(package)
        if errors:
            raise TypertLoaderError(
                "One or more configured Typert packages failed to load.",
                errors=errors,
            )
        return loaded

    def discover(self) -> list[LoadedPackage]:
        """Mount every installed entry-point contribution.

        A discovered package that does not declare a usable contribution is
        recorded in :attr:`discovery_errors` and skipped; one broken optional
        package must not prevent the process from starting.

        Returns:
            The newly mounted packages.
        """
        loaded: list[LoadedPackage] = []
        for entry_point in _typert_entry_points():
            try:
                exported = entry_point.load()
                contribution = validate_typert_manifest(
                    getattr(entry_point, "name", None),
                    exported,
                    subject=f"entry-point:{entry_point.name}",
                )
                package = self.load_contribution(
                    contribution,
                    source=f"entry-point:{entry_point.name}",
                    required=False,
                )
                if package is not None:
                    loaded.append(package)
            except Exception as exc:  # noqa: BLE001 - one optional package is contained
                message = f"entry-point:{getattr(entry_point, 'name', '?')}: {exc}"
                self.discovery_errors.append(message)
                logger.warning("Skipping optional Typert entry point: %s", message)
        return loaded

    def discover_directory(self, root: str | Path) -> list[LoadedPackage]:
        """Mount every ``typert.json`` manifest under one directory.

        A missing root is not an error: a deployment without a Typert plugin
        directory simply has no extra packages.

        Args:
            root: The directory to scan.

        Returns:
            The newly mounted packages.
        """
        base = Path(root)
        if not base.is_dir():
            return []
        loaded: list[LoadedPackage] = []
        for manifest_path in sorted(base.glob(f"*/{TYPERT_MANIFEST_FILE}")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                message = f"{manifest_path}: {exc}"
                self.discovery_errors.append(message)
                logger.warning("Skipping unreadable Typert manifest: %s", message)
                continue
            if not isinstance(manifest, Mapping):
                message = f"{manifest_path}: the manifest must be an object."
                self.discovery_errors.append(message)
                logger.warning("Skipping invalid Typert manifest: %s", message)
                continue
            package = self.load_manifest_mapping(
                manifest,
                source=str(manifest_path),
                required=False,
            )
            if package is not None:
                loaded.append(package)
        return loaded

    # -- unmounting -----------------------------------------------------------

    def unmount(self, name: str) -> bool:
        """Withdraw one mounted package.

        Args:
            name: The contribution package name.

        Returns:
            Whether a mounted package was withdrawn.
        """
        with self._lock:
            mounted = self._mounted.get(name)
            if not mounted:
                return False
            loaded = mounted.pop()
            if not mounted:
                del self._mounted[name]
        loaded.disposer.dispose()
        return True

    def unmount_all(self) -> int:
        """Withdraw every mounted package, newest first."""
        with self._lock:
            names = list(self._mounted)
        count = 0
        for name in reversed(names):
            while self.unmount(name):
                count += 1
        return count

    def mounted(self) -> list[LoadedPackage]:
        """Return every mounted package in mount order."""
        with self._lock:
            return [item for items in self._mounted.values() for item in items]

    def is_mounted(self, name: str) -> bool:
        """Whether one package is currently mounted."""
        with self._lock:
            return name in self._mounted

    def status(self) -> dict[str, Any]:
        """Return the inspectable loader state."""
        with self._lock:
            mounted = [
                {
                    "name": item.name,
                    "source": item.source,
                    "face": item.contribution.face,
                    "schemas": len(item.contribution.schemas),
                    "descriptors": len(item.contribution.descriptors),
                    "active": item.disposer.active,
                }
                for item in self.mounted()
            ]
        return {
            "entry_point_group": TYPERT_ENTRY_POINT_GROUP,
            "manifest_file": TYPERT_MANIFEST_FILE,
            "mounted": mounted,
            "discovery_errors": list(self.discovery_errors),
        }


def _typert_entry_points() -> list[Any]:
    try:
        return list(importlib.metadata.entry_points(group=TYPERT_ENTRY_POINT_GROUP))
    except TypeError:  # pragma: no cover - Python < 3.10 compatibility
        return list(
            importlib.metadata.entry_points().get(TYPERT_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
        )


def validate_typert_manifest(
    expected_package: str | None,
    exported: Any,
    *,
    subject: str = "manifest",
) -> RemoteContribution:
    """Validate one exported contribution or JSON manifest.

    Args:
        expected_package: The package the artifact must belong to, when the
            caller knows it.
        exported: A contribution, a factory, a module, or a manifest mapping.
        subject: The source name used in a failure message.

    Returns:
        The validated contribution.

    Raises:
        TypertLoaderError: When the artifact is missing, owned by another
            package, not a host face, or structurally invalid.
    """
    if isinstance(exported, ModuleType):
        exported = getattr(exported, TYPERT_ATTRIBUTE, None)
    if callable(exported) and not isinstance(exported, RemoteContribution):
        exported = exported()
    if isinstance(exported, Mapping):
        contribution = _contribution_from_mapping(exported, subject=subject)
    elif isinstance(exported, RemoteContribution):
        contribution = exported
    else:
        raise TypertLoaderError(
            f"{subject} does not export a Typert contribution.",
        )
    try:
        contribution.validate()
    except RemoteError as exc:
        raise TypertLoaderError(f"{subject}: {exc.message}") from exc
    if expected_package and contribution.package != expected_package:
        raise TypertLoaderError(
            f"{subject} contributes package {contribution.package!r}, "
            f"but the artifact is owned by {expected_package!r}.",
        )
    if contribution.face != "host":
        raise TypertLoaderError(
            f"{subject} declares face {contribution.face!r}; a host artifact must declare 'host'.",
        )
    return contribution


def _contribution_from_mapping(
    manifest: Mapping[str, Any],
    *,
    subject: str,
) -> RemoteContribution:
    package = manifest.get("package")
    if not isinstance(package, str) or not package:
        raise TypertLoaderError(f"{subject}: a manifest needs a package name.")
    face = str(manifest.get("face") or "host")
    raw_schemas = manifest.get("schemas", [])
    if not isinstance(raw_schemas, list):
        raise TypertLoaderError(f"{subject}: schemas must be an array.")
    schemas: list[Any] = []
    for index, raw in enumerate(raw_schemas):
        if not isinstance(raw, Mapping):
            raise TypertLoaderError(f"{subject}: schema {index} must be an object.")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise TypertLoaderError(f"{subject}: schema {index} needs a name.")
        if "schema" in raw:
            schema_value = raw["schema"]
            if not isinstance(schema_value, Mapping):
                raise TypertLoaderError(
                    f"{subject}: schema {name!r} must carry an object 'schema'.",
                )

            def factory(_value: Mapping[str, Any] = schema_value) -> Any:
                return dict(_value)

        elif "factory" in raw:
            factory_path = raw["factory"]
            if not isinstance(factory_path, str) or ":" not in factory_path:
                raise TypertLoaderError(
                    f"{subject}: schema {name!r} factory must be 'module:function'.",
                )
            factory = _load_factory(factory_path, subject=subject)

        else:
            raise TypertLoaderError(
                f"{subject}: schema {name!r} needs a 'schema' or a 'factory'.",
            )
        schemas.append((name, factory))

    raw_invocations = manifest.get("invocations", [])
    if not isinstance(raw_invocations, list):
        raise TypertLoaderError(f"{subject}: invocations must be an array.")
    descriptors: list[RemoteDescriptor] = []
    for index, raw in enumerate(raw_invocations):
        descriptors.append(_descriptor_from_mapping(raw, subject=subject, index=index))

    package_model = manifest.get("model")
    return RemoteContribution(
        package=package,
        face=face,
        schemas=tuple(schemas),
        descriptors=tuple(descriptors),
        package_model=package_model if isinstance(package_model, Mapping) else None,
    )


def _descriptor_from_mapping(
    raw: Any,
    *,
    subject: str,
    index: int,
) -> RemoteDescriptor:
    if not isinstance(raw, Mapping):
        raise TypertLoaderError(f"{subject}: invocation {index} must be an object.")
    endpoint = raw.get("id") or raw.get("endpoint")
    namespace = raw.get("namespace")
    method = raw.get("method")
    if isinstance(endpoint, str) and "/" in endpoint and not (namespace and method):
        namespace, method = endpoint.split("/", 1)
    if not isinstance(namespace, str) or not isinstance(method, str):
        raise TypertLoaderError(
            f"{subject}: invocation {index} needs an id or namespace/method.",
        )
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        service = namespace
    raw_parameters = raw.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise TypertLoaderError(f"{subject}: invocation parameters must be an array.")
    parameters: list[RemoteParameter] = []
    for parameter in raw_parameters:
        if not isinstance(parameter, Mapping):
            raise TypertLoaderError(
                f"{subject}: invocation {index} has a non-object parameter.",
            )
        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            raise TypertLoaderError(
                f"{subject}: invocation {index} has an unnamed parameter.",
            )
        source = str(parameter.get("source") or "json")
        if source not in {"json", "lookup"}:
            raise TypertLoaderError(
                f"{subject}: parameter {name!r} has unknown source {source!r}.",
            )
        parameters.append(
            RemoteParameter(
                name=name,
                codec=JsonCodec(type_symbol=str(parameter.get("type_symbol") or "json")),
                source=source,  # type: ignore[arg-type]
                accepts_undefined=bool(parameter.get("accepts_undefined", False)),
                wire=str(parameter.get("wire") or name),
                resource_kind=(
                    str(parameter["resource_kind"]) if parameter.get("resource_kind") else None
                ),
                lookup=(str(parameter["lookup"]) if parameter.get("lookup") else None),
            )
        )
    resources = tuple(
        resource_from_mapping(item)
        for item in raw.get("resources", [])
        if isinstance(item, Mapping)
    ) or (GLOBAL,)
    return RemoteDescriptor(
        id=f"{namespace}/{method}",
        service=service,
        namespace=namespace,
        method=method,
        implementation=None,
        mode=str(raw.get("mode") or "unary"),  # type: ignore[arg-type]
        parameters=tuple(parameters),
        result_codec=JsonCodec(type_symbol=str(raw.get("result_type_symbol") or "json")),
        scope=str(raw["scope"]) if raw.get("scope") else None,
        description=str(raw.get("description") or "") or None,
        resources=resources,
        source=subject,
        invocable=False,
    )


def _load_factory(path: str, *, subject: str) -> Callable[[], Any]:
    module_path, _, attribute = path.partition(":")
    try:
        module = importlib.import_module(module_path)
        factory = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise TypertLoaderError(
            f"{subject}: schema factory {path!r} cannot be imported.",
        ) from exc
    if not callable(factory):
        raise TypertLoaderError(f"{subject}: schema factory {path!r} is not callable.")
    return factory


__all__ = [
    "LoadedPackage",
    "TYPERT_ATTRIBUTE",
    "TYPERT_ENTRY_POINT_GROUP",
    "TYPERT_MANIFEST_FILE",
    "TYPERT_PACKAGE_ATTRIBUTE",
    "TypertLoader",
    "TypertLoaderError",
    "validate_typert_manifest",
]
