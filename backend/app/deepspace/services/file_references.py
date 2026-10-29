"""File reference discovery for DeepSpace.

Ported from NeoSIS @averqel/neosis-file-reference and @averqel/neosis-file-reference-local.
Provides @file completion and discovery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
)


@dataclass(frozen=True, slots=True)
class FileReferenceCandidate:
    """A file or directory candidate for @file completion."""

    path: str
    is_directory: bool
    size: int | None = None
    mime_type: str | None = None


class FileReferenceService:
    """Service for discovering file references in the workspace."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._max_candidates = getattr(settings, "deepspace_file_references_max_candidates", 50)

    async def list(
        self,
        workspace_root: Path,
        query: str,
        signal: Any = None,
    ) -> list[FileReferenceCandidate]:
        """List file and directory candidates for a query.

        Args:
            workspace_root: The workspace root to search in.
            query: The query string (path prefix or pattern).
            signal: Optional cancellation signal.

        Returns:
            List of matching candidates.
        """
        if not query:
            return []

        # Normalize query
        query = query.strip()
        if not query:
            return []

        try:
            # Resolve the query as a path prefix
            base_path = resolve_within_root(workspace_root, query)
            search_dir = base_path.absolute

            # If the query ends with / or is a directory, list its contents
            if query.endswith("/") or search_dir.is_dir():
                return await self._list_directory(search_dir, base_path.relative)
            else:
                # List parent directory and filter by prefix
                parent_dir = search_dir.parent
                parent_rel = (
                    base_path.relative.rsplit("/", 1)[0] if "/" in base_path.relative else ""
                )
                return await self._list_directory_with_prefix(
                    parent_dir, parent_rel, search_dir.name
                )
        except WorkspacePathError:
            # Query doesn't resolve to a valid path, try fuzzy matching
            return await self._fuzzy_search(workspace_root, query)

    async def _list_directory(self, abs_dir: Path, rel_dir: str) -> list[FileReferenceCandidate]:
        """List contents of a directory."""
        candidates = []
        try:
            for entry in sorted(abs_dir.iterdir(), key=lambda e: e.name):
                if len(candidates) >= self._max_candidates:
                    break

                rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if entry.is_dir():
                    rel_path += "/"

                stat = entry.stat()
                candidates.append(
                    FileReferenceCandidate(
                        path=rel_path,
                        is_directory=entry.is_dir(),
                        size=stat.st_size if entry.is_file() else None,
                        mime_type=self._guess_mime(entry) if entry.is_file() else None,
                    )
                )
        except (OSError, PermissionError):
            pass
        return candidates

    async def _list_directory_with_prefix(
        self, abs_dir: Path, rel_dir: str, prefix: str
    ) -> list[FileReferenceCandidate]:
        """List directory contents filtered by prefix."""
        candidates = []
        try:
            for entry in sorted(abs_dir.iterdir(), key=lambda e: e.name):
                if len(candidates) >= self._max_candidates:
                    break
                if not entry.name.startswith(prefix):
                    continue

                rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if entry.is_dir():
                    rel_path += "/"

                stat = entry.stat()
                candidates.append(
                    FileReferenceCandidate(
                        path=rel_path,
                        is_directory=entry.is_dir(),
                        size=stat.st_size if entry.is_file() else None,
                        mime_type=self._guess_mime(entry) if entry.is_file() else None,
                    )
                )
        except (OSError, PermissionError):
            pass
        return candidates

    async def _fuzzy_search(self, workspace_root: Path, query: str) -> list[FileReferenceCandidate]:
        """Fuzzy search for files matching the query."""
        candidates = []
        query_lower = query.lower()

        for root, dirs, files in os.walk(workspace_root):
            if len(candidates) >= self._max_candidates:
                break

            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for fname in files:
                if len(candidates) >= self._max_candidates:
                    break
                if query_lower in fname.lower():
                    abs_path = Path(root) / fname
                    try:
                        rel_path = abs_path.relative_to(workspace_root).as_posix()
                        stat = abs_path.stat()
                        candidates.append(
                            FileReferenceCandidate(
                                path=rel_path,
                                is_directory=False,
                                size=stat.st_size,
                                mime_type=self._guess_mime(abs_path),
                            )
                        )
                    except (OSError, ValueError):
                        pass

        return candidates

    def _guess_mime(self, path: Path) -> str | None:
        """Guess MIME type from file extension."""
        ext = path.suffix.lower()
        mime_map = {
            ".py": "text/x-python",
            ".js": "text/javascript",
            ".ts": "text/typescript",
            ".json": "application/json",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".html": "text/html",
            ".css": "text/css",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
            ".toml": "application/toml",
            ".rs": "text/x-rust",
            ".go": "text/x-go",
            ".java": "text/x-java",
            ".cpp": "text/x-c++",
            ".c": "text/x-c",
            ".h": "text/x-c",
            ".sh": "text/x-shellscript",
            ".sql": "application/sql",
        }
        return mime_map.get(ext)


def get_file_reference_service(settings: Settings) -> FileReferenceService:
    """Get the file reference service instance."""
    return FileReferenceService(settings)
