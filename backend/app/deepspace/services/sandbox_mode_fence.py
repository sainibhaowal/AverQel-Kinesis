"""Sandbox mode fence for DeepSpace terminals.

Prevents sandbox mode changes while terminal sessions are live, mirroring
the AverQel KineSis sandbox mode fence.
"""

from __future__ import annotations

from typing import Any


class SandboxModeFenceError(RuntimeError):
    """Raised when a sandbox mode change is blocked by live terminals."""


class SandboxModeFence:
    """Fence that blocks sandbox mode changes while terminals are live."""

    def __init__(self, terminal_manager: Any) -> None:
        self._terminal_manager = terminal_manager

    def check_change_allowed(self, tenant_id: Any, user_id: Any, conversation_id: Any) -> bool:
        """Return True if a sandbox mode change is allowed."""
        try:
            if hasattr(self._terminal_manager, "list_sessions"):
                sessions = self._terminal_manager.list_sessions(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
                )
            elif hasattr(self._terminal_manager, "list"):
                sessions = self._terminal_manager.list(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
                )
            else:
                sessions = []
            if sessions:
                return False
        except Exception:
            pass
        # Also check global
        try:
            if hasattr(self._terminal_manager, "list_all_sessions"):
                all_sessions = self._terminal_manager.list_all_sessions()
            else:
                all_sessions = []
            if all_sessions:
                return False
        except Exception:
            pass
        return True

    def assert_change_allowed(self, tenant_id: Any, user_id: Any, conversation_id: Any) -> None:
        if not self.check_change_allowed(tenant_id, user_id, conversation_id):
            raise SandboxModeFenceError(
                "Cannot change sandbox mode while terminal sessions are active. "
                "Close all terminals first."
            )
