"""The libraries and binaries the deployment ships.

A program the agent writes runs in the deployment's own environment, so it
can import the libraries the deployment installs and call the binaries the
deployment puts on the path.  This module reports where those libraries and
binaries actually are, so the agent writes an import that resolves instead
of guessing at a name or a path.

Every path in the report is one the host really has.  A library that is not
installed is left out, and a binary the host does not carry is reported as
no path at all.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.deepspace.services.runtime_manifest import (
    RUNTIME_PACKAGE_DISTRIBUTIONS,
    RuntimeManifestError,
    load_runtime_manifest,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
)

#: The libraries the deployment ships, with the module each one imports as.
#:
#: The label is the name the library is known by; the module name is what a
#: program writes in its import statement.
WORKSPACE_LIBRARIES = RUNTIME_PACKAGE_DISTRIBUTIONS


class LoadWorkspaceDependenciesArguments(ToolArguments):
    """Report the libraries and binaries the deployment ships."""


def _module_path(module_name: str) -> str | None:
    """Return the absolute path of one importable module.

    Args:
        module_name: The module's import name.

    Returns:
        The path of the module's file, or None when the module is not
        installed or resolves to no file on this host.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        # A module whose parent package cannot be imported is not installed
        # in a usable way, so it is reported as absent.
        return None
    if spec is None:
        return None
    origin = spec.origin
    if origin and os.path.isfile(origin):
        return os.path.abspath(origin)
    for location in spec.submodule_search_locations or ():
        if os.path.isdir(location):
            return os.path.abspath(location)
    return None


def _binary_path(name: str) -> str | None:
    """Return the absolute path of one binary on the path.

    Args:
        name: The binary's name.

    Returns:
        The binary's path, or None when the host does not carry it.
    """
    found = shutil.which(name)
    if found is None:
        return None
    path = os.path.abspath(found)
    return path if os.path.exists(path) else None


