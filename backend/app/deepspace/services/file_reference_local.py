"""Local file reference provider for DeepSpace.

Provides fuzzy search and indexing for @file references in the workspace.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Configuration
MAX_INDEXED_FILES = 10_000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
INDEX_REFRESH_INTERVAL = 300  # 5 minutes
SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
        "env",
        ".env",
        "build",
        "dist",
        "target",
        ".next",
        ".cache",
        "coverage",
        ".pytest_cache",
        "site-packages",
        "vendor",
        "bin",
        "obj",
        ".idea",
        ".vscode",
    }
)
SKIP_EXTS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".mkv",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".lock",
        ".log",
        ".tmp",
        ".temp",
        ".swp",
        ".swo",
    }
)


@dataclass(frozen=True, slots=True)
class FileReferenceEntry:
    """A file entry in the reference index."""

    path: str  # Relative to workspace root
    size: int
    mtime: float
    extension: str


class FileReferenceIndex:
    """Fuzzy-searchable index of workspace files."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._entries: list[FileReferenceEntry] = []
        self._path_to_entry: dict[str, FileReferenceEntry] = {}
        self._last_refresh = 0.0
        self._lock = threading.RLock()

    def _should_skip_dir(self, dirname: str) -> bool:
        return dirname.startswith(".") or dirname in SKIP_DIRS

    def _should_skip_file(self, filename: str) -> bool:
        if filename.startswith("."):
            return True
        ext = Path(filename).suffix.lower()
        return ext in SKIP_EXTS

    def _scan_workspace(self) -> list[FileReferenceEntry]:
        """Scan workspace and return file entries."""
        entries = []
        count = 0

        try:
            for root, dirs, files in os.walk(self.workspace_root, topdown=True):
                # Filter directories in-place
                dirs[:] = [d for d in dirs if not self._should_skip_dir(d)]

                root_path = Path(root)
                for filename in files:
                    if count >= MAX_INDEXED_FILES:
                        break

                    if self._should_skip_file(filename):
                        continue

                    file_path = root_path / filename
                    try:
                        stat = file_path.stat()
                        if stat.st_size > MAX_FILE_SIZE:
                            continue

                        rel_path = file_path.relative_to(self.workspace_root).as_posix()
                        ext = Path(filename).suffix.lower()

                        entry = FileReferenceEntry(
                            path=rel_path, size=stat.st_size, mtime=stat.st_mtime, extension=ext
                        )
                        entries.append(entry)
                        count += 1
                    except (OSError, ValueError):
                        continue

        except Exception as e:
            logger.warning("Workspace scan failed: %s", e)

        return entries

    def refresh(self, force: bool = False) -> None:
        """Refresh the file index."""
        with self._lock:
            now = time.time()
            if not force and (now - self._last_refresh) < INDEX_REFRESH_INTERVAL:
                return

            entries = self._scan_workspace()
            self._entries = entries
            self._path_to_entry = {e.path: e for e in entries}
            self._last_refresh = now
            logger.debug("Refreshed file index: %d files", len(entries))

    def search(self, query: str, limit: int = 20) -> list[FileReferenceEntry]:
        """Fuzzy search for files matching the query."""
        self.refresh()

        query_lower = query.lower()
        # Split query into parts for multi-term matching
        query_parts = [p for p in query_lower.split() if p]

        with self._lock:
            if not query_parts:
                return self._entries[:limit]

            scored = []
            for entry in self._entries:
                path_lower = entry.path.lower()
                score = 0

                # Exact path match (highest score)
                if query_lower == path_lower:
                    score = 1000
                # Path ends with query
                elif path_lower.endswith(query_lower):
                    score = 800
                # Filename matches exactly
                elif query_lower == Path(entry.path).name.lower():
                    score = 700
                # Filename contains query
                elif query_lower in Path(entry.path).name.lower():
                    score = 500
                # Path contains query
                elif query_lower in path_lower:
                    score = 300
                # Multi-term: all parts in path
                elif all(part in path_lower for part in query_parts):
                    score = 200
                # Multi-term: all parts in filename
                elif all(part in Path(entry.path).name.lower() for part in query_parts):
                    score = 150
                # Fuzzy: any part in path
                elif any(part in path_lower for part in query_parts):
                    score = 50

                if score > 0:
                    # Boost score for common code extensions
                    if entry.extension in {
                        ".py",
                        ".js",
                        ".ts",
                        ".tsx",
                        ".jsx",
                        ".md",
                        ".json",
                        ".yaml",
                        ".yml",
                        ".toml",
                        ".ini",
                        ".cfg",
                    }:
                        score += 50
                    scored.append((score, entry))

            scored.sort(key=lambda x: -x[0])
            return [e for _, e in scored[:limit]]

    def get_by_path(self, path: str) -> FileReferenceEntry | None:
        """Get entry by exact path."""
        self.refresh()
        with self._lock:
            return self._path_to_entry.get(path)

    def get_all(self) -> list[FileReferenceEntry]:
        """Get all entries."""
        self.refresh()
        with self._lock:
            return list(self._entries)


