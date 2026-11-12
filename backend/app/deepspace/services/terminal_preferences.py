"""Tenant- and user-owned, non-sensitive terminal preferences.

This is a deliberate consumer of DeepSpace's storage-domain facility. The
domain stores only a default shell and terminal dimensions; conversation
content, credentials, paths, and process state remain in their authoritative
stores. Every record key is derived from the authenticated tenant and user,
and callers never supply a storage key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.auth.dependencies import AuthContext
from app.deepspace.services.storage_domain import (
    DomainSpec,
    define_domain,
    domain_table,
)

TerminalShell = Literal["bash", "pwsh", "cmd"]

DEFAULT_TERMINAL_PREFERENCES: dict[str, Any] = {
    "default_shell": None,
    "rows": 40,
    "columns": 160,
}

TERMINAL_PREFERENCES_DOMAIN = define_domain(
    DomainSpec(
        name="deepspace_terminal_preferences",
        version=1,
        tables={"users": domain_table("json")},
    )
)


@dataclass(frozen=True, slots=True)
class TerminalPreferences:
    """Validated settings applied when a user opens a terminal."""

    default_shell: TerminalShell | None = None
    rows: int = 40
    columns: int = 160

    def as_dict(self) -> dict[str, Any]:
        return {
            "default_shell": self.default_shell,
            "rows": self.rows,
            "columns": self.columns,
        }


class TerminalPreferencesStore:
    """Access terminal preferences through a tenant/user-scoped domain key."""

    def __init__(self, domain: Any) -> None:
        self._table = domain.table("users")

    @staticmethod
    def _key(auth: AuthContext) -> str:
        """Return the store-owned composite key for an authenticated identity."""
        tenant_id = UUID(str(auth.tenant_id))
        user_id = UUID(str(auth.user_id))
        return f"{tenant_id}:{user_id}"

    @staticmethod
    def _parse(value: Any) -> TerminalPreferences:
        """Validate persisted data and safely fall back for old/corrupt values."""
        if not isinstance(value, dict):
            return TerminalPreferences()
        shell = value.get("default_shell")
        if shell is not None and (
            not isinstance(shell, str) or shell not in {"bash", "pwsh", "cmd"}
        ):
            shell = None
        rows = value.get("rows", 40)
        columns = value.get("columns", 160)
        if isinstance(rows, bool) or not isinstance(rows, int) or not 10 <= rows <= 200:
            rows = 40
        if isinstance(columns, bool) or not isinstance(columns, int) or not 20 <= columns <= 400:
            columns = 160
        return TerminalPreferences(default_shell=shell, rows=rows, columns=columns)

    async def get(self, auth: AuthContext) -> TerminalPreferences:
        """Read only the preference row owned by this authenticated identity."""
        return self._parse(self._table.get(self._key(auth)))

    async def put(
        self,
        auth: AuthContext,
        *,
        default_shell: TerminalShell | None,
        rows: int,
        columns: int,
    ) -> TerminalPreferences:
        """Persist validated, non-sensitive terminal preferences."""
        if default_shell not in {None, "bash", "pwsh", "cmd"}:
            raise ValueError("default_shell must be bash, pwsh, cmd, or null")
        if isinstance(rows, bool) or not isinstance(rows, int) or not 10 <= rows <= 200:
            raise ValueError("rows must be an integer between 10 and 200")
        if isinstance(columns, bool) or not isinstance(columns, int) or not 20 <= columns <= 400:
            raise ValueError("columns must be an integer between 20 and 400")
        preferences = TerminalPreferences(default_shell, rows, columns)
        await self._table.put(self._key(auth), preferences.as_dict())
        return preferences


__all__ = [
    "DEFAULT_TERMINAL_PREFERENCES",
    "TERMINAL_PREFERENCES_DOMAIN",
    "TerminalPreferences",
    "TerminalPreferencesStore",
    "TerminalShell",
]
