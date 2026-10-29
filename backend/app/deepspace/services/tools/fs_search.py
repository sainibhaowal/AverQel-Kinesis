"""Search tools for the DeepSpace project workspace.

``fs_grep`` searches file contents with a regular expression and
``fs_glob`` lists paths by name pattern.  Both stay inside the per-tenant
workspace root and both cap how much they return, so a broad pattern over a
large tree cannot flood the model's context.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    boolean_property,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.workspace_root import (
    resolve_within_root,
    workspace_root_for,
)

#: The largest number of files one search may read.
MAX_SEARCH_FILES = 5_000

#: The largest number of matches one search may report.
MAX_MATCHES = 200

#: The largest single match line a search may report, in characters.
MAX_MATCH_LINE_CHARS = 500

#: Directory names a search never descends into.
SKIPPED_DIRECTORIES = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
)


class FsGrepArguments(ToolArguments):
    """Search workspace file contents with a regular expression."""

    pattern: str = Field(min_length=1, max_length=2_000)
    path: str | None = Field(default=None, max_length=1024)
    glob: str | None = Field(default=None, max_length=255)
    output_mode: Literal["content", "files_with_matches", "count"] = "content"
    case_sensitive: bool = True
    max_results: int | None = Field(default=None, ge=1, le=MAX_MATCHES)

    @model_validator(mode="after")
    def validate_pattern(self) -> FsGrepArguments:
        """Reject a pattern that is not a valid regular expression."""
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            re.compile(self.pattern, flags)
        except re.error as exc:
            raise ValueError(f"fs_grep pattern is not a valid regular expression: {exc}") from exc
        return self


class FsGlobArguments(ToolArguments):
    """List workspace paths by name pattern."""

    pattern: str = Field(min_length=1, max_length=512)
    path: str | None = Field(default=None, max_length=1024)
    max_results: int | None = Field(default=None, ge=1, le=2_000)


def _iter_files(base: Path, glob_filter: str | None) -> list[Path]:
    """Collect the searchable files under a directory.

    Args:
        base: The directory to walk.
        glob_filter: An optional filename pattern that decides which files
            participate.

    Returns:
        The matching files, in a stable order.
    """
    collected: list[Path] = []
    for candidate in sorted(base.rglob("*")):
        if len(collected) >= MAX_SEARCH_FILES:
            break
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if any(part in SKIPPED_DIRECTORIES for part in candidate.parts):
            continue
        if glob_filter and not fnmatch.fnmatch(candidate.name, glob_filter):
            continue
        collected.append(candidate)
    return collected


def _search_root(context: ToolContext, raw_path: str | None) -> tuple[Path, str]:
    """Resolve the directory a search runs in.

    The search runs in the workspace the conversation works in, so the files
    it finds are the ones the conversation's own tools read and write.

    Args:
        context: The calling request's tool context.
        raw_path: The optional caller-supplied directory.

    Returns:
        The absolute directory and its workspace-relative form.
    """
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    normalized_path = str(raw_path or "").strip().replace("\\", "/")
    if not normalized_path or normalized_path in {".", "./"}:
        return root, ""
    resolved = resolve_within_root(root, raw_path)
    return resolved.absolute, resolved.relative


async def _fs_grep(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Search workspace file contents with a regular expression."""
    base, relative_base = _search_root(context, arguments.get("path"))
    if not base.exists():
        return {
            "status": "not_found",
            "path": relative_base,
            "message": "The search directory does not exist in the project workspace.",
        }
    flags = 0 if bool(arguments.get("case_sensitive", True)) else re.IGNORECASE
    try:
        pattern = re.compile(str(arguments["pattern"]), flags)
    except re.error as exc:
        return {
            "status": "error",
            "message": f"The pattern is not a valid regular expression: {exc}",
        }
    output_mode = str(arguments.get("output_mode") or "content")
    limit = min(MAX_MATCHES, int(arguments.get("max_results") or MAX_MATCHES))
    glob_filter = arguments.get("glob")
    matches: list[dict[str, Any]] = []
    files_with_matches: list[str] = []
    counts: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for candidate in _iter_files(base, str(glob_filter) if glob_filter else None):
        scanned += 1
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_matches = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            file_matches += 1
            if output_mode == "files_with_matches":
                if candidate.name not in files_with_matches:
                    files_with_matches.append(candidate.name)
                break
            if output_mode == "count":
                continue
            if len(matches) >= limit:
                truncated = True
                break
            matches.append(
                {
                    "path": str(candidate.relative_to(base)),
                    "line": line_number,
                    "text": line[:MAX_MATCH_LINE_CHARS],
                }
            )
        if output_mode == "count":
            if file_matches:
                counts.append({"path": str(candidate.relative_to(base)), "matches": file_matches})
        if truncated and output_mode != "count":
            break
    if output_mode == "files_with_matches":
        return {
            "status": "ok",
            "output_mode": output_mode,
            "files": files_with_matches[:limit],
            "file_count": len(files_with_matches),
            "truncated": len(files_with_matches) > limit,
        }
    if output_mode == "count":
        return {
            "status": "ok",
            "output_mode": output_mode,
            "counts": counts[:limit],
            "file_count": len(counts),
            "truncated": len(counts) > limit,
        }
    return {
        "status": "ok",
        "output_mode": output_mode,
        "matches": matches,
        "match_count": len(matches),
        "scanned_files": scanned,
        "truncated": truncated,
    }


