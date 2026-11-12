"""Portable, explicit path helpers for AverQel's two per-user roots.

The application-data root and shared agent-configuration root are deliberately
separate.  Resolving either root never creates or migrates user data.
"""

from __future__ import annotations

import os
from pathlib import Path

AVERQEL_HOME_ENV = "AVERQEL_HOME"
AGENTS_HOME_ENV = "AVERQEL_AGENTS_HOME"


def expand_user_path(value: str | os.PathLike[str], *, home: Path | None = None) -> Path:
    """Expand ``~`` and return an absolute path without requiring it to exist."""
    raw = os.fspath(value).strip()
    if not raw:
        raise ValueError("A non-empty path is required.")
    home_path = (home or Path.home()).resolve(strict=False)
    if raw == "~":
        candidate = home_path
    elif raw.startswith("~/") or raw.startswith("~\\"):
        candidate = home_path / raw[2:]
    else:
        candidate = Path(raw).expanduser()
    return candidate.resolve(strict=False)


def resolve_averqel_home(
    *, environ: dict[str, str] | os._Environ[str] | None = None, home: Path | None = None
) -> Path:
    """Resolve the application-data root, honoring ``AVERQEL_HOME``."""
    env = os.environ if environ is None else environ
    configured = str(env.get(AVERQEL_HOME_ENV, "")).strip()
    return (
        expand_user_path(configured, home=home)
        if configured
        else ((home or Path.home()) / ".averqel-harness").resolve(strict=False)
    )


def resolve_agents_home(
    *, environ: dict[str, str] | os._Environ[str] | None = None, home: Path | None = None
) -> Path:
    """Resolve the shared agent root, honoring ``AVERQEL_AGENTS_HOME``."""
    env = os.environ if environ is None else environ
    configured = str(env.get(AGENTS_HOME_ENV, "")).strip()
    return (
        expand_user_path(configured, home=home)
        if configured
        else ((home or Path.home()) / ".agents").resolve(strict=False)
    )


def join_home_path(root: Path, *parts: str | os.PathLike[str]) -> Path:
    """Join relative components to a root while refusing traversal components."""
    result = root.resolve(strict=False)
    for part in parts:
        child = Path(os.fspath(part))
        if child.is_absolute() or any(piece == ".." for piece in child.parts):
            raise ValueError("Path components must stay relative to their root.")
        result = result / child
    return result


def display_path(path: str | os.PathLike[str], *, home: Path | None = None) -> str:
    """Render a path under the user's home with a stable ``~/`` label."""
    raw_path = os.fspath(path)
    resolved = (
        expand_user_path(raw_path, home=home)
        if raw_path == "~" or raw_path.startswith("~/") or raw_path.startswith("~\\")
        else Path(raw_path).resolve(strict=False)
    )
    home_path = (home or Path.home()).resolve(strict=False)
    try:
        relative = resolved.relative_to(home_path)
    except ValueError:
        return str(resolved)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def canonicalize_watch_path(path: str | os.PathLike[str]) -> Path:
    """Canonicalize an existing path or its deepest existing ancestor.

    Watch targets can be temporarily missing during an editor's atomic-save
    sequence. Resolving the nearest existing ancestor still collapses symlink
    aliases without requiring the final target to exist.
    """
    candidate = Path(os.path.abspath(os.fspath(path)))
    suffix: list[str] = []
    ancestor = candidate
    while not ancestor.exists() and not ancestor.is_symlink():
        if ancestor.parent == ancestor:
            break
        suffix.append(ancestor.name)
        ancestor = ancestor.parent
    canonical = ancestor.resolve(strict=False)
    for name in reversed(suffix):
        canonical /= name
    return Path(os.path.normcase(str(canonical)))
