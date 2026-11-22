"""Anonymous User Identity service for DeepSpace.

Provides a per-harness-home anonymous UUID that persists across restarts
and is attached to telemetry, feedback, and DeepSeek requests.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

ANONYMOUS_USER_ID_FILE_NAME = ".anonymous-user-id"


class AnonymousUserIdService:
    """
    Provides a stable anonymous user ID per harness home directory.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, str] = {}
        self._home_dir: Path | None = None

    def get_home_dir(self) -> Path:
        """Return the AverQel home directory used to scope the identity."""
        if self._home_dir is None:
            home = (
                os.environ.get("AVERQEL_HOME")
                or os.environ.get("AKS_HOME")
                or os.environ.get("DSH_HOME")
            )
            if home:
                self._home_dir = Path(home).expanduser()
            else:
                self._home_dir = Path.home() / ".averqel-harness"
            self._home_dir.mkdir(parents=True, exist_ok=True)
        return self._home_dir

    def get_id_file_path(self) -> Path:
        """Get the path to the anonymous user ID file."""
        return self.get_home_dir() / ANONYMOUS_USER_ID_FILE_NAME

    def get_or_create_id(self) -> str:
        """
        Get or create the anonymous user ID for this harness home.
        """
        file_path = self.get_id_file_path()

        # Check cache first
        if file_path in self._cache:
            return self._cache[file_path]

        # Try to read existing ID
        try:
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8").strip()
                if self._is_valid_uuid(content):
                    self._cache[file_path] = content
                    return content
        except OSError:
            pass

        # Mint new ID
        new_id = str(uuid.uuid4())

        # Write atomically
        try:
            file_path.write_text(new_id + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning(f"Failed to write anonymous user ID: {e}")

        self._cache[file_path] = new_id
        return new_id

    def _is_valid_uuid(self, value: str) -> bool:
        """Validate UUID format."""
        try:
            uuid.UUID(value)
            return True
        except ValueError:
            return False

    def reset_id(self) -> str:
        """Force reset the anonymous user ID."""
        file_path = self.get_id_file_path()
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._cache.pop(file_path, None)
        return self.get_or_create_id()

    def get_id_for_telemetry(self) -> str:
        """Get the ID formatted for telemetry (user.id resource attribute)."""
        return self.get_or_create_id()

    def get_id_for_feedback(self) -> str:
        """Get the ID formatted for feedback acknowledgement."""
        return self.get_or_create_id()

    def get_id_for_deepseek(self) -> str:
        """Get the ID formatted for DeepSeek requests (x-deepseek-harness-user-id header)."""
        return self.get_or_create_id()


def create_anonymous_user_id_service() -> AnonymousUserIdService:
    """Create the anonymous user ID service."""
    return AnonymousUserIdService()


__all__ = ["AnonymousUserIdService", "create_anonymous_user_id_service"]