def ensure_runtime_payload(settings: Any = None) -> dict[str, Any] | None:
    """Install the packaged dependency payload on first use.

    The desktop installer may ship the payload beside the application instead
    of inside the runtime directory.  When a source is configured, the first
    dependency query copies it to the runtime root.  A fresh target is renamed
    into place atomically; an existing incomplete target is merged in place so
    a user's other files are never removed.

    Args:
        settings: The deployment settings in force.

    Returns:
        A report of the payload location, or None when no payload is
        configured.
    """
    source_value = str(getattr(settings, "deepspace_runtime_payload_source", "") or "").strip()
    if not source_value:
        return None
    source = Path(source_value).expanduser()
    if not source.is_dir():
        raise RuntimeManifestError(f"The dependency payload source {source} is not a directory.")
    root_value = str(getattr(settings, "deepspace_runtime_payload_root", "") or "").strip()
    if root_value:
        target = Path(root_value).expanduser()
    else:
        target = Path(getattr(sys, "_MEIPASS", sys.prefix)) / "dependencies"
    marker = target / "runtime.json"
    if marker.is_file():
        return {"status": "present", "path": str(target), "source": str(source)}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # An earlier interrupted install left a directory without a manifest.
        # Merge into it rather than deleting anything the user may own.
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        if not marker.is_file():
            raise RuntimeManifestError(
                f"The dependency payload at {target} is incomplete and has no runtime.json."
            )
        return {"status": "repaired", "path": str(target), "source": str(source)}
    staging = Path(tempfile.mkdtemp(prefix=".deepspace-deps-", dir=str(target.parent)))
    try:
        shutil.copytree(source, staging / "payload", symlinks=True)
        os.replace(staging / "payload", target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not marker.is_file():
        raise RuntimeManifestError(f"The dependency payload at {target} has no runtime.json.")
    return {"status": "installed", "path": str(target), "source": str(source)}


def workspace_dependencies(settings: Any = None) -> dict[str, Any]:
    """Report the libraries and binaries the deployment ships.

    Returns:
        The path of every installed library, keyed by the name the library
        is known by, and the paths of the Node, pnpm, and Python binaries.
        A binary the host does not carry is reported as None.
    """
    payload = ensure_runtime_payload(settings)
    manifest_path = str(getattr(settings, "deepspace_runtime_manifest_path", "") or "").strip()
    if manifest_path:
        candidate_path = Path(manifest_path).expanduser()
    elif payload is not None:
        candidate_path = Path(payload["path"]) / "runtime.json"
    else:
        runtime_root = getattr(sys, "_MEIPASS", sys.prefix)
        candidate_path = Path(runtime_root) / "runtime.json"

    manifest = None
    if manifest_path:
        manifest = load_runtime_manifest(candidate_path)
    elif candidate_path.is_file():
        manifest = load_runtime_manifest(candidate_path)
    elif getattr(sys, "frozen", False):
        raise RuntimeManifestError("The packaged runtime manifest is missing.")
    packages: dict[str, str] = {}
    versions: dict[str, str] = {}
    package_storage: dict[str, str] = {}
    for label, module_name in WORKSPACE_LIBRARIES:
        if manifest is not None:
            entry = manifest.packages.get(label)
            if entry is not None:
                packages[label] = entry["path"]
                versions[label] = entry["version"]
                package_storage[label] = entry.get("storage", "filesystem")
            continue
        path = _module_path(module_name)
        if path is not None:
            packages[label] = path
            package_storage[label] = "filesystem"
            try:
                versions[label] = importlib.metadata.version(label)
            except importlib.metadata.PackageNotFoundError:
                versions[label] = "unknown"
    binaries = (
        {
            name: _binary_path(name)
            for name in ("node", "pnpm", "soffice", "libreoffice", "pdftoppm")
        }
        if manifest is None
        else {}
    )
    office_runtime: dict[str, str | None] = {}
    office_sources: dict[str, str] = {}
    for name in ("soffice", "libreoffice", "pdftoppm"):
        if manifest is None:
            executable = binaries[name]
            source = "active-host-path" if executable else "unavailable"
        else:
            executable = manifest.executables.get(name)
            source = "manifest" if executable else "unavailable"
            if not executable and getattr(sys, "frozen", False) and not manifest_path:
                executable = _binary_path(name)
                source = "host-path" if executable else "unavailable"
        office_runtime[name] = executable
        office_sources[name] = source
    python_path = manifest.python_path if manifest is not None else os.path.abspath(sys.executable)
    return {
        "python_packages": packages,
        "python_package_versions": versions,
        "python_package_storage": package_storage,
        "node": manifest.executables.get("node") if manifest is not None else binaries["node"],
        "pnpm": manifest.executables.get("pnpm") if manifest is not None else binaries["pnpm"],
        "python": python_path,
        "office_runtime": office_runtime,
        "office_runtime_sources": office_sources,
        "runtime": {
            "source": "manifest" if manifest is not None else "active-interpreter",
            "platform": manifest.platform if manifest is not None else sys.platform,
            "architecture": manifest.architecture if manifest is not None else platform.machine(),
            "python_version": (
                manifest.python_version if manifest is not None else platform.python_version()
            ),
        },
        "payload": payload,
    }


async def _load_workspace_dependencies(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    """Report the libraries and binaries the deployment ships."""
    del arguments
    try:
        return {"status": "ok", **workspace_dependencies(context.settings)}
    except RuntimeManifestError as exc:
        return {
            "status": "unavailable",
            "error_code": "RUNTIME_MANIFEST_INVALID",
            "message": str(exc),
        }


LOAD_WORKSPACE_DEPENDENCIES_DESCRIPTION = (
    "Report the runtime platform, package versions and absolute paths for libraries and binaries this deployment ships, so a program "
    "you write can import them. The result names the Python packages that are "
    "installed, with the absolute path of each one, and the paths of the node, pnpm, "
    "and python binaries. The python_package_storage field says when a package is "
    "embedded in the frozen executable archive. A package that is not installed is "
    "left out. The office_runtime_sources field distinguishes bundled executables "
    "from host-installed Office tools."
)

LOAD_WORKSPACE_DEPENDENCIES_TOOL: NativeToolSpec = define_native_tool(
    name="load_workspace_dependencies",
    description=LOAD_WORKSPACE_DEPENDENCIES_DESCRIPTION,
    properties={},
    arguments_model=LoadWorkspaceDependenciesArguments,
    mode="read",
    handler=_load_workspace_dependencies,
)

#: The tools this module owns.
DEPENDENCY_TOOLS: list[NativeToolSpec] = [LOAD_WORKSPACE_DEPENDENCIES_TOOL]

__all__ = [
    "DEPENDENCY_TOOLS",
    "LOAD_WORKSPACE_DEPENDENCIES_DESCRIPTION",
    "LOAD_WORKSPACE_DEPENDENCIES_TOOL",
    "WORKSPACE_LIBRARIES",
    "LoadWorkspaceDependenciesArguments",
    "ensure_runtime_payload",
    "workspace_dependencies",
]
