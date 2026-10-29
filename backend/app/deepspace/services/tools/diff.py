"""The workspace diff tool for DeepSpace.

The tool answers one question: what changed in the project workspace?  A git
repository answers it with git's own diff, which is exact and cheap.  A
workspace that is not a repository has no such history, so the tool falls
back to the newest snapshot the workspace snapshot service recorded and
diffs the two file sets.

The tool only reads.  Recording a baseline snapshot is the caller's job, so
the API route or the turn hook records one and the tool compares against
it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    WorkspaceUnavailableError,
    resolve_within_root,
    workspace_root_for,
)
from app.deepspace.services.workspace_snapshot import (
    TRUNCATION_NOTICE,
    changed_paths,
    collect_snapshot_files,
    diff_snapshots,
    snapshot_store,
    truncate_diff,
)

#: The largest diff the tool returns when the model asks for no size.
DEFAULT_MAX_DIFF_BYTES = 200_000

#: The largest diff the tool will produce, in bytes.
MAX_MAX_DIFF_BYTES = 1_000_000

#: The wall-clock budget one git command gets, in seconds.
GIT_TIMEOUT_SECONDS = 15

#: The deadline the tool declares for itself, in seconds.  The git command
#: runs under ``GIT_TIMEOUT_SECONDS``, so the call needs a little more than
#: that to report the timeout as its own result.
WORKSPACE_DIFF_DEADLINE_SECONDS = GIT_TIMEOUT_SECONDS + 5

#: The largest number of files one snapshot collection records.
MAX_SNAPSHOT_FILES = 2000


class WorkspaceDiffArguments(ToolArguments):
    """Show what changed in the project workspace."""

    path: str | None = Field(default=None, min_length=1, max_length=1024)
    max_bytes: int | None = Field(default=DEFAULT_MAX_DIFF_BYTES, ge=1, le=MAX_MAX_DIFF_BYTES)


class _GitCommandError(RuntimeError):
    """A git command could not produce a diff."""


def _root_for(context: ToolContext) -> Path:
    """Resolve the workspace root for the calling tenant and user.

    This is the resolution the file system tools use, so both tools see the
    same tree.  A deployment that configures no workspace at all falls back
    to the process working directory, which is the tree the worker runs in.

    Args:
        context: The calling request's tool context.

    Returns:
        The absolute workspace root.
    """
    try:
        if context.workspace_root is not None:
            return context.workspace_root
        return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)
    except WorkspaceUnavailableError:
        return Path.cwd().resolve()


def _max_bytes(arguments: dict[str, Any]) -> int:
    """Return the output cap the model asked for.

    Args:
        arguments: The validated tool arguments.

    Returns:
        The largest diff the result may carry, in bytes.
    """
    raw = arguments.get("max_bytes")
    if raw is None:
        return DEFAULT_MAX_DIFF_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DIFF_BYTES
    return max(1, min(MAX_MAX_DIFF_BYTES, value))


def _under(relative_path: str, prefix: str) -> bool:
    """Report whether a workspace-relative path lies under a directory.

    Args:
        relative_path: The path to test.
        prefix: The directory the caller limited the diff to.

    Returns:
        True when the path is the directory itself or lies under it.
    """
    base = prefix.rstrip("/")
    return relative_path == base or relative_path.startswith(f"{base}/")


def _git_environment(root: Path) -> dict[str, str]:
    """Return the reduced environment one git command runs with.

    The command reads the workspace, so it needs no credential from the
    worker and gets none.

    Args:
        root: The workspace root the command runs in.

    Returns:
        The environment the command sees.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME") or str(root),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


