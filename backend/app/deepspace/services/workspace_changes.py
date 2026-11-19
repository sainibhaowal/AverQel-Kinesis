"""Workspace changes recorder for DeepSpace.

Records workspace changes via Git snapshots and file captures.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB
DEFAULT_MAX_FILES = 500
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_OUTPUT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_DIFF_TIMEOUT_MS = 100


@dataclass(frozen=True, slots=True)
class WorkspaceChangeEntry:
    """A single file change in the workspace."""

    path: str
    status: str  # added, modified, deleted, renamed
    lines_added: int = 0
    lines_removed: int = 0
    is_binary: bool = False
    oversized: bool = False
    display_path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceChangesSummary:
    """Summary of workspace changes for one turn."""

    turn: int
    entries: list[Any]  # List of file change entries
    total_files: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0


class WorkspaceChangesRecorder:
    """Records workspace changes via Git snapshots and file captures."""

    def __init__(
        self,
        db: Session | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.config = {
            "max_files": 500,
            "max_file_bytes": 2 * 1024 * 1024,
            "timeout_ms": 30000,
            "output_max_bytes": 8 * 1024 * 1024,
            "diff_timeout_ms": 100,
        }
        if config:
            self.config.update(config)

    def _run_git(
        self, args: list[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command with timeout."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_CONFIG_COUNT": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        return subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def _get_repo_root(self, cwd: Path) -> str | None:
        """Find the Git repository root."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _is_git_repo(self, cwd: Path) -> bool:
        """Check if the directory is a Git repository."""
        return self._get_repo_root(Path(cwd)) is not None

    def _run_git_command(
        self, args: list[str], cwd: Path, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr)."""
        try:
            result = self._run_git(args, cwd, timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except Exception as e:
            return -1, "", str(e)

    def _get_git_status(self, cwd: Path) -> list[str]:
        """Get list of changed files from git status."""
        code, stdout, _ = self._run_git_command(["status", "--porcelain"], cwd)
        if code != 0:
            return []
        lines = stdout.strip().split("\n")
        return [line[3:] for line in lines if line.strip()]

    def _get_git_diff_stat(self, cwd: Path, file_path: str) -> tuple[int, int]:
        """Get line additions and deletions for a specific file."""
        code, stdout, _ = self._run_git_command(["diff", "--numstat", "--", file_path], cwd)
        if code != 0 or not stdout.strip():
            return 0, 0
        parts = stdout.strip().split("\t")
        if len(parts) >= 2:
            added = int(parts[0]) if parts[0] != "-" else 0
            removed = int(parts[1]) if parts[1] != "-" else 0
            return added, removed
        return 0, 0

    def _get_file_content(self, cwd: Path, file_path: str) -> bytes | None:
        """Get file content."""
        full_path = cwd / file_path
        if not full_path.exists():
            return None
        try:
            return full_path.read_bytes()
        except Exception:
            return None

    def _is_binary_file(self, content: bytes) -> bool:
        """Check if file content is binary."""
        if not content:
            return False
        # Check for null bytes in first 1024 bytes
        sample = content[:1024]
        return b"\x00" in sample

    async def record_turn_changes(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        turn: int,
        workspace_root: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record workspace changes for a turn.

        This should be called at the end of each turn to capture changes.
        """
        cwd = Path(workspace_root)

        # Check if we're in a git repo
        if not self._is_git_repo(cwd):
            # No git repo - just return empty changes
            return {
                "entries": [],
                "total_files": 0,
                "total_lines_added": 0,
                "total_lines_removed": 0,
            }

        # Get repo root
        repo_root = self._get_repo_root(cwd)
        if not repo_root:
            return {
                "entries": [],
                "total_files": 0,
                "total_lines_added": 0,
                "total_lines_removed": 0,
            }

        # Get changed files from git status
        changed_files = self._get_git_status(cwd)

        entries = []
        total_added = 0
        total_removed = 0

        for file_path in changed_files[:500]:  # Limit to max files
            full_path = cwd / file_path
            if not full_path.exists():
                # File was deleted
                status = "deleted"
                added, removed = 0, 0
                is_binary = False
                oversized = False
            else:
                # File exists - check if it's new or modified
                try:
                    # Check if file is tracked by git
                    code, _, _ = self._run_git_command(
                        ["ls-files", "--error-unmatch", file_path], cwd
                    )
                    if code != 0:
                        status = "added"
                    else:
                        status = "modified"

                    # Get diff stats
                    added, removed = self._get_git_diff_stat(cwd, file_path)
                    total_added += added
                    total_removed += removed

                    # Check file size and binary
                    file_size = full_path.stat().st_size
                    is_binary = False
                    try:
                        with open(full_path, "rb") as f:
                            chunk = f.read(1024)
                            if b"\x00" in chunk:
                                is_binary = True
                    except Exception:
                        is_binary = True

                    oversized = file_size > 2 * 1024 * 1024

                except Exception:
                    status = "modified"
                    added, removed = 0, 0
                    is_binary = False
                    oversized = False

            entry = {
                "path": file_path,
                "status": status,
                "lines_added": added,
                "lines_removed": removed,
                "is_binary": is_binary,
                "oversized": oversized,
                "display_path": file_path,
            }
            entries.append(entry)

        return {
            "entries": entries,
            "total_files": len(entries),
            "total_lines_added": total_added,
            "total_lines_removed": total_removed,
        }


class WorkspaceChangesService:
    """Service for recording and querying workspace changes."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.recorder = WorkspaceChangesRecorder(db)

    async def record_turn_changes(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        turn: int,
        workspace_root: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record workspace changes for a turn."""
        return await self.recorder.record_turn_changes(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn=turn,
            workspace_root=workspace_root,
            run_id=run_id,
        )

    def get_changes_summary(self, conversation_id: str, turn: int) -> dict[str, Any] | None:
        """Get the changes summary for a turn."""
        # Query the session event store for workspace/changes event
        return None

    def get_file_diff(
        self, conversation_id: str, turn: int, file_index: int
    ) -> dict[str, Any] | None:
        """Get the diff for a specific file in a turn."""
        return None


__all__ = [
    "WorkspaceChangesRecorder",
    "WorkspaceChangesService",
]
