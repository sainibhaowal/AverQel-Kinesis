"""File access permission management for local desktop deployments.

This module manages workspace file access permissions for the local desktop
deployment. It handles user consent for file access outside the workspace
root and persists user decisions.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from app.core.averqel_home import get_averqel_home

logger = logging.getLogger(__name__)


class FileAccessDecision(Enum):
    """User's decision for a file access request."""

    ALLOW = "allow"
    DENY = "deny"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_DENY = "always_deny"


class FileAccessScope(Enum):
    """Scope of a file access permission."""

    WORKSPACE = "workspace"  # Inside workspace root
    HOME = "home"  # User home directory
    SYSTEM = "system"  # System paths
    EXTERNAL = "external"  # Other locations


@dataclass(frozen=True, slots=True)
class FileAccessRequest:
    """A request for file access."""

    path: str
    scope: FileAccessScope
    operation: str  # "read", "write", "delete", "list"
    requested_by: str  # "backend", "subagent", "user"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FileAccessPermission:
    """A stored file access permission."""

    path_pattern: str  # Glob pattern or exact path
    scope: FileAccessScope
    operations: frozenset[str]  # Operations allowed
    decision: FileAccessDecision
    created_at: datetime
    expires_at: datetime | None = None
    source: str = "user"  # "user", "system", "default"

    def matches(self, path: str, operation: str) -> bool:
        """Check if this permission matches the given path and operation."""
        import fnmatch

        if operation not in self.operations:
            return False

        # Check if path matches the pattern
        if self.path_pattern == "**" or fnmatch.fnmatch(path, self.path_pattern):
            return True

        # Check if path is under the pattern directory
        try:
            pattern_path = Path(self.path_pattern).resolve()
            request_path = Path(path).resolve()
            if pattern_path in request_path.parents or pattern_path == request_path:
                return True
        except (OSError, ValueError):
            pass

        return False


