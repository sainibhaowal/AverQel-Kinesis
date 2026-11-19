"""File system observation policy for DeepSpace.

Ported from @deepseek-ai/dsh-fs-observation-policy. Automatically records
file versions on read and enforces them on subsequent mutations, so the
agent doesn't need to manually pass expected_version.

The policy maintains an in-memory map of (tenant, conversation, path) ->
last-observed version. On read, the version is recorded. On write/edit,
the policy checks the recorded version against the current file version.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path


def file_version(path: Path) -> str:
    """Return the version of one workspace file.

    The version is the lowercase hexadecimal SHA-256 digest of the file's
    bytes, the same hash the workspace snapshot records, so a version the
    agent read stays valid wherever it is used.

    Args:
        path: The absolute path of the file.

    Returns:
        The file's version.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class ObservedFile:
    """A file version observed during a read."""

    path: str
    version: str | None
    present: bool


@dataclass
class ObservationPolicy:
    """In-memory observation policy for file mutations.

    Tracks which files have been read and their versions, so mutations
    can enforce version guards without the caller explicitly passing
    expected_version.
    """

    _observations: dict[str, ObservedFile] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, tenant_id: str, conversation_id: str, path: str) -> str:
        return f"{tenant_id}:{conversation_id}:{path}"

    def observe(
        self, tenant_id: str, conversation_id: str, path: str, version: str | None, present: bool
    ) -> None:
        """Record a file observation after a read."""
        key = self._key(tenant_id, conversation_id, path)
        with self._lock:
            self._observations[key] = ObservedFile(path=path, version=version, present=present)

    def get_observed(self, tenant_id: str, conversation_id: str, path: str) -> ObservedFile | None:
        """Get the last observed version for a file."""
        key = self._key(tenant_id, conversation_id, path)
        with self._lock:
            return self._observations.get(key)

    def check_mutation(
        self, tenant_id: str, conversation_id: str, path: str, current_version: str | None
    ) -> str | None:
        """Check if a mutation is allowed based on observed state.

        Returns None if allowed, or an error message if refused.
        """
        observed = self.get_observed(tenant_id, conversation_id, path)
        if observed is None:
            # File was never read — allow create (if absent) or require read first
            if current_version is None:
                # File doesn't exist — allow create
                return None
            # File exists but was never read — refuse
            return "FS_NOT_OBSERVED: Read the file before modifying it."
        if not observed.present and current_version is not None:
            # File was observed as absent but now exists
            return "FS_STALE: File was created since last read."
        if observed.present and observed.version != current_version:
            return "FS_STALE_VERSION: File changed since last read. Re-read and retry."
        return None

    def clear(self, tenant_id: str, conversation_id: str) -> None:
        """Clear all observations for a conversation."""
        prefix = f"{tenant_id}:{conversation_id}:"
        with self._lock:
            keys_to_remove = [k for k in self._observations if k.startswith(prefix)]
            for k in keys_to_remove:
                del self._observations[k]

    def record_read(
        self,
        tenant_id: str,
        conversation_id: str,
        path: str,
        version: str | None,
        present: bool,
    ) -> None:
        """Record a file observation after a read. Convenience wrapper."""
        self.observe(tenant_id, conversation_id, path, version, present)

    def check_write(
        self,
        tenant_id: str,
        conversation_id: str,
        path: str,
        current_version: str | None,
        expected_version: str | None = None,
        strict: bool = True,  # Changed default to True for enforcement by default
    ) -> str | None:
        """Check if a write/edit/delete is allowed based on observed state.

        Returns None if allowed, or an error code string if refused.

        Args:
            expected_version: If provided, enforce version check strictly.
            strict: If True, enforce observation policy strictly even
                without explicit expected_version. Default is True for enforcement by default.
        """
        # If expected_version is provided, always enforce
        if expected_version is not None:
            return self.check_mutation(tenant_id, conversation_id, path, current_version)
        # If strict mode (default True), enforce observation policy
        if strict:
            return self.check_mutation(tenant_id, conversation_id, path, current_version)
        # Advisory mode: don't enforce observation policy
        return None


# Global instance
_policy: ObservationPolicy | None = None


def get_observation_policy() -> ObservationPolicy:
    """Get the global observation policy instance."""
    global _policy
    if _policy is None:
        _policy = ObservationPolicy()
    return _policy


def set_observation_policy(policy: ObservationPolicy | None) -> None:
    """Set the global observation policy instance (for testing)."""
    global _policy
    _policy = policy


def clear_observation_policy() -> None:
    """Clear the global observation policy instance (for testing)."""
    global _policy
    _policy = None


__all__ = [
    "ObservedFile",
    "ObservationPolicy",
    "get_observation_policy",
    "set_observation_policy",
    "clear_observation_policy",
]
