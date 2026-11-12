"""Search tools for the DeepSpace project workspace.

``fs_grep`` searches file contents with a regular expression and
``fs_glob`` lists paths by name pattern. Read-only and Workspace modes keep
both tools inside the selected workspace. Full access accepts an absolute host
path and performs a bounded user-root search for one exact filename.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from app.deepspace.services.host_access import resolve_conversation_path
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
    WorkspacePathError,
    workspace_root_for,
)

#: The largest number of files one search may read.
MAX_SEARCH_FILES = 5_000

#: The largest number of filesystem entries one search may inspect. The cap
#: applies to the walk itself, not only to the number of results returned.
MAX_SEARCH_ENTRIES = 10_000

#: The largest number of matches one search may report.
MAX_MATCHES = 200

#: The largest single match line a search may report, in characters.
MAX_MATCH_LINE_CHARS = 500

#: Directory names a search never descends into.
SKIPPED_DIRECTORIES = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
)

#: Linux trees that are virtual, process-backed, or otherwise unsafe to walk
# as part of ordinary host-file discovery. They are skipped even in Full
# access because they do not contain user documents and can reject directory
# enumeration with a permission error.
PROTECTED_HOST_SEARCH_ROOTS = tuple(
    Path(path) for path in ("/proc", "/sys", "/dev", "/run", "/boot", "/root") if os.name != "nt"
)

#: The preferred roots the model should use when a user asks for local files
# outside the conversation workspace. The workspace remains the default when
# no path is supplied, preserving normal coding-agent behavior.
PREFERRED_HOST_SEARCH_ROOTS = (
    "the conversation workspace",
    "the current user's home",
    "/mnt",
    "/media",
)

#: Keep a malformed or hostile filesystem from producing an unbounded result
# payload while still returning useful diagnostics for skipped paths.
MAX_SKIPPED_PATHS = 200


class FsGrepArguments(ToolArguments):
    """Search workspace file contents with a regular expression."""

    pattern: str = Field(min_length=1, max_length=2_000)
    path: str | None = Field(default=None, max_length=1024)
    entire_system: bool = False
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
    """List paths under the selected, authorized search root."""

    pattern: str = Field(min_length=1, max_length=512)
    path: str | None = Field(default=None, max_length=1024)
    entire_system: bool = False
    max_results: int | None = Field(default=None, ge=1, le=2_000)


@dataclass(slots=True)
class SearchTraversal:
    """Bounded counters shared by the directories in one search request."""

    entries_scanned: int = 0
    truncated: bool = False


def _record_skipped(skipped_paths: list[dict[str, str]], path: Path, reason: str) -> None:
    """Record one skipped directory without allowing diagnostics to grow unbounded."""
    if len(skipped_paths) >= MAX_SKIPPED_PATHS:
        return
    skipped_paths.append({"path": str(path), "reason": reason})


def _protected_host_root(path: Path) -> Path | None:
    """Return the protected host root containing ``path``, when there is one."""
    for protected_root in PROTECTED_HOST_SEARCH_ROOTS:
        try:
            path.relative_to(protected_root)
        except ValueError:
            continue
        return protected_root
    return None


def _directory_skip_reason(path: Path) -> str | None:
    """Explain why a directory should not be descended into."""
    protected_root = _protected_host_root(path)
    if protected_root is not None:
        return f"protected system tree: {protected_root}"
    if path.name in SKIPPED_DIRECTORIES:
        return "excluded development dependency or cache directory"
    return None


def _iter_search_paths(
    base: Path,
    skipped_paths: list[dict[str, str]],
    traversal: SearchTraversal,
) -> Iterator[Path]:
    """Stream a directory tree without failing on unreadable or protected paths.

    ``Path.rglob`` cannot prune a protected directory before it tries to read
    it. A breadth-first walk checks nearby user folders before deep caches,
    while a shared entry budget makes even a no-match search finite. Only a
    bounded number of entries are materialized for sorting at a time.
    """
    pending = deque([base])
    while pending:
        current = pending.popleft()
        skip_reason = _directory_skip_reason(current)
        if skip_reason is not None:
            _record_skipped(skipped_paths, current, skip_reason)
            continue
        try:
            with os.scandir(current) as entries:
                bounded_entries = []
                for entry in entries:
                    if traversal.entries_scanned >= MAX_SEARCH_ENTRIES:
                        traversal.truncated = True
                        break
                    traversal.entries_scanned += 1
                    bounded_entries.append(entry)
                ordered_entries = sorted(
                    bounded_entries,
                    key=lambda entry: (entry.name.startswith("."), entry.name.casefold()),
                )
        except PermissionError:
            _record_skipped(skipped_paths, current, "permission denied while reading directory")
            continue
        except OSError as exc:
            _record_skipped(skipped_paths, current, f"unreadable directory: {exc.strerror or exc}")
            continue

        child_directories: list[Path] = []
        for entry in ordered_entries:
            candidate = Path(entry.path)
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except PermissionError:
                _record_skipped(
                    skipped_paths, candidate, "permission denied while inspecting entry"
                )
                continue
            except OSError as exc:
                _record_skipped(
                    skipped_paths,
                    candidate,
                    f"unreadable entry: {exc.strerror or exc}",
                )
                continue

            if is_directory:
                skip_reason = _directory_skip_reason(candidate)
                if skip_reason is not None:
                    _record_skipped(skipped_paths, candidate, skip_reason)
                    continue
                child_directories.append(candidate)
            yield candidate

        if traversal.truncated:
            return
        pending.extend(child_directories)


def _iter_files(
    base: Path,
    glob_filter: str | None,
    skipped_paths: list[dict[str, str]],
    traversal: SearchTraversal,
) -> list[Path]:
    """Collect the searchable files under a directory.

    Args:
        base: The directory to walk.
        glob_filter: An optional filename pattern that decides which files
            participate.

    Returns:
        The matching files, in a stable order.
    """
    collected: list[Path] = []
    for candidate in _iter_search_paths(base, skipped_paths, traversal):
        if len(collected) >= MAX_SEARCH_FILES:
            traversal.truncated = True
            break
        try:
            is_file = candidate.is_file()
            is_symlink = candidate.is_symlink()
        except PermissionError:
            _record_skipped(skipped_paths, candidate, "permission denied while inspecting file")
            continue
        except OSError as exc:
            _record_skipped(
                skipped_paths,
                candidate,
                f"unreadable file: {exc.strerror or exc}",
            )
            continue
        if not is_file or is_symlink:
            continue
        if glob_filter and not fnmatch.fnmatch(candidate.name, glob_filter):
            continue
        collected.append(candidate)
    return collected


def _search_root(
    context: ToolContext, raw_path: str | None, entire_system: bool = False
) -> tuple[Path, str]:
    """Resolve the directory a search runs in under the active mode.

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
        if root.resolve(strict=False) == Path("/") and not entire_system:
            raise WorkspacePathError(
                "Scanning the entire host requires path='/' and entire_system=true."
            )
        return root, ""
    mode = str(
        context.sandbox_mode
        or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
    )
    resolved = resolve_conversation_path(root, str(raw_path), mode)
    if resolved.absolute == Path("/") and not entire_system:
        raise WorkspacePathError(
            "Scanning the entire host requires path='/' and entire_system=true."
        )
    return resolved.absolute, resolved.relative