class FilePermissionStore:
    """Manages file access permissions for the local desktop app."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or get_averqel_home()
        self._lock = threading.RLock()
        self._permissions: dict[str, FileAccessPermission] = {}
        self._initialized = False
        self._permission_file = self._home / "file_permissions.json"

    def _ensure_initialized(self) -> None:
        """Load permissions from disk if not already loaded."""
        if self._initialized:
            return

        with self._lock:
            if getattr(self, "_initialized", False):
                return

            self._load_from_disk()
            self._initialized = True

    def _load_from_disk(self) -> None:
        """Load permissions from the JSON file."""
        try:
            if self._permission_file.exists():
                content = self._permission_file.read_text(encoding="utf-8")
                data = json.loads(content)

                for item in data.get("permissions", []):
                    try:
                        permission = FileAccessPermission(
                            path_pattern=item["path_pattern"],
                            scope=FileAccessScope(item["scope"]),
                            operations=frozenset(item["operations"]),
                            decision=FileAccessDecision(item["decision"]),
                            created_at=datetime.fromisoformat(item["created_at"]),
                            expires_at=(
                                datetime.fromisoformat(item["expires_at"])
                                if item.get("expires_at")
                                else None
                            ),
                            source=item.get("source", "user"),
                        )
                        self._permissions[permission_key(permission)] = permission
                    except (KeyError, ValueError, TypeError) as exc:
                        logger.warning("Failed to load file permission: %s", exc, exc_info=True)

                logger.info(
                    "Loaded %d file access permissions from disk",
                    len(self._permissions),
                )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load file permissions: %s", exc)

    def _save_to_disk(self) -> None:
        """Save permissions to the JSON file."""
        try:
            data = {
                "version": 1,
                "updated_at": datetime.now(UTC).isoformat(),
                "permissions": [
                    {
                        "path_pattern": p.path_pattern,
                        "scope": p.scope.value,
                        "operations": list(p.operations),
                        "decision": p.decision.value,
                        "created_at": p.created_at.isoformat(),
                        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                        "source": p.source,
                    }
                    for p in self._permissions.values()
                ],
            }

            # Atomic write
            temp_file = self._permission_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(self._permission_file)
        except OSError as exc:
            logger.error("Failed to save file permissions: %s", exc)

    def check_permission(
        self, path: str, operation: str, scope: FileAccessScope | None = None
    ) -> FileAccessDecision | None:
        """Check if access to the given path is allowed."""
        self._ensure_initialized()

        with self._lock:
            for permission in self._permissions.values():
                if permission.matches(path, operation):
                    if permission.decision in (
                        FileAccessDecision.ALLOW,
                        FileAccessDecision.ALWAYS_ALLOW,
                    ):
                        return FileAccessDecision.ALLOW
                    if permission.decision in (
                        FileAccessDecision.DENY,
                        FileAccessDecision.ALWAYS_DENY,
                    ):
                        return FileAccessDecision.DENY

        return None

    def request_permission(
        self,
        path: str,
        operation: str,
        scope: FileAccessScope,
        requested_by: str = "backend",
    ) -> FileAccessDecision:
        """Request permission for file access. Returns the decision."""
        self._ensure_initialized()

        # Check existing permissions first
        existing = self.check_permission(path, "read")
        if existing:
            return existing

        # For workspace scope, auto-allow
        if scope == FileAccessScope.WORKSPACE:
            return FileAccessDecision.ALLOW

        # For local desktop, we auto-allow home directory access
        if scope == FileAccessScope.HOME:
            try:
                home = Path.home()
                Path(path).resolve().relative_to(home)
                return FileAccessDecision.ALLOW
            except (ValueError, OSError):
                pass

        # For system paths, deny by default
        if scope == FileAccessScope.SYSTEM:
            return FileAccessDecision.DENY

        # For external paths, we would show a prompt (handled by desktop app)
        # For now, deny by default for external
        return FileAccessDecision.DENY

    def grant_permission(
        self,
        path_pattern: str,
        scope: FileAccessScope,
        operations: set[str],
        decision: FileAccessDecision = FileAccessDecision.ALWAYS_ALLOW,
        source: str = "user",
    ) -> None:
        """Grant a file access permission."""
        self._ensure_initialized()

        with self._lock:
            permission = FileAccessPermission(
                path_pattern=path_pattern,
                scope=scope,
                operations=frozenset(operations),
                decision=decision,
                created_at=datetime.now(UTC),
                source=source,
            )

            self._permissions[permission_key(permission)] = permission
            self._save_to_disk()

            logger.info(
                "Granted file permission: %s (%s) for operations: %s",
                path_pattern,
                scope.value,
                operations,
            )

    def revoke_permission(self, path_pattern: str, scope: FileAccessScope) -> bool:
        """Revoke a file access permission."""
        self._ensure_initialized()

        with self._lock:
            key = f"{scope.value}:{path_pattern}"
            if key in self._permissions:
                del self._permissions[key]
                self._save_to_disk()
                logger.info("Revoked file permission: %s (%s)", path_pattern, scope.value)
                return True

        return False

    def list_permissions(self) -> list[FileAccessPermission]:
        """List all stored permissions."""
        self._ensure_initialized()

        with self._lock:
            return list(self._permissions.values())


def permission_key(permission: FileAccessPermission) -> str:
    """Generate a unique key for a permission."""
    return f"{permission.scope.value}:{permission.path_pattern}"


# Global instance
_file_permission_store: FilePermissionStore | None = None


def get_file_permission_store() -> FilePermissionStore:
    """Get the global file permission store instance."""
    global _file_permission_store
    if _file_permission_store is None:
        _file_permission_store = FilePermissionStore()
    return _file_permission_store


__all__ = [
    "FileAccessDecision",
    "FileAccessScope",
    "FileAccessRequest",
    "FileAccessPermission",
    "FilePermissionStore",
    "get_file_permission_store",
    "permission_key",
]