class FileReferenceLocalService:
    """Service for local file references with @file grammar support."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._index: FileReferenceIndex | None = None
        if workspace_root:
            self.set_workspace_root(workspace_root)

    def set_workspace_root(self, workspace_root: Path) -> None:
        """Set or change the workspace root."""
        self._index = FileReferenceIndex(Path(workspace_root).resolve())

    def is_available(self) -> bool:
        return self._index is not None

    def search_files(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search for files matching query."""
        if not self._index:
            return []

        entries = self._index.search(query, limit)
        return [
            {
                "path": e.path,
                "size": e.size,
                "mtime": e.mtime,
                "extension": e.extension,
            }
            for e in entries
        ]

    def resolve_file_ref(self, ref: str) -> dict[str, Any] | None:
        """Resolve a @file reference to file info."""
        if not self._index:
            return None

        # Strip @ prefix if present
        path = ref[1:] if ref.startswith("@") else ref
        entry = self._index.get_by_path(path)

        if entry:
            return {
                "path": entry.path,
                "size": entry.size,
                "mtime": entry.mtime,
                "extension": entry.extension,
                "resolved": True,
            }

        # Try fuzzy search
        entries = self._index.search(path, limit=1)
        if entries:
            entry = entries[0]
            return {
                "path": entry.path,
                "size": entry.size,
                "mtime": entry.mtime,
                "extension": entry.extension,
                "resolved": True,
                "fuzzy": True,
            }

        return None

    def get_all_files(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get all indexed files."""
        if not self._index:
            return []
        entries = self._index.get_all()[:limit]
        return [
            {
                "path": e.path,
                "size": e.size,
                "mtime": e.mtime,
                "extension": e.extension,
            }
            for e in entries
        ]

    def refresh_index(self) -> None:
        """Force refresh the index."""
        if self._index:
            self._index.refresh(force=True)


# Global service instance
_file_reference_service: FileReferenceLocalService | None = None


def get_file_reference_service() -> FileReferenceLocalService | None:
    """Get the global file reference service."""
    return _file_reference_service


def init_file_reference_service(workspace_root: Path) -> FileReferenceLocalService:
    """Initialize the global file reference service."""
    global _file_reference_service
    _file_reference_service = FileReferenceLocalService(workspace_root)
    return _file_reference_service


def get_workspace_files(
    workspace_root: Path, query: str = "", limit: int = 20
) -> list[dict[str, Any]]:
    """Search workspace files (standalone function)."""
    index = FileReferenceIndex(Path(workspace_root).resolve())
    entries = index.search(query, limit)
    return [
        {
            "path": e.path,
            "size": e.size,
            "mtime": e.mtime,
            "extension": e.extension,
        }
        for e in entries
    ]


# @file grammar parsing
FILE_REF_PATTERN = re.compile(r"@([^\s\)\]\}]+)")


def parse_file_refs(text: str) -> list[str]:
    """Extract @file references from text."""
    return FILE_REF_PATTERN.findall(text)


def resolve_file_refs_in_text(text: str, workspace_root: Path) -> list[dict[str, Any]]:
    """Resolve all @file references in text."""
    refs = parse_file_refs(text)
    results = []
    index = FileReferenceIndex(Path(workspace_root).resolve())

    for ref in refs:
        entry = index.get_by_path(ref)
        if entry:
            results.append(
                {
                    "ref": f"@{ref}",
                    "path": entry.path,
                    "resolved": True,
                    "size": entry.size,
                    "extension": entry.extension,
                }
            )
        else:
            # Try fuzzy
            entries = index.search(ref, limit=1)
            if entries:
                e = entries[0]
                results.append(
                    {
                        "ref": f"@{ref}",
                        "path": e.path,
                        "resolved": True,
                        "fuzzy": True,
                        "size": e.size,
                        "extension": e.extension,
                    }
                )
            else:
                results.append(
                    {
                        "ref": f"@{ref}",
                        "resolved": False,
                    }
                )

    return results


__all__ = [
    "FileReferenceEntry",
    "FileReferenceIndex",
    "FileReferenceLocalService",
    "get_file_reference_service",
    "init_file_reference_service",
    "get_workspace_files",
    "parse_file_refs",
    "resolve_file_refs_in_text",
    "FILE_REF_PATTERN",
    "MAX_INDEXED_FILES",
    "INDEX_REFRESH_INTERVAL",
]
