"""The code-metrics plugin's tool handler.

The plugin counts the files and the lines of code in the conversation's
workspace, grouped by file extension.  It reads the workspace only, and it
never reads outside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The directories the counter skips.
SKIPPED_DIRECTORIES = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
)

#: The largest file the counter reads.
MAX_FILE_BYTES = 512_000

#: The largest number of files the counter walks.
MAX_FILES = 20_000


def _is_binary(payload: bytes) -> bool:
    """Report whether a payload looks like binary content.

    Args:
        payload: The file's first bytes.

    Returns:
        True when the content is not text.
    """
    return b"\x00" in payload[:8192]


def _workspace_root(context: Any) -> Path:
    """Return the workspace the call runs in.

    Args:
        context: The calling request's tool context.

    Returns:
        The workspace root.
    """
    settings = getattr(context, "settings", None)
    auth = getattr(context, "auth", None)
    if settings is not None and auth is not None:
        from app.deepspace.services.workspace_root import workspace_root_for

        return Path(workspace_root_for(settings, auth.tenant_id, auth.user_id))
    return Path.cwd()


def _count(root: Path, target: Path) -> dict[str, Any]:
    """Count the files and the lines under one path.

    Args:
        root: The workspace root.
        target: The path to count.

    Returns:
        The counts.
    """
    files = 0
    lines = 0
    by_extension: dict[str, int] = {}
    walked = 0
    for path in target.rglob("*"):
        if walked >= MAX_FILES:
            break
        if not path.is_file():
            continue
        if any(part in SKIPPED_DIRECTORIES for part in path.parts):
            continue
        walked += 1
        try:
            payload = path.read_bytes()[:MAX_FILE_BYTES]
        except OSError:
            continue
        if _is_binary(payload):
            continue
        files += 1
        lines += payload.count(b"\n") + (1 if payload and not payload.endswith(b"\n") else 0)
        extension = path.suffix or "(none)"
        by_extension[extension] = by_extension.get(extension, 0) + 1
    return {
        "files": files,
        "lines": lines,
        "by_extension": dict(sorted(by_extension.items())),
    }


def code_metrics(arguments: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Count the files and the lines of code in the workspace.

    Args:
        arguments: The tool's arguments.
        context: The calling request's tool context.

    Returns:
        The counts.
    """
    root = _workspace_root(context)
    relative = str(arguments.get("path") or "").strip()
    target = root
    if relative:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return {"status": "error", "message": "That path is outside the workspace."}
        target = candidate
    if not target.exists():
        return {"status": "error", "message": "That path does not exist in the workspace."}
    counts = _count(root, target)
    return {
        "status": "ok",
        "path": relative or ".",
        "files": counts["files"],
        "lines": counts["lines"],
        "by_extension": counts["by_extension"],
    }


__all__ = ["code_metrics"]
