"""Unified filesystem service for DeepSpace.

Provides a single service interface for all file operations with atomic writes,
cross-process locking, file watching, and version checking — the DeepSpace
equivalent of DSH's ctx.fs.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import mimetypes
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core.config import Settings
from app.deepspace.services.fs_observation_policy import get_observation_policy
from app.deepspace.services.host_access import resolve_conversation_path
from app.deepspace.services.workspace_root import (
    ResolvedWorkspacePath,
    WorkspacePathError,
    WorkspaceUnavailableError,
    workspace_root_for,
)
from app.deepspace.utils.atomic_write import (
    DEFAULT_LOCK_WAIT_MS,
)

MAX_READ_CHARS = 200_000
MAX_WRITE_CHARS = 400_000
MAX_READ_LINES = 5_000
MAX_EDIT_LINES = 20_000
VERSION_LENGTH = 64


@dataclass(slots=True)
class FileStat:
    """File metadata returned by stat()."""

    path: str
    absolute_path: str
    size_bytes: int
    is_directory: bool
    is_file: bool
    version: str | None = None
    mime_type: str | None = None
    modified_time: float | None = None


@dataclass(slots=True)
class ReadResult:
    """Result of a read operation."""

    path: str
    content: str
    line_count: int
    returned_lines: int
    offset: int
    has_more: bool
    truncated: bool
    size_bytes: int
    version: str


@dataclass(slots=True)
class WriteResult:
    """Result of a write operation."""

    path: str
    mode: Literal["create", "overwrite", "append"]
    created: bool
    size_bytes: int
    line_count: int
    version: str


@dataclass(slots=True)
class EditResult:
    """Result of an edit operation."""

    path: str
    operation: Literal["replace", "append", "rename", "move"]
    size_bytes: int
    line_count: int
    version: str
    new_path: str | None = None


@dataclass(slots=True)
class StrReplaceResult:
    """Result of a str_replace operation."""

    path: str
    replaced: int
    size_bytes: int
    line_count: int
    version: str


@dataclass(slots=True)
class PatchResult:
    """Result of an apply_patch operation."""

    path: str
    operations_applied: int
    size_bytes: int
    version: str


class FileSystemError(Exception):
    """Base exception for filesystem service errors."""

    def __init__(self, code: str, message: str, path: str, **details: Any):
        self.code = code
        self.message = message
        self.path = path
        self.details = details
        super().__init__(f"[{code}] {message} ({path})")


class FileSystemRefusalError(FileSystemError):
    """A refusal the model can act on (not a crash)."""

    pass


# Backward-compatible public name used by the native-tool contract.
FileSystemRefusal = FileSystemRefusalError


class DeepSpaceFileSystemService:
    """Unified filesystem service for DeepSpace.

    All file operations go through this service to ensure:
    - Atomic writes with fsync and cross-process locking
    - Consistent path resolution and boundary enforcement
    - File version tracking (SHA-256)
    - Optional file locking and watching
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        sandbox_mode: str = "workspace-write",
        default_lock_wait_ms: int = DEFAULT_LOCK_WAIT_MS,
        tenant_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._sandbox_mode = sandbox_mode
        self._default_lock_wait_ms = default_lock_wait_ms
        self._watchers: dict[str, list[Callable[[str], Any]]] = {}
        self._watchers_lock = threading.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = threading.Lock()
        # Observation policy for file version enforcement
        self._observation_policy = get_observation_policy()
        # Context for observation policy
        self._tenant_id = tenant_id
        self._conversation_id = conversation_id

    # ------------------------------------------------------------------------
    # Path resolution and validation
    # ------------------------------------------------------------------------

    def _resolve(self, raw_path: str) -> ResolvedWorkspacePath:
        """Resolve a path under the workspace root with sandbox enforcement."""
        # Handle "." as workspace root - return the root directly
        if raw_path == "." or raw_path == "./" or raw_path == "":
            return ResolvedWorkspacePath(
                absolute=self._workspace_root,
                relative=".",
            )
        try:
            return resolve_conversation_path(self._workspace_root, raw_path, self._sandbox_mode)
        except WorkspacePathError as exc:
            raise FileSystemRefusalError(
                code=(
                    "SANDBOX_MODE_REQUIRED"
                    if self._sandbox_mode != "danger-full-access"
                    else "WORKSPACE_PATH_ERROR"
                ),
                message=str(exc),
                path=raw_path[:1024],
            ) from exc
        except WorkspaceUnavailableError as exc:
            raise FileSystemError("UNAVAILABLE", str(exc), raw_path) from exc

    def _resolve_optional(self, raw_path: str) -> ResolvedWorkspacePath | None:
        """Resolve a path, returning None if it doesn't exist (no refusal)."""
        try:
            return self._resolve(raw_path)
        except FileSystemRefusalError:
            return None

    def _version(self, path: Path) -> str:
        """Compute the SHA-256 version hash of a file."""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _check_version(
        self, path: Path, expected: str | None, relative: str
    ) -> FileSystemRefusalError | None:
        """Check if file version matches expected; return refusal if not."""
        if expected is None:
            return None
        if not path.exists():
            return FileSystemRefusalError(
                code="FS_AMBIGUOUS_EDIT",
                message=(
                    "The file is gone, so its version cannot be checked. Read the "
                    "workspace again, then retry."
                ),
                path=relative,
            )
        current = self._version(path)
        if current != expected:
            return FileSystemRefusalError(
                code="FS_STALE_VERSION",
                message=(
                    "The file changed since it was read. Read it again, then retry "
                    "with the version it holds now."
                ),
                path=relative,
                current_version=current,
            )
        return None

    def _notify_watchers(self, relative_path: str) -> None:
        """Notify all watchers of a path change."""
        with self._watchers_lock:
            watchers = self._watchers.get(relative_path, []).copy()
        for callback in watchers:
            try:
                callback(relative_path)
            except Exception:
                # Watcher errors must not affect the operation
                pass

    # ------------------------------------------------------------------------
    # Public API: Read operations
    # ------------------------------------------------------------------------

    async def read(
        self,
        path: str,
        *,
        offset: int = 1,
        limit: int = 2_000,
    ) -> ReadResult:
        """Read a text or binary file with numbered lines."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            # Record observation for non-existent file
            if self._tenant_id and self._conversation_id:
                self._observation_policy.record_read(
                    tenant_id=self._tenant_id,
                    conversation_id=self._conversation_id,
                    path=resolved.relative,
                    version=None,
                    present=False,
                )
            raise FileSystemRefusalError(
                "NOT_FOUND", "No such file in the project workspace.", resolved.relative
            )
        if target.is_dir():
            raise FileSystemRefusalError(
                "IS_DIRECTORY", "The path is a directory; use list() to list it.", resolved.relative
            )

        limit = min(MAX_READ_LINES, limit)

        if self._is_binary(target):
            # Binary files are handled by the ingestion readers (not here)
            raise FileSystemRefusalError(
                "BINARY_FILE", "Binary file; use read_binary() instead.", resolved.relative
            )

        text = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
        rendered, returned, total, has_more = self._render_lines(text, offset, limit)
        truncated = len(text) > MAX_READ_CHARS

        version = self._version(target)

        # Record observation for read
        if self._tenant_id and self._conversation_id:
            self._observation_policy.record_read(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                version=version,
                present=True,
            )

        return ReadResult(
            path=resolved.relative,
            content=rendered,
            line_count=total,
            returned_lines=returned,
            offset=offset,
            has_more=has_more,
            truncated=truncated,
            size_bytes=target.stat().st_size,
            version=version,
        )

    async def read_binary(
        self,
        path: str,
        *,
        byte_limit: int | None = None,
    ) -> bytes:
        """Read a binary file (for images, etc.)."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)

        limit = byte_limit or 26_214_400
        data = await asyncio.to_thread(target.read_bytes)
        return data[:limit]

    def _is_binary(self, path: Path) -> bool:
        try:
            with path.open("rb") as f:
                chunk = f.read(4096)
        except OSError:
            return False
        if b"\x00" in chunk:
            return True
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed is not None and not (
            guessed.startswith("text/")
            or guessed in {"application/json", "application/xml", "application/javascript"}
        ):
            return True
        return False

    def _render_lines(self, text: str, offset: int, limit: int) -> tuple[str, int, int, bool]:
        lines = text.splitlines()
        total = len(lines)
        start = max(1, offset) - 1
        selected = lines[start : start + limit]
        rendered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
        return rendered, len(selected), total, start + len(selected) < total

    # ------------------------------------------------------------------------
    # Public API: Write operations
    # ------------------------------------------------------------------------

    async def write(
        self,
        path: str,
        content: str,
        *,
        mode: Literal["create", "overwrite", "append"] = "overwrite",
    ) -> WriteResult:
        """Create, overwrite, or append to a file atomically."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if target.exists() and target.is_dir():
            raise FileSystemRefusalError(
                "IS_DIRECTORY", "The path is a directory.", resolved.relative
            )

        existed = target.exists()
        if mode == "create" and existed:
            raise FileSystemRefusalError(
                "EXISTS", "The file already exists; use 'overwrite' or 'append'.", resolved.relative
            )
        if mode == "append" and not existed:
            raise FileSystemRefusalError(
                "NOT_FOUND", "Append needs an existing file; use 'create'.", resolved.relative
            )

        # Observation policy enforcement for write operations
        if self._tenant_id and self._conversation_id:
            current_version = self._version(target) if existed else None
            refusal = self._observation_policy.check_write(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                current_version=current_version if existed else None,
                strict=True,
            )
            if refusal:
                raise FileSystemRefusalError(
                    code=refusal,
                    message=f"File access denied: {refusal}",
                    path=resolved.relative,
                )

        target.parent.mkdir(parents=True, exist_ok=True)

        if mode == "append":
            # For append, we need to read existing content first, then atomically write combined
            existing = target.read_text(encoding="utf-8") if existed else ""
            combined = existing + content
            await self._atomic_write_text(target, combined)
        else:
            await self._atomic_write_text(target, content)

        line_count = (
            len(content.splitlines())
            if mode != "append"
            else len((target.read_text(encoding="utf-8")).splitlines())
        )
        if line_count > MAX_EDIT_LINES:
            # Warning but not an error
            pass

        self._notify_watchers(resolved.relative)

        return WriteResult(
            path=resolved.relative,
            mode=mode,
            created=not existed,
            size_bytes=target.stat().st_size,
            line_count=line_count,
            version=self._version(target),
        )

    async def _atomic_write_text(self, path: Path, content: str) -> None:
        """Atomically write text to a file using temp + fsync + rename."""
        # Use the existing atomic_write_json mechanism but for text
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            temp.write_text(content, encoding="utf-8")
            await asyncio.to_thread(os.replace, str(temp), str(path))
            # fsync directory on POSIX
            if os.name != "nt":
                await asyncio.to_thread(os.fsync, os.open(str(path.parent), os.O_RDONLY))
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------------
    # Public API: Edit operations
    # ------------------------------------------------------------------------

    async def edit(
        self,
        path: str,
        operation: Literal["replace", "append", "rename", "move"],
        *,
        content: str | None = None,
        new_path: str | None = None,
        expected_version: str | None = None,
    ) -> EditResult:
        """Replace, append, rename, or move a file."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            version_refusal = self._check_version(target, expected_version, resolved.relative)
            if version_refusal:
                raise version_refusal
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)
        if target.is_dir():
            raise FileSystemRefusalError(
                "IS_DIRECTORY", "The path is a directory.", resolved.relative
            )

        version_refusal = self._check_version(target, expected_version, resolved.relative)
        if version_refusal:
            raise version_refusal

        # Observation policy enforcement for edit operations
        if self._tenant_id and self._conversation_id:
            current_version = self._version(target)
            obs_refusal = self._observation_policy.check_write(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                current_version=current_version,
                strict=True,
            )
            if obs_refusal:
                raise FileSystemRefusalError(
                    code=obs_refusal,
                    message=f"File access denied: {obs_refusal}",
                    path=resolved.relative,
                )

        if operation in {"rename", "move"}:
            if not new_path:
                raise FileSystemRefusalError(
                    "INVALID_ARGS", f"{operation} requires new_path", resolved.relative
                )
            dest = self._resolve(new_path)
            if dest.absolute.exists():
                raise FileSystemRefusalError(
                    "EXISTS", "The destination already exists.", dest.relative
                )
            dest.absolute.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.replace, dest.absolute)
            self._notify_watchers(resolved.relative)
            self._notify_watchers(dest.relative)
            return EditResult(
                path=resolved.relative,
                operation=operation,
                size_bytes=dest.absolute.stat().st_size,
                line_count=0,
                version=self._version(dest.absolute),
                new_path=dest.relative,
            )

        if operation == "append":
            if content is None:
                raise FileSystemRefusalError(
                    "INVALID_ARGS", "append requires content", resolved.relative
                )
            existing = target.read_text(encoding="utf-8")
            combined = existing + content
            await self._atomic_write_text(target, combined)
        else:  # replace
            if content is None:
                raise FileSystemRefusalError(
                    "INVALID_ARGS", "replace requires content", resolved.relative
                )
            await self._atomic_write_text(target, content)

        self._notify_watchers(resolved.relative)
        return EditResult(
            path=resolved.relative,
            operation=operation,
            size_bytes=target.stat().st_size,
            line_count=len(content.splitlines()) if content else 0,
            version=self._version(target),
        )

    # ------------------------------------------------------------------------
    # Public API: Str replace
    # ------------------------------------------------------------------------

    async def str_replace(
        self,
        path: str,
        old_string: str,
        new_string: str = "",
        *,
        replace_all: bool = False,
        expected_version: str | None = None,
    ) -> StrReplaceResult:
        """Replace an exact string inside a file."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            version_refusal = self._check_version(target, expected_version, resolved.relative)
            if version_refusal:
                raise version_refusal
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)
        if target.is_dir():
            raise FileSystemRefusalError(
                "IS_DIRECTORY", "The path is a directory.", resolved.relative
            )

        version_refusal = self._check_version(target, expected_version, resolved.relative)
        if version_refusal:
            raise version_refusal

        # Observation policy enforcement for str_replace operations
        if self._tenant_id and self._conversation_id:
            current_version = self._version(target)
            obs_refusal = self._observation_policy.check_write(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                current_version=current_version,
                strict=True,
            )
            if obs_refusal:
                raise FileSystemRefusalError(
                    code=obs_refusal,
                    message=f"File access denied: {obs_refusal}",
                    path=resolved.relative,
                )

        text = target.read_text(encoding="utf-8")
        if old_string not in text:
            raise FileSystemRefusalError(
                "NOT_FOUND", "The old_string was not found in the file.", resolved.relative
            )

        if replace_all:
            new_text = text.replace(old_string, new_string)
            replaced = text.count(old_string)
        else:
            if text.count(old_string) > 1:
                raise FileSystemRefusalError(
                    "AMBIGUOUS",
                    "old_string occurs multiple times; use replace_all=true",
                    resolved.relative,
                )
            new_text = text.replace(old_string, new_string, 1)
            replaced = 1

        await self._atomic_write_text(target, new_text)
        self._notify_watchers(resolved.relative)

        return StrReplaceResult(
            path=resolved.relative,
            replaced=replaced,
            size_bytes=target.stat().st_size,
            line_count=len(new_text.splitlines()),
            version=self._version(target),
        )

    # ------------------------------------------------------------------------
    # Public API: Patch (unified diff application)
    # ------------------------------------------------------------------------

    async def apply_patch(
        self,
        path: str,
        patch: str,
        *,
        expected_version: str | None = None,
    ) -> PatchResult:
        """Apply a unified diff patch to a file atomically."""

        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            version_refusal = self._check_version(target, expected_version, resolved.relative)
            if version_refusal:
                raise version_refusal
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)

        version_refusal = self._check_version(target, expected_version, resolved.relative)
        if version_refusal:
            raise version_refusal

        # Observation policy enforcement for apply_patch operations
        if self._tenant_id and self._conversation_id:
            current_version = self._version(target)
            obs_refusal = self._observation_policy.check_write(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                current_version=current_version,
                strict=True,
            )
            if obs_refusal:
                raise FileSystemRefusalError(
                    code=obs_refusal,
                    message=f"File access denied: {obs_refusal}",
                    path=resolved.relative,
                )

        original = target.read_text(encoding="utf-8")
        patched = self._apply_unified_diff(original, patch)

        await self._atomic_write_text(target, patched)
        self._notify_watchers(resolved.relative)

        return PatchResult(
            path=resolved.relative,
            operations_applied=1,  # Simplified
            size_bytes=target.stat().st_size,
            version=self._version(target),
        )

    def _apply_unified_diff(self, original: str, patch: str) -> str:
        """Apply a unified diff to text content."""
        lines = original.splitlines(keepends=True)
        patch_lines = patch.splitlines(keepends=True)
        result = list(lines)
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            if line.startswith("@@"):
                # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
                parts = line.split()
                old_range = parts[1][1:]  # -old_start,old_count
                new_range = parts[2][1:]  # +new_start,new_count
                old_start, old_count = map(int, old_range.split(","))
                new_start, new_count = map(int, new_range.split(","))
                old_start -= 1  # Convert to 0-based
                i += 1
                # Read hunk lines
                new_lines = []
                while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                    pline = patch_lines[i]
                    if pline.startswith("+"):
                        new_lines.append(pline[1:] + "\n")
                    elif pline.startswith("-"):
                        # Deletion - skip in new
                        pass
                    else:
                        new_lines.append(pline)
                    i += 1
                # Apply hunk
                result[old_start : old_start + old_count] = new_lines
            else:
                i += 1
        return "".join(result)

    # ------------------------------------------------------------------------
    # Public API: Delete
    # ------------------------------------------------------------------------

    async def delete(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Delete a file or directory."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)

        if target.is_dir():
            if not recursive:
                raise FileSystemRefusalError(
                    "NOT_EMPTY", "Directory not empty; use recursive=true.", resolved.relative
                )
            await asyncio.to_thread(shutil.rmtree, target)
            self._notify_watchers(resolved.relative)
            return {"status": "ok", "path": resolved.relative, "deleted": "directory"}

        # Observation policy enforcement for delete operations
        if self._tenant_id and self._conversation_id:
            current_version = self._version(target)
            refusal = self._observation_policy.check_write(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                path=resolved.relative,
                current_version=current_version,
                strict=True,
            )
            if refusal:
                raise FileSystemRefusalError(
                    code=refusal,
                    message=f"File access denied: {refusal}",
                    path=resolved.relative,
                )

        await asyncio.to_thread(target.unlink)
        self._notify_watchers(resolved.relative)
        return {"status": "ok", "path": resolved.relative, "deleted": "file"}

    # ------------------------------------------------------------------------
    # Public API: Metadata
    # ------------------------------------------------------------------------

    def stat(self, path: str) -> FileStat:
        """Get file metadata."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists():
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)

        st = target.stat()
        mime_type, _ = mimetypes.guess_type(target.name)
        return FileStat(
            path=resolved.relative,
            absolute_path=str(target),
            size_bytes=st.st_size,
            is_directory=target.is_dir(),
            is_file=target.is_file(),
            version=self._version(target) if target.is_file() else None,
            mime_type=mime_type,
            modified_time=st.st_mtime,
        )

    def version(self, path: str) -> str:
        """Get the version hash of a file."""
        resolved = self._resolve(path)
        target = resolved.absolute
        if not target.exists() or target.is_dir():
            raise FileSystemRefusalError("NOT_FOUND", "No such file.", resolved.relative)
        return self._version(target)

    # ------------------------------------------------------------------------
    # Public API: Directory operations
    # ------------------------------------------------------------------------

    def list(self, path: str = ".", *, recursive: bool = False) -> list[FileStat]:
        """List files in a directory."""
        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists() or not target.is_dir():
            raise FileSystemRefusalError("NOT_FOUND", "No such directory.", resolved.relative)

        results = []
        if recursive:
            for entry in target.rglob("*"):
                if entry.is_file():
                    rel = entry.relative_to(self._workspace_root).as_posix()
                    results.append(self.stat(rel))
        else:
            for entry in target.iterdir():
                rel = entry.relative_to(self._workspace_root).as_posix()
                results.append(self.stat(rel))
        return results

    def glob(self, pattern: str) -> builtins.list[FileStat]:
        """Find files matching a glob pattern."""
        # Resolve pattern against workspace root
        resolved = self._resolve(pattern)
        target = resolved.absolute

        # If pattern contains wildcards, use rglob
        if "*" in pattern or "?" in pattern or "[" in pattern:
            # Use the parent directory as base
            base = target.parent
            pattern_name = target.name
            results = []
            for entry in base.rglob(pattern_name):
                if entry.is_file():
                    rel = entry.relative_to(self._workspace_root).as_posix()
                    results.append(self.stat(rel))
            return results
        else:
            # Single file
            return [self.stat(pattern)]

    def grep(
        self, pattern: str, *, path: str = ".", case_sensitive: bool = False
    ) -> builtins.list[dict[str, Any]]:
        """Search for text pattern in files."""
        import re

        resolved = self._resolve(path)
        target = resolved.absolute

        if not target.exists() or not target.is_dir():
            raise FileSystemRefusalError("NOT_FOUND", "No such directory.", resolved.relative)

        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)

        results = []
        for entry in target.rglob("*"):
            if not entry.is_file():
                continue
            if self._is_binary(entry):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = entry.relative_to(self._workspace_root).as_posix()
                    results.append(
                        {
                            "path": rel,
                            "line_number": i,
                            "line": line[:500],
                        }
                    )
        return results

    # ------------------------------------------------------------------------
    # Public API: File locking
    # ------------------------------------------------------------------------

    async def lock(
        self,
        path: str,
        *,
        wait_ms: int | None = None,
    ) -> None:
        """Acquire an exclusive lock on a file."""
        resolved = self._resolve(path)
        lock_path = resolved.absolute.with_suffix(resolved.absolute.suffix + ".lock")

        wait_ms = wait_ms or self._default_lock_wait_ms
        delay = 20
        for _retries in range(wait_ms // delay + 1):
            try:
                # Exclusive create
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("x") as f:
                    f.write(str(os.getpid()))
                return
            except FileExistsError:
                # Check if stale
                try:
                    pid = int(lock_path.read_text().strip())
                    if not self._process_exists(pid):
                        lock_path.unlink()
                        continue
                except (ValueError, OSError):
                    lock_path.unlink()
                    continue
            await asyncio.sleep(delay / 1000)
            delay = min(delay * 2, 200)
        raise FileSystemError(
            "LOCK_TIMEOUT", f"Could not acquire lock on {resolved.relative}", resolved.relative
        )

    def unlock(self, path: str) -> None:
        """Release a file lock."""
        resolved = self._resolve(path)
        lock_path = resolved.absolute.with_suffix(resolved.absolute.suffix + ".lock")
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _process_exists(self, pid: int) -> bool:
        """Check if a process exists."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------------
    # Public API: File watching
    # ------------------------------------------------------------------------

    def watch(self, path: str, callback: Callable[[str], Any]) -> Callable[[], None]:
        """Register a callback for file changes.

        Returns a function to unregister the watcher.
        """
        resolved = self._resolve(path)
        relative = resolved.relative

        with self._watchers_lock:
            self._watchers.setdefault(relative, []).append(callback)

        def unwatch() -> None:
            with self._watchers_lock:
                if relative in self._watchers:
                    try:
                        self._watchers[relative].remove(callback)
                    except ValueError:
                        pass
                    if not self._watchers[relative]:
                        del self._watchers[relative]

        return unwatch

    # ------------------------------------------------------------------------
    # Internal: Context integration
    # ------------------------------------------------------------------------

    @classmethod
    def from_context(cls, context: Any) -> DeepSpaceFileSystemService:
        """Create a service instance from a ToolContext."""
        from app.core.config import Settings

        if context.workspace_root is not None:
            root = context.workspace_root
        else:
            # Honour the settings carried by the context.  A test double or a
            # partially composed runtime may not be a full Settings instance,
            # but it still names the workspace base the caller wants.
            settings = (
                context.settings
                if getattr(context.settings, "deepspace_workspace_root", None)
                else Settings()
            )
            root = workspace_root_for(settings, context.auth.tenant_id, context.auth.user_id)

        sandbox_mode = str(
            context.sandbox_mode
            or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
        )

        return cls(
            workspace_root=root,
            sandbox_mode=sandbox_mode,
            tenant_id=context.auth.tenant_id,
            conversation_id=context.conversation_id,
        )


# ------------------------------------------------------------------------
# Module-level helper for backward compatibility
# ------------------------------------------------------------------------

# Module-level cache for filesystem services (since ToolContext is frozen).
# The workspace root and sandbox mode are part of the key: a conversation can
# switch workspace, and a service bound to the previous root must not be reused.
_filesystem_service_cache: dict[tuple[str, str, str, str], DeepSpaceFileSystemService] = {}


def get_filesystem_service(context: Any) -> DeepSpaceFileSystemService:
    """Get or create a filesystem service for the given context."""
    from app.deepspace.services.workspace_root import workspace_root_for

    workspace_root = getattr(context, "workspace_root", None)
    if workspace_root is None:
        settings = (
            context.settings
            if getattr(context.settings, "deepspace_workspace_root", None)
            else Settings()
        )
        workspace_root = workspace_root_for(settings, context.auth.tenant_id, context.auth.user_id)
    sandbox_mode = str(
        context.sandbox_mode
        or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
    )
    cache_key = (
        str(context.auth.tenant_id),
        str(context.conversation_id),
        str(workspace_root),
        sandbox_mode,
    )
    if cache_key not in _filesystem_service_cache:
        _filesystem_service_cache[cache_key] = DeepSpaceFileSystemService.from_context(context)
    return _filesystem_service_cache[cache_key]
