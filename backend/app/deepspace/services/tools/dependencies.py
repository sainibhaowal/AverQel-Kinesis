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

import importlib.util
import os
import shutil
import sys
from typing import Any

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
WORKSPACE_LIBRARIES: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("python-docx", "docx"),
    ("python-pptx", "pptx"),
    ("openpyxl", "openpyxl"),
    ("Pillow", "PIL"),
    ("lxml", "lxml"),
    ("XlsxWriter", "xlsxwriter"),
)


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


def workspace_dependencies() -> dict[str, Any]:
    """Report the libraries and binaries the deployment ships.

    Returns:
        The path of every installed library, keyed by the name the library
        is known by, and the paths of the Node, pnpm, and Python binaries.
        A binary the host does not carry is reported as None.
    """
    packages: dict[str, str] = {}
    for label, module_name in WORKSPACE_LIBRARIES:
        path = _module_path(module_name)
        if path is not None:
            packages[label] = path
    return {
        "python_packages": packages,
        "node": _binary_path("node"),
        "pnpm": _binary_path("pnpm"),
        "python": os.path.abspath(sys.executable),
    }


async def _load_workspace_dependencies(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    """Report the libraries and binaries the deployment ships."""
    del arguments
    del context
    return {"status": "ok", **workspace_dependencies()}


LOAD_WORKSPACE_DEPENDENCIES_DESCRIPTION = (
    "Report where the libraries and binaries this deployment ships live, so a program "
    "you write can import them. The result names the Python packages that are "
    "installed, with the absolute path of each one, and the paths of the node, pnpm, "
    "and python binaries. A package that is not installed is left out, and a binary "
    "this host does not carry is reported as null."
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
    "workspace_dependencies",
]
