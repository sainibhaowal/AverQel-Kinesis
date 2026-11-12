"""Immutable launch-time environment snapshot with source provenance.

Environment files are read as data, never executed as shell code.  The parser
supports the common single-line ``KEY=value`` form used for application
configuration and intentionally does not expand variables or run commands.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_snapshot_lock = threading.Lock()
_launch_snapshot: LaunchEnvironment | None = None


class EnvironmentSource(StrEnum):
    USER_ENV = "user_env"
    PROJECT_ENV = "project_env"
    PROCESS = "process"


def _read_env_file(path: Path) -> dict[str, str]:
    """Read simple dotenv assignments without interpolation or execution."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


class LaunchEnvironment:
    """A frozen, provenance-aware view of launch environment values.

    Resolution precedence is process > working-directory ``.env`` > user
    ``~/.env``. Keys are case-insensitive on every platform so behavior does
    not change when a profile moves between Windows and Unix.
    """

    def __init__(
        self,
        *,
        process: Mapping[str, str] | None = None,
        project_env: Path | None = None,
        user_env: Path | None = None,
    ) -> None:
        process_values = dict(os.environ if process is None else process)
        project_path = project_env or (Path.cwd() / ".env")
        user_path = user_env or (Path.home() / ".env")
        combined: dict[str, tuple[str, str, EnvironmentSource]] = {}
        for source, values in (
            (EnvironmentSource.USER_ENV, _read_env_file(user_path)),
            (EnvironmentSource.PROJECT_ENV, _read_env_file(project_path)),
            (EnvironmentSource.PROCESS, process_values),
        ):
            for name, value in values.items():
                normalized = name.casefold()
                combined[normalized] = (name, str(value), source)
        self._values = MappingProxyType(dict(combined))
        self._names = MappingProxyType({key: item[0] for key, item in combined.items()})
        self._values_by_name = MappingProxyType({key: item[1] for key, item in combined.items()})
        self._sources = MappingProxyType({key: item[2] for key, item in combined.items()})
        self._process = MappingProxyType(
            {key.casefold(): str(value) for key, value in process_values.items()}
        )

    @property
    def sources(self) -> Mapping[str, EnvironmentSource]:
        """The immutable winning source for each normalized variable name."""
        return self._sources

    def get(
        self, name: str, *, allowed_sources: set[EnvironmentSource] | None = None
    ) -> str | None:
        """Get a value, optionally excluding lower-trust sources."""
        key = name.casefold()
        if key not in self._values_by_name:
            return None
        if allowed_sources is not None and self._sources[key] not in allowed_sources:
            return None
        return self._values_by_name[key]

    def source_for(self, name: str) -> EnvironmentSource | None:
        """Return the winning source for a variable without revealing its value."""
        return self._sources.get(name.casefold())

    def as_dict(self, *, allowed_sources: set[EnvironmentSource] | None = None) -> dict[str, str]:
        """Return a defensive copy of the effective values."""
        return {
            self._names[key]: value
            for key, value in self._values_by_name.items()
            if allowed_sources is None or self._sources[key] in allowed_sources
        }

    @property
    def launched_through_ssh(self) -> bool:
        """Detect SSH only from inherited process values, never dotenv files."""
        return any(self._process.get(name, "").strip() for name in ("ssh_connection", "ssh_tty"))


def get_launch_environment() -> LaunchEnvironment:
    """Return the process-wide immutable snapshot, captured on first use."""
    global _launch_snapshot
    if _launch_snapshot is None:
        with _snapshot_lock:
            if _launch_snapshot is None:
                _launch_snapshot = LaunchEnvironment()
    return _launch_snapshot