async def _run_git(root: Path, arguments: list[str]) -> str:
    """Run one git command in the workspace and return its output.

    Args:
        root: The workspace root the command runs in.
        arguments: The arguments after the program name.

    Returns:
        The command's standard output.

    Raises:
        _GitCommandError: When the command fails or does not finish in time.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_environment(root),
        )
    except OSError as exc:
        raise _GitCommandError("git is not available on this host.") from exc
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise _GitCommandError("git did not finish in time.") from exc
    if process.returncode != 0:
        raise _GitCommandError(f"git exited with status {process.returncode}.")
    return stdout.decode("utf-8", errors="replace")


def _changed_paths_from_diff(text: str) -> list[str]:
    """Return the paths a git diff touches.

    Args:
        text: The git diff text.

    Returns:
        The changed paths, in the order the diff names them.
    """
    paths: list[str] = []
    for line in text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        candidate = line[4:].strip()
        if len(candidate) >= 2 and candidate.startswith('"') and candidate.endswith('"'):
            candidate = candidate[1:-1]
        if candidate == "/dev/null":
            continue
        for prefix in ("a/", "b/"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
                break
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


async def _git_diff(root: Path, relative_path: str | None, max_bytes: int) -> dict[str, Any] | None:
    """Return the git diff for the workspace, or None when git cannot serve it.

    Args:
        root: The workspace root to read.
        relative_path: The path the caller limited the diff to, when it did.
        max_bytes: The largest diff the result may carry, in bytes.

    Returns:
        The tool result for the git diff, or None when the workspace is not
        a git repository or git failed.
    """
    if not (root / ".git").is_dir():
        return None
    pathspec = ["--", relative_path] if relative_path else ["--"]
    try:
        unstaged = await _run_git(root, ["--no-pager", "diff", "--no-color", *pathspec])
        staged = await _run_git(root, ["--no-pager", "diff", "--no-color", "--cached", *pathspec])
    except _GitCommandError:
        return None
    text = f"{unstaged}{staged}"
    bounded, truncated = truncate_diff(text, max_bytes)
    return {
        "status": "ok",
        "source": "git",
        "diff": bounded,
        "changed_files": _changed_paths_from_diff(text),
        "truncated": truncated,
    }


def _snapshot_diff(
    context: ToolContext, root: Path, relative_path: str | None, max_bytes: int
) -> dict[str, Any]:
    """Return the snapshot diff for the workspace.

    Args:
        context: The calling request's tool context.
        root: The workspace root to read.
        relative_path: The path the caller limited the diff to, when it did.
        max_bytes: The largest diff the result may carry, in bytes.

    Returns:
        The tool result for the snapshot diff.
    """
    store = snapshot_store()
    baseline = store.latest(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        root=str(root),
    )
    current = collect_snapshot_files(root, max_files=MAX_SNAPSHOT_FILES)
    if relative_path:
        current = [item for item in current if _under(item.relative_path, relative_path)]
    if baseline is None:
        listing = "\n".join(f"- {item.relative_path}" for item in current)
        text = (
            "No baseline snapshot exists for this workspace yet, so there is "
            f"nothing to compare against. The workspace holds {len(current)} "
            f"text file(s):\n{listing}\n"
        )
        bounded, truncated = truncate_diff(text, max_bytes)
        return {
            "status": "ok",
            "source": "none",
            "diff": bounded,
            "changed_files": [item.relative_path for item in current],
            "truncated": truncated,
        }
    before = list(baseline.files)
    if relative_path:
        before = [item for item in before if _under(item.relative_path, relative_path)]
    text = diff_snapshots(before, current, root=root, max_diff_bytes=max_bytes)
    return {
        "status": "ok",
        "source": "snapshot",
        "diff": text,
        "changed_files": changed_paths(before, current),
        "truncated": text.endswith(TRUNCATION_NOTICE),
    }


async def _workspace_diff(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Show what changed in the project workspace."""
    try:
        root = _root_for(context)
    except OSError as exc:
        return {
            "status": "error",
            "message": f"The project workspace could not be resolved: {exc}",
        }
    raw_path = arguments.get("path")
    relative_path: str | None = None
    if raw_path:
        try:
            relative_path = resolve_within_root(root, str(raw_path)).relative
        except (WorkspacePathError, OSError) as exc:
            return {
                "status": "error",
                "message": str(exc),
                "path": str(raw_path)[:1024],
            }
    max_bytes = _max_bytes(arguments)
    git_result = await _git_diff(root, relative_path, max_bytes)
    if git_result is not None:
        return git_result
    return _snapshot_diff(context, root, relative_path, max_bytes)


WORKSPACE_DIFF_DESCRIPTION = (
    "Show what changed in the project workspace. Returns a unified diff of the "
    "changed files together with the list of their paths. Use path to limit "
    "the diff to one file or directory."
)

DIFF_TOOLS: tuple[NativeToolSpec, ...] = (
    define_native_tool(
        name="workspace_diff",
        description=WORKSPACE_DIFF_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative file or directory to limit the diff to.",
                min_length=1,
                max_length=1024,
            ),
            "max_bytes": integer_property(
                "The largest diff to return, in bytes.",
                minimum=1,
                maximum=MAX_MAX_DIFF_BYTES,
            ),
        },
        arguments_model=WorkspaceDiffArguments,
        mode="read",
        handler=_workspace_diff,
        deadline_seconds=WORKSPACE_DIFF_DEADLINE_SECONDS,
    ),
)

__all__ = [
    "DEFAULT_MAX_DIFF_BYTES",
    "DIFF_TOOLS",
    "GIT_TIMEOUT_SECONDS",
    "MAX_MAX_DIFF_BYTES",
    "WORKSPACE_DIFF_DEADLINE_SECONDS",
    "WorkspaceDiffArguments",
]