def _path_refusal(
    context: ToolContext, raw_path: str | None, exc: WorkspacePathError
) -> dict[str, Any]:
    """Explain how to widen a search whose path leaves the workspace."""
    refusal: dict[str, Any] = {
        "status": "error",
        "message": str(exc),
        "path": str(raw_path or "")[:1_024],
        "capability_failure": True,
        "stop_turn": True,
        "no_retry": True,
        "retryable": False,
        "capability": {
            "available": False,
            "reason": str(exc),
            "next_action": (
                "Report the access limitation. Do not retry the same path with bash, find, "
                "grep, sandbox_execute, or browser navigation."
            ),
        },
    }
    effective_mode = str(
        context.sandbox_mode
        or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
    )
    if effective_mode != "danger-full-access":
        refusal.update(
            {
                "error_code": "SANDBOX_MODE_REQUIRED",
                "required_mode": "danger-full-access",
                "instruction": (
                    "This search path is outside the conversation workspace. Ask the user to "
                    "switch to Full access before retrying."
                ),
            }
        )
    return refusal


def _is_exact_filename(pattern: str) -> bool:
    """Return whether a glob pattern names one basename rather than a pattern."""
    return (
        bool(pattern)
        and "/" not in pattern
        and "\\" not in pattern
        and not any(token in pattern for token in ("*", "?", "["))
    )


