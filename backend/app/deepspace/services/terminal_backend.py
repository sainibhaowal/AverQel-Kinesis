"""Pluggable terminal backend interface for DeepSpace.

Python-native port of the DeepSeek Harness TerminalBackend seam. Allows
registering multiple terminal backends (shell, ssh, etc.) behind a common
interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TerminalBackendSpawnSpec:
    owner: Any = None
    cwd: str | None = None
    command: str | None = None
    argv: Sequence[str] = field(default_factory=tuple)
    shell: str = "bash"
    rows: int = 40
    cols: int = 160
    shell_activity: bool = False
    grace_ms: int = 3000
    max_buffer_bytes: int = 262_144
    idle_timeout_seconds: float = 1800.0
    env: dict[str, str] | None = None


class TerminalBackendSession(Protocol):
    """The process/session surface consumed by DeepSpace terminal tools."""

    pty: bool
    idle_deadline: float
    buffer: Any
    pid: int
    returncode: int | None
    process: Any

    @property
    def is_alive(self) -> bool: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def pending_bytes(self) -> int: ...

    def touch(self) -> None: ...

    def read(self, max_bytes: int) -> str: ...

    def peek(self, max_bytes: int) -> str: ...

    def subscribe(self) -> Any: ...

    def unsubscribe(self, queue: Any) -> None: ...

    def resize(self, *, columns: int, rows: int) -> None: ...

    async def write(self, data: str) -> int: ...

    def waits_for_input(self) -> bool: ...

    def foreground_pgid(self) -> int | None: ...

    def signal(self, sig: int) -> bool: ...

    async def close(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class TerminalBackend(ABC):
    """Abstract terminal backend."""

    @abstractmethod
    async def spawn(self, spec: TerminalBackendSpawnSpec) -> TerminalBackendSession:
        pass


class TerminalBackendRegistry:
    """Registry for pluggable terminal backends."""

    def __init__(self) -> None:
        self._backends: dict[str, TerminalBackend] = {}
        self._default: str | None = None

    def register(self, name: str, backend: TerminalBackend, *, default: bool = False) -> None:
        if not name or not name.strip() or name != name.strip():
            raise ValueError("A terminal backend needs a non-empty name.")
        if name in self._backends:
            raise ValueError(f"Terminal backend {name!r} is already registered.")
        self._backends[name] = backend
        if default or self._default is None:
            self._default = name

    def get(self, name: str | None = None) -> TerminalBackend:
        key = name or self._default
        if key is None or key not in self._backends:
            raise KeyError(f"Terminal backend not registered: {key}")
        return self._backends[key]

    def list_backends(self) -> list[str]:
        return list(self._backends.keys())

    def unregister(self, name: str) -> None:
        self._backends.pop(name, None)
        if self._default == name:
            self._default = next(iter(self._backends), None)