async def _fs_glob(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List workspace paths by name pattern."""
    base, relative_base = _search_root(context, arguments.get("path"))
    if not base.exists():
        return {
            "status": "not_found",
            "path": relative_base,
            "message": "The search directory does not exist in the project workspace.",
        }
    pattern = str(arguments["pattern"])
    limit = min(2_000, int(arguments.get("max_results") or 500))
    matched: list[str] = []
    for candidate in sorted(base.rglob("*")):
        if len(matched) >= limit:
            break
        if any(part in SKIPPED_DIRECTORIES for part in candidate.parts):
            continue
        relative = str(candidate.relative_to(base))
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(candidate.name, pattern):
            matched.append(relative)
            continue
        # ``**`` in a caller pattern is a path-aware wildcard, so a pattern
        # such as ``src/**\/*.ts`` has to match against the full relative
        # path rather than only the file name.
        if "**" in pattern:
            regex = re.escape(pattern).replace(r"\*\*/\*", ".*").replace(r"\*", "[^/]*")
            if re.fullmatch(regex, relative):
                matched.append(relative)
    return {
        "status": "ok",
        "pattern": pattern,
        "path": relative_base,
        "matches": matched,
        "match_count": len(matched),
        "truncated": len(matched) >= limit,
    }


FS_GREP_DESCRIPTION = (
    "Search the project workspace for a regular expression. Use output_mode "
    "'files_with_matches' to list matching files, 'count' for per-file counts, "
    "or 'content' for the matching lines."
)
FS_GLOB_DESCRIPTION = (
    "List project workspace paths by name pattern, such as '**/*.ts' or "
    "'*.md'. Use it to discover the files a task touches."
)

FS_SEARCH_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="fs_grep",
        description=FS_GREP_DESCRIPTION,
        properties={
            "pattern": string_property(
                "The regular expression to search for.", min_length=1, max_length=2_000
            ),
            "path": string_property(
                "Workspace-relative directory to search. Defaults to the workspace root.",
                min_length=1,
                max_length=1024,
            ),
            "glob": string_property(
                "Only search files whose name matches this pattern, such as '*.py'.",
                max_length=255,
            ),
            "output_mode": string_property(
                "What the search reports.",
                enum=["content", "files_with_matches", "count"],
            ),
            "case_sensitive": boolean_property("Match the pattern case-sensitively."),
            "max_results": integer_property(
                "Largest number of results to return.", minimum=1, maximum=MAX_MATCHES
            ),
        },
        required=["pattern"],
        arguments_model=FsGrepArguments,
        mode="read",
        handler=_fs_grep,
    ),
    define_native_tool(
        name="fs_glob",
        description=FS_GLOB_DESCRIPTION,
        properties={
            "pattern": string_property(
                "The name pattern to match, such as '**/*.ts'.", min_length=1, max_length=512
            ),
            "path": string_property(
                "Workspace-relative directory to list. Defaults to the workspace root.",
                min_length=1,
                max_length=1024,
            ),
            "max_results": integer_property(
                "Largest number of paths to return.", minimum=1, maximum=2_000
            ),
        },
        required=["pattern"],
        arguments_model=FsGlobArguments,
        mode="read",
        handler=_fs_glob,
    ),
]

__all__ = ["FS_SEARCH_TOOLS", "FsGlobArguments", "FsGrepArguments"]