def _exact_name_search_roots(context: ToolContext) -> list[Path]:
    """Choose safe user-file roots for one exact-name Full-access search."""
    workspace = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    workspace = workspace.expanduser().resolve(strict=False)
    home = Path.home().expanduser().resolve(strict=False)
    try:
        workspace_is_in_home = workspace.is_relative_to(home)
    except AttributeError:  # pragma: no cover - Python 3.8 compatibility
        workspace_is_in_home = workspace == home or home in workspace.parents

    candidates = [home, workspace] if workspace_is_in_home else [workspace, home]
    if os.name != "nt":
        candidates.extend((Path("/mnt"), Path("/media")))

    roots: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        if any(resolved == root or root in resolved.parents for root in roots):
            continue
        roots.append(resolved)
    return roots


def _fs_grep_sync(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Search workspace file contents with a regular expression."""
    try:
        base, relative_base = _search_root(
            context, arguments.get("path"), bool(arguments.get("entire_system", False))
        )
    except WorkspacePathError as exc:
        return _path_refusal(context, arguments.get("path"), exc)
    if not base.exists():
        return {
            "status": "not_found",
            "path": relative_base,
            "search_root": str(base),
            "message": "The search directory does not exist.",
            "skipped_paths": [],
        }
    flags = 0 if bool(arguments.get("case_sensitive", True)) else re.IGNORECASE
    try:
        compiled_pattern = re.compile(str(arguments["pattern"]), flags)
    except re.error as exc:
        return {
            "status": "error",
            "search_root": str(base),
            "message": f"The pattern is not a valid regular expression: {exc}",
        }
    output_mode = str(arguments.get("output_mode") or "content")
    limit = min(MAX_MATCHES, int(arguments.get("max_results") or MAX_MATCHES))
    glob_filter = arguments.get("glob")
    skipped_paths: list[dict[str, str]] = []
    traversal = SearchTraversal()
    matches: list[dict[str, Any]] = []
    files_with_matches: list[str] = []
    counts: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for candidate in _iter_files(
        base, str(glob_filter) if glob_filter else None, skipped_paths, traversal
    ):
        scanned += 1
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _record_skipped(
                skipped_paths,
                candidate,
                f"unreadable file: {exc.strerror or exc}",
            )
            continue
        file_matches = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not compiled_pattern.search(line):
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
            "search_root": str(base),
            "files": files_with_matches[:limit],
            "file_count": len(files_with_matches),
            "truncated": len(files_with_matches) > limit,
            "scanned_entries": traversal.entries_scanned,
            "search_truncated": traversal.truncated,
            "skipped_paths": skipped_paths,
        }
    if output_mode == "count":
        return {
            "status": "ok",
            "output_mode": output_mode,
            "search_root": str(base),
            "counts": counts[:limit],
            "file_count": len(counts),
            "truncated": len(counts) > limit,
            "scanned_entries": traversal.entries_scanned,
            "search_truncated": traversal.truncated,
            "skipped_paths": skipped_paths,
        }
    return {
        "status": "ok",
        "output_mode": output_mode,
        "search_root": str(base),
        "matches": matches,
        "match_count": len(matches),
        "scanned_files": scanned,
        "truncated": truncated,
        "scanned_entries": traversal.entries_scanned,
        "search_truncated": traversal.truncated,
        "skipped_paths": skipped_paths,
    }


async def _fs_grep(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a bounded content search away from the API event loop."""
    return await asyncio.to_thread(_fs_grep_sync, arguments, context)


def _fs_glob_sync(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List paths under the selected, authorized search root."""
    try:
        base, relative_base = _search_root(
            context, arguments.get("path"), bool(arguments.get("entire_system", False))
        )
    except WorkspacePathError as exc:
        return _path_refusal(context, arguments.get("path"), exc)
    pattern = str(arguments["pattern"])
    auto_search = (
        not str(arguments.get("path") or "").strip()
        and _is_exact_filename(pattern)
        and str(
            context.sandbox_mode
            or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
        )
        == "danger-full-access"
    )
    if not base.exists() and not auto_search:
        result: dict[str, Any] = {
            "status": "not_found",
            "path": relative_base,
            "search_root": str(base),
            "message": "The search directory does not exist.",
            "skipped_paths": [],
        }
        if _is_exact_filename(pattern):
            result.update(
                {
                    "capability_failure": True,
                    "stop_turn": True,
                    "no_retry": True,
                    "retryable": False,
                    "search_complete": False,
                    "capability": {
                        "available": False,
                        "reason": "The authorized workspace root does not exist.",
                        "requested_filename": pattern,
                        "searched_roots": [],
                        "search_complete": False,
                        "next_action": (
                            "Report that the authorized workspace root is unavailable. Do not "
                            "retry with another tool."
                        ),
                    },
                }
            )
        return result
    limit = min(2_000, int(arguments.get("max_results") or 500))
    skipped_paths: list[dict[str, str]] = []
    matched: list[str] = []
    traversal = SearchTraversal()
    roots = _exact_name_search_roots(context) if auto_search else [base]
    searched_roots: list[Path] = []
    result_truncated = False
    for search_root in roots:
        if not search_root.exists():
            _record_skipped(skipped_paths, search_root, "search root does not exist")
            continue
        searched_roots.append(search_root)
        for candidate in _iter_search_paths(search_root, skipped_paths, traversal):
            relative = str(candidate.relative_to(search_root))
            is_match = fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
                candidate.name, pattern
            )
            # ``**`` in a caller pattern is a path-aware wildcard, so a
            # pattern such as ``src/**/*.ts`` matches the relative path.
            if not is_match and "**" in pattern:
                regex = re.escape(pattern).replace(r"\*\*/\*", ".*").replace(r"\*", "[^/]*")
                is_match = re.fullmatch(regex, relative) is not None
            if not is_match:
                continue
            matched.append(str(candidate.resolve(strict=False)) if auto_search else relative)
            if len(matched) > limit:
                result_truncated = True
                break
            # Exact-name host search stops at its first result. It is a
            # lookup, not a request to enumerate every copy on the machine.
            if auto_search and _is_exact_filename(pattern):
                break
        if traversal.truncated or result_truncated or (auto_search and matched):
            break
    result: dict[str, Any] = {
        "status": "ok",
        "pattern": pattern,
        "path": relative_base,
        "search_root": str(searched_roots[0] if searched_roots else base),
        "search_roots": [str(root) for root in searched_roots],
        "matches": matched[:limit],
        "match_count": min(len(matched), limit),
        "scanned_entries": traversal.entries_scanned,
        "search_truncated": traversal.truncated,
        "truncated": result_truncated or traversal.truncated,
        "skipped_paths": skipped_paths,
    }
    if _is_exact_filename(pattern) and not matched:
        search_was_limited = traversal.truncated or result_truncated
        skipped = bool(skipped_paths)
        search_complete = not search_was_limited and not skipped
        if search_was_limited:
            reason = (
                "The exact-name search was bounded before it could prove that the file is absent."
            )
        elif skipped:
            reason = (
                "The exact-name search could not inspect every candidate because some paths were "
                "skipped or denied."
            )
        elif auto_search:
            reason = "No exact match was found in the authorized user roots that were searched."
        else:
            reason = "No exact match was found in the authorized conversation workspace."
        result.update(
            {
                "search_complete": search_complete,
                "capability_failure": True,
                "stop_turn": True,
                "no_retry": True,
                "retryable": False,
                "message": (
                    f"{reason} Report the searched roots and skipped paths to the user. "
                    "Do not retry with bash, find, grep, sandbox_execute, browser navigation, "
                    "or another exact-name search."
                ),
                "capability": {
                    "available": False,
                    "reason": reason,
                    "requested_filename": pattern,
                    "searched_roots": result["search_roots"],
                    "search_complete": search_complete,
                    "next_action": (
                        "Report the exact search evidence. If the file is outside the authorized "
                        "roots, ask the user to import it or switch to Full access."
                    ),
                },
            }
        )
    elif traversal.truncated or result_truncated:
        result.update(
            {
                "search_complete": False,
                "capability_failure": True,
                "stop_turn": True,
                "no_retry": True,
                "retryable": False,
                "message": (
                    "The filesystem search reached its bounded traversal limit. The result is "
                    "partial; report scanned_entries and skipped_paths instead of repeating it "
                    "through another tool."
                ),
                "capability": {
                    "available": False,
                    "reason": "bounded_search_limit",
                    "search_complete": False,
                    "next_action": "Report the partial result; do not retry the same search.",
                },
            }
        )
    return result


async def _fs_glob(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a bounded path search away from the API event loop."""
    return await asyncio.to_thread(_fs_glob_sync, arguments, context)


FS_GREP_DESCRIPTION = (
    "Search the project workspace for a regular expression. In Full access, an explicit absolute "
    "host path is also allowed. For user-file discovery outside the workspace, use the current "
    "user's home, /mnt, or /media; do not use / unless the user explicitly requests an entire "
    "system search with entire_system=true. Protected virtual/system trees are skipped and "
    "reported. Each search inspects at most 10,000 filesystem entries and returns "
    "scanned_entries and search_truncated; do not repeat a partial search. Use output_mode "
    "'files_with_matches' to list matching files, 'count' for per-file counts, "
    "or 'content' for the matching lines."
)
FS_GLOB_DESCRIPTION = (
    "List project workspace paths by name pattern, such as '**/*.ts' or "
    "'*.md'. In Full access, an explicit absolute host path is also allowed. For user-file "
    "discovery outside the workspace, use the current user's home, /mnt, or /media; do not use / "
    "unless the user explicitly requests an entire system search with entire_system=true. "
    "For one exact filename in Full access, omit path: the tool searches the workspace and "
    "current user's home, then /mnt and /media as budget permits, stopping at the first match. "
    "Every search inspects at most 10,000 filesystem entries and returns scanned_entries and "
    "search_truncated; do not repeat a search when it reports truncation. "
    "Protected virtual/system trees are skipped and reported. Use it to discover the files a "
    "task touches."
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
                "Workspace-relative directory or explicit absolute host path in Full access. "
                "Defaults to the workspace root; for user-file discovery use an explicit current "
                "user home, /mnt, or /media path, not / unless an entire-system search is requested.",
                min_length=1,
                max_length=1024,
            ),
            "entire_system": boolean_property(
                "Set true only when the user explicitly requests scanning the entire host at '/'."
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
                "Workspace-relative directory or explicit absolute host path in Full access. "
                "Wildcard searches default to the workspace; for one exact filename in Full "
                "access, omit path to search the workspace and standard user roots. Never use / "
                "unless the user requests an entire-system search.",
                min_length=1,
                max_length=1024,
            ),
            "entire_system": boolean_property(
                "Set true only when the user explicitly requests scanning the entire host at '/'."
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
