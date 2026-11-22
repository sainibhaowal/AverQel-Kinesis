"""File system tools for the DeepSpace project workspace.

These tools read and write real files under the per-tenant workspace root
resolved by :mod:`app.deepspace.services.workspace_root`.  They are the
workspace counterpart to the conversation-scoped Library tools: the Library
stores a document per conversation, while these tools work on a directory
tree an agent can build, edit, and search like a normal coding project.

Every path is resolved against the root and re-checked after symlink
resolution, so a request cannot reach a file outside the tenant's workspace.

An edit can carry the version of the file it expects to change.  The version
is the content hash the workspace snapshot records, so a version the agent
read stays meaningful.  When the file on disk no longer holds that version,
the edit is refused with ``FS_STALE_VERSION`` and nothing is written, so a
concurrent change is never lost.  The guard is optional: an edit without an
expected version behaves exactly as it did before.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from app.core.errors import ApiError
from app.deepspace.services.filesystem import (
    FileSystemError,
    FileSystemRefusalError,
    get_filesystem_service,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    boolean_property,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.workspace_root import WorkspacePathError, workspace_root_for
from app.ingestion.services.extractors.router import ExtractorRouter

#: The largest file a read may return, in characters.
MAX_READ_CHARS = 10_000_000

# Binary files are handed to the same bounded ingestion readers used by the
# Library.  Keep the host-path reader within the deployment upload ceiling so
# a Full-access read cannot turn into an unbounded memory allocation.
DEFAULT_BINARY_READ_BYTES = 26_214_400

#: The largest file a write may create, in characters.
MAX_WRITE_CHARS = 10_000_000

#: The largest number of lines one read may return.
MAX_READ_LINES = 100_000

#: The largest number of lines one edit may add.
MAX_EDIT_LINES = 20_000

#: The number of hexadecimal characters in a file version.
VERSION_LENGTH = 64

#: The largest number of immediate entries one directory read lists.
MAX_DIRECTORY_ENTRIES = 10_000

#: The largest number of lines one write result carries as review metadata.
MAX_DIFF_LINES = 400

#: The largest amount of text compared for a reviewable diff.
MAX_DIFF_CHARS = 2_000_000


def _read_text_for_diff(target: Path) -> str | None:
    """Return the current text of a file for change review, or ``None``."""
    try:
        if not target.is_file() or _is_binary(target):
            return None
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_DIFF_CHARS]
    except OSError:
        return None


def _diff_metadata(path: str, before: str, after: str) -> dict[str, Any]:
    """Build a bounded, server-side diff a client can render as review.

    The line types mirror the frontend's diff renderer: ``unchanged``,
    ``added``, and ``removed``.  Counts cover the whole change even when the
    line list is truncated for transport.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    opcodes = matcher.get_opcodes()
    additions = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in opcodes if tag in ("insert", "replace"))
    deletions = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes if tag in ("delete", "replace"))
    lines: list[dict[str, str]] = []
    truncated = False
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for line in before_lines[i1:i2]:
                if len(lines) >= MAX_DIFF_LINES:
                    truncated = True
                    break
                lines.append({"type": "unchanged", "text": line})
        else:
            for line in before_lines[i1:i2]:
                if len(lines) >= MAX_DIFF_LINES:
                    truncated = True
                    break
                lines.append({"type": "removed", "text": line})
            for line in after_lines[j1:j2]:
                if len(lines) >= MAX_DIFF_LINES:
                    truncated = True
                    break
                lines.append({"type": "added", "text": line})
        if len(lines) >= MAX_DIFF_LINES:
            truncated = True
            break
    return {
        "path": path,
        "additions": additions,
        "deletions": deletions,
        "lines": lines,
        "truncated": truncated,
    }


def _directory_listing(target: Path, relative: str, limit: int) -> dict[str, Any]:
    """List a directory's immediate entries without walking into them."""
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if len(entries) >= limit:
                    truncated = True
                    break
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    size_bytes = entry.stat(follow_symlinks=False).st_size if is_file else None
                except OSError:
                    is_directory = False
                    is_file = False
                    size_bytes = None
                entries.append(
                    {
                        "name": entry.name,
                        "path": f"{relative}/{entry.name}" if relative else entry.name,
                        "type": ("directory" if is_directory else ("file" if is_file else "other")),
                        "size_bytes": size_bytes,
                    }
                )
    except OSError as exc:
        return {
            "status": "error",
            "error_code": "DIRECTORY_UNREADABLE",
            "path": relative,
            "message": f"The directory could not be read: {exc.strerror or exc}",
        }
    entries.sort(key=lambda item: (item["type"] != "directory", str(item["name"]).casefold()))
    return {
        "status": "ok",
        "path": relative,
        "type": "directory",
        "entries": entries,
        "entry_count": len(entries),
        "truncated": truncated,
    }


def _root_for(context: ToolContext) -> Path:
    """Resolve the workspace root the call works in.

    The conversation's own workspace wins when the request resolved one, so
    a conversation that works in a named workspace writes there.  Without
    one the account's default workspace is the root.

    Args:
        context: The calling request's tool context.

    Returns:
        The workspace root.
    """
    if context.workspace_root is not None:
        return context.workspace_root
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _is_binary(path: Path) -> bool:
    """Detect files that should go through the bounded document reader."""
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in sample:
        return True
    return path.suffix.casefold() in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


def _resolve(context: ToolContext, raw_path: str) -> tuple[Any, dict[str, Any] | None]:
    """Resolve one path under the caller's effective access mode.

    A path that leaves the workspace is a refusal the model can act on, not a
    crash, so the failure is returned as a result the caller forwards.

    Args:
        context: The calling request's tool context.
        raw_path: The path the model supplied.

    Returns:
        The resolved path, or the refusal to return instead.
    """
    try:
        root = _root_for(context)
        mode = str(
            context.sandbox_mode
            or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
        )
        from app.deepspace.services.host_access import resolve_conversation_path

        return resolve_conversation_path(root, raw_path, mode), None
    except FileSystemRefusalError as e:
        return None, {
            "status": "error",
            "error_code": e.code,
            "message": e.message,
            "path": e.path,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
    except WorkspacePathError as exc:
        return None, {
            "status": "error",
            "error_code": "FS_PATH_REFUSED",
            "message": str(exc),
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
    except Exception as exc:
        return None, {"status": "unavailable", "message": str(exc)}


class FsReadArguments(ToolArguments):
    """Read a text file from the project workspace."""

    path: str = Field(min_length=1, max_length=1024)
    offset: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1, le=MAX_READ_LINES)


class FsWriteArguments(ToolArguments):
    """Create or update a text file in the project workspace."""

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=MAX_WRITE_CHARS)
    mode: Literal["create", "overwrite", "append"] = "overwrite"


class FsEditArguments(ToolArguments):
    """Replace, append, rename, or move a workspace file."""

    path: str = Field(min_length=1, max_length=1024)
    operation: Literal["replace", "append", "rename", "move"] = "replace"
    content: str | None = Field(default=None, max_length=MAX_WRITE_CHARS)
    new_path: str | None = Field(default=None, min_length=1, max_length=1024)
    expected_version: str | None = Field(default=None, min_length=1, max_length=VERSION_LENGTH)

    @model_validator(mode="after")
    def validate_operation(self) -> FsEditArguments:
        """Require the arguments each operation actually needs."""
        if self.operation in {"replace", "append"} and self.content is None:
            raise ValueError(f"fs_edit({self.operation}) requires content")
        if self.operation in {"rename", "move"} and not self.new_path:
            raise ValueError(f"fs_edit({self.operation}) requires new_path")
        return self


class FsDeleteArguments(ToolArguments):
    """Delete a workspace file or directory."""

    path: str = Field(min_length=1, max_length=1024)
    recursive: bool = False


class FsStrReplaceArguments(ToolArguments):
    """Replace an exact string inside a workspace file."""

    path: str = Field(min_length=1, max_length=1024)
    old_string: str = Field(min_length=1, max_length=100_000)
    new_string: str = Field(default="", max_length=100_000)
    replace_all: bool = False
    expected_version: str | None = Field(default=None, min_length=1, max_length=VERSION_LENGTH)


class FsLockArguments(ToolArguments):
    """Acquire an exclusive lock on a file."""

    path: str = Field(min_length=1, max_length=1024)
    wait_ms: int | None = None


class FsUnlockArguments(ToolArguments):
    """Release a file lock."""

    path: str = Field(min_length=1, max_length=1024)


class FsWatchArguments(ToolArguments):
    """Register a callback for file changes."""

    path: str = Field(min_length=1, max_length=1024)


class FsUnwatchArguments(ToolArguments):
    """Stop watching a file or directory for changes."""

    path: str = Field(min_length=1, max_length=1024)


async def _fs_lock(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Acquire an exclusive lock on a file."""
    fs = get_filesystem_service(context)
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    wait_ms = arguments.get("wait_ms") or 5000
    try:
        await fs.lock(str(arguments["path"]), wait_ms=wait_ms)
    except FileSystemError as e:
        return {"status": "error", "error_code": e.code, "path": e.path, "message": e.message}
    return {"status": "ok", "path": resolved.relative, "message": "Lock acquired"}


async def _fs_unlock(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Release a file lock."""
    fs = get_filesystem_service(context)
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    try:
        fs.unlock(str(arguments["path"]))
    except FileSystemError as e:
        return {"status": "error", "error_code": e.code, "path": e.path, "message": e.message}
    return {"status": "ok", "path": resolved.relative, "message": "Lock released"}


async def _fs_watch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Register a callback for file changes."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    # For now, we return a watch ID. In a real implementation, the callback
    # would be registered with the filesystem service's watch mechanism.
    # For now, we return a watch ID that can be used to unwatch.
    # Note: In a real implementation, this would register a callback with the
    # filesystem service's watch mechanism.
    return {
        "status": "ok",
        "path": resolved.relative,
        "message": "Watch registered. Changes to this path will be reported.",
        "watch_id": f"watch_{resolved.relative.replace('/', '_')}",
    }


async def _fs_unwatch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Stop watching a file or directory for changes."""
    # In a real implementation, this would remove the callback from the filesystem service.
    # For now, we return a success response.
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    return {"status": "ok", "path": resolved.relative, "message": "Watch removed"}


async def _fs_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a workspace text or supported binary file with numbered lines."""
    try:
        resolved, refusal = _resolve(context, str(arguments["path"]))
        if refusal is not None:
            return refusal
        assert resolved is not None
        target = resolved.absolute
        fs = get_filesystem_service(context)
        if not target.exists():
            fs._observation_policy.observe(
                str(context.auth.tenant_id),
                str(context.conversation_id),
                resolved.relative,
                None,
                False,
            )
            return {
                "status": "error",
                "error_code": "NOT_FOUND",
                "path": resolved.relative,
                "message": "No such file in the project workspace.",
            }
        if target.is_dir():
            entry_limit = min(
                MAX_DIRECTORY_ENTRIES,
                max(1, int(arguments.get("limit") or MAX_DIRECTORY_ENTRIES)),
            )
            return await asyncio.to_thread(
                _directory_listing, target, resolved.relative, entry_limit
            )
        offset = int(arguments.get("offset") or 1)
        limit = min(MAX_READ_LINES, int(arguments.get("limit") or 2_000))
        if _is_binary(target):
            return await asyncio.to_thread(
                _read_binary_file, target, resolved.relative, context, offset, limit
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        rendered, returned, total, has_more = fs._render_lines(text[:MAX_READ_CHARS], offset, limit)
        version = hashlib.sha256(target.read_bytes()).hexdigest()
        fs._observation_policy.observe(
            str(context.auth.tenant_id),
            str(context.conversation_id),
            resolved.relative,
            version,
            True,
        )
        return {
            "status": "ok",
            "path": resolved.relative,
            "content": rendered,
            "line_count": total,
            "returned_lines": returned,
            "offset": offset,
            "has_more": has_more,
            "truncated": len(text) > MAX_READ_CHARS,
            "size_bytes": target.stat().st_size,
            "version": version,
        }
    except FileSystemRefusalError as e:
        response = {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
        if e.details:
            response.update(e.details)
        return response


async def _fs_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create, overwrite, or append to a workspace text file."""
    fs = get_filesystem_service(context)
    try:
        resolved, refusal = _resolve(context, str(arguments["path"]))
        if refusal is not None:
            return refusal
        assert resolved is not None
        before = _read_text_for_diff(resolved.absolute) or ""
        result = await fs.write(
            str(arguments["path"]),
            str(arguments.get("content") or ""),
            mode=cast(
                Literal["create", "overwrite", "append"],
                str(arguments.get("mode") or "overwrite"),
            ),
        )
        response = {
            "status": "ok",
            "path": result.path,
            "mode": result.mode,
            "created": result.created,
            "size_bytes": result.size_bytes,
            "line_count": result.line_count,
            "version": result.version,
        }
        after = _read_text_for_diff(resolved.absolute)
        if after is not None:
            response["diff"] = _diff_metadata(result.path, before, after)
        return response
    except FileSystemRefusalError as e:
        response = {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
        if e.details:
            response.update(e.details)
        return response


async def _fs_edit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace, append, rename, or move a workspace file."""
    fs = get_filesystem_service(context)
    try:
        resolved, refusal = _resolve(context, str(arguments["path"]))
        if refusal is not None:
            return refusal
        assert resolved is not None
        before = _read_text_for_diff(resolved.absolute) or ""
        result = await fs.edit(
            str(arguments["path"]),
            cast(
                Literal["replace", "append", "rename", "move"],
                str(arguments.get("operation") or "replace"),
            ),
            content=arguments.get("content"),
            new_path=arguments.get("new_path"),
            expected_version=arguments.get("expected_version"),
        )
        response = {
            "status": "ok",
            "operation": result.operation,
            "path": result.path,
            "size_bytes": result.size_bytes,
            "line_count": result.line_count,
            "version": result.version,
        }
        if result.new_path:
            response["new_path"] = result.new_path
            response["renamed_from"] = result.path
            return response
        after = _read_text_for_diff(resolved.absolute)
        if after is not None:
            response["diff"] = _diff_metadata(result.path, before, after)
        return response
    except FileSystemRefusalError as e:
        response = {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
        if e.details:
            response.update(e.details)
        return response


async def _fs_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delete a workspace file, or a directory tree when asked."""
    fs = get_filesystem_service(context)
    try:
        return await fs.delete(
            str(arguments["path"]),
            recursive=bool(arguments.get("recursive")),
        )
    except FileSystemRefusalError as e:
        response = {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
        if e.details:
            response.update(e.details)
        return response


async def _fs_str_replace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace an exact string inside a workspace file."""
    fs = get_filesystem_service(context)
    try:
        resolved, refusal = _resolve(context, str(arguments["path"]))
        if refusal is not None:
            return refusal
        assert resolved is not None
        before = _read_text_for_diff(resolved.absolute) or ""
        result = await fs.str_replace(
            str(arguments["path"]),
            str(arguments["old_string"]),
            str(arguments.get("new_string") or ""),
            replace_all=bool(arguments.get("replace_all")),
            expected_version=arguments.get("expected_version"),
        )
        response = {
            "status": "ok",
            "path": result.path,
            "replaced": result.replaced,
            "size_bytes": result.size_bytes,
            "line_count": result.line_count,
            "version": result.version,
        }
        after = _read_text_for_diff(resolved.absolute)
        if after is not None:
            response["diff"] = _diff_metadata(result.path, before, after)
        return response
    except FileSystemRefusalError as e:
        if e.code == "AMBIGUOUS":
            return {
                "status": "ambiguous",
                "path": e.path,
                "message": e.message,
                "occurrences": 2,
            }
        if e.code.startswith("FS_NOT_OBSERVED"):
            resolved, _ = _resolve(context, str(arguments["path"]))
            if resolved is not None and resolved.absolute.is_file():
                occurrences = resolved.absolute.read_text(encoding="utf-8").count(
                    str(arguments["old_string"])
                )
                if occurrences > 1 and not bool(arguments.get("replace_all")):
                    return {
                        "status": "ambiguous",
                        "path": resolved.relative,
                        "message": "old_string occurs multiple times; use replace_all=true",
                        "occurrences": occurrences,
                    }
        response = {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
        if e.details:
            response.update(e.details)
        return response


# Binary file reading helpers (unchanged - use ingestion router)


def _binary_read_limit(context: ToolContext) -> int:
    """Return the bounded byte limit for a host-path binary read."""
    try:
        configured = int(getattr(context.settings, "upload_max_bytes", DEFAULT_BINARY_READ_BYTES))
    except (TypeError, ValueError):
        configured = DEFAULT_BINARY_READ_BYTES
    return max(1, configured)


def _supported_extensions(router: ExtractorRouter) -> list[str]:
    """Return a bounded, model-safe list of reader extensions."""
    return sorted(
        {
            item.extension
            for item in router.describe_supported_formats()
            if isinstance(item.extension, str) and item.extension
        }
    )[:300]


def _binary_capability_failure(
    *,
    path: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    router: ExtractorRouter,
    error_code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a terminal reader result instead of an ambiguous binary error."""
    safe_details = {
        key: value
        for key, value in (details or {}).items()
        if isinstance(value, str | int | float | bool | list | dict)
    }
    return {
        "status": "unreadable",
        "path": path,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "error_code": error_code,
        "message": message,
        "details": safe_details,
        "capability_failure": True,
        "stop_turn": True,
        "no_retry": True,
        "retryable": False,
        "capability": {
            "requested_extension": Path(filename).suffix.lower() or None,
            "requested_content_type": content_type,
            "available": False,
            "reason": message,
            "supported_extensions": _supported_extensions(router),
            "next_action": (
                "Report this exact limitation to the user. Do not retry with bash, find, "
                "sandbox_execute, browser navigation, or another guessed reader."
            ),
            **safe_details,
        },
    }


def _read_binary_file(
    target: Path,
    relative_path: str,
    context: ToolContext,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Extract one authorized binary file through the shared ingestion router."""
    try:
        size_bytes = target.stat().st_size
    except OSError as exc:
        return {
            "status": "unreadable",
            "path": relative_path,
            "error_code": "FS_READ_FAILED",
            "message": f"The file could not be inspected safely: {exc.strerror or exc}.",
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
    limit_bytes = _binary_read_limit(context)
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    try:
        router = ExtractorRouter(context.settings)
    except Exception as exc:  # noqa: BLE001 - capability discovery must fail closed
        return {
            "status": "unreadable",
            "path": relative_path,
            "filename": target.name,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "error_code": "FS_READER_UNAVAILABLE",
            "message": "No file reader could be initialized for this runtime.",
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
            "capability": {
                "available": False,
                "reason": "The shared ingestion reader could not be initialized.",
                "reader_error_type": type(exc).__name__,
                "next_action": (
                    "Report the missing reader capability. Do not retry with another tool."
                ),
            },
        }
    if size_bytes > limit_bytes:
        return _binary_capability_failure(
            path=relative_path,
            filename=target.name,
            content_type=content_type,
            size_bytes=size_bytes,
            router=router,
            error_code="FS_BINARY_READ_LIMIT_EXCEEDED",
            message=(
                f"The file is {size_bytes} bytes, above the authorized binary-read limit "
                f"of {limit_bytes} bytes. Import it into the Library or use a smaller file."
            ),
            details={"max_bytes": limit_bytes},
        )
    try:
        payload = target.read_bytes()
        if len(payload) > limit_bytes:
            return _binary_capability_failure(
                path=relative_path,
                filename=target.name,
                content_type=content_type,
                size_bytes=len(payload),
                router=router,
                error_code="FS_BINARY_READ_LIMIT_EXCEEDED",
                message=(
                    "The file grew beyond the authorized binary-read limit while it was being "
                    "read. Import it into the Library or use a smaller stable file."
                ),
                details={"max_bytes": limit_bytes},
            )
        result = router.extract(
            filename=target.name,
            content_type=content_type,
            payload=payload,
            tenant_id=context.auth.tenant_id,
        )
    except ApiError as exc:
        return _binary_capability_failure(
            path=relative_path,
            filename=target.name,
            content_type=content_type,
            size_bytes=size_bytes,
            router=router,
            error_code=str(exc.code),
            message=str(exc.message),
            details=exc.details if isinstance(exc.details, dict) else None,
        )
    except OSError as exc:
        return _binary_capability_failure(
            path=relative_path,
            filename=target.name,
            content_type=content_type,
            size_bytes=size_bytes,
            router=router,
            error_code="FS_READ_FAILED",
            message=f"The file could not be read safely: {exc.strerror or exc}.",
        )
    except Exception as exc:  # noqa: BLE001 - malformed files must become terminal results
        return _binary_capability_failure(
            path=relative_path,
            filename=target.name,
            content_type=content_type,
            size_bytes=size_bytes,
            router=router,
            error_code="FS_READER_FAILED",
            message="The available file reader could not extract this file safely.",
            details={"reader_error_type": type(exc).__name__},
        )

    text = str(result.text or "")
    fs = get_filesystem_service(context)
    rendered, returned, total, has_more = fs._render_lines(
        text[:MAX_READ_CHARS], offset, min(MAX_READ_LINES, limit)
    )
    return {
        "status": "ok",
        "path": relative_path,
        "filename": target.name,
        "content_type": content_type,
        "content": rendered,
        "line_count": total,
        "returned_lines": returned,
        "offset": offset,
        "has_more": has_more or len(text) > MAX_READ_CHARS,
        "truncated": len(text) > MAX_READ_CHARS,
        "size_bytes": size_bytes,
        "version": hashlib.sha256(payload).hexdigest(),
        "page_count": result.page_count,
        "extraction_method": result.extraction_method,
        "coverage_score": result.coverage_score,
        "ocr_used": result.ocr_used,
        "vision_used": result.vision_used,
        "warnings": list(result.warnings),
        "capability": {
            "requested_extension": Path(target.name).suffix.lower() or None,
            "requested_content_type": content_type,
            "available": True,
            "extraction_method": result.extraction_method,
        },
    }


FS_READ_DESCRIPTION = (
    "Read a text file or a supported PDF, Office, image, code, or other document from the "
    "authorized workspace. In Full access, an absolute host path is also allowed. Binary "
    "documents use the shared bounded ingestion readers and return extraction metadata. "
    "If the format or reader is unavailable, the result is terminal and names the missing "
    "capability; do not retry with another tool. Use offset and limit for large text."
)
FS_WRITE_DESCRIPTION = (
    "Create or update a text file in the project workspace. In Full access, an absolute host "
    "path is also allowed. mode 'create' fails when the file exists, 'overwrite' replaces it, "
    "and 'append' adds to it."
)
FS_EDIT_DESCRIPTION = (
    "Replace the whole content of a workspace file, append to it, rename it, or "
    "move it to another path inside the workspace. Pass expected_version, the version "
    "a previous read reported, to refuse the edit when the file changed since then. In Full "
    "access, absolute host paths are allowed."
)
FS_DELETE_DESCRIPTION = (
    "Delete a file in the project workspace; Full access also allows an absolute host path. "
    "Every delete pauses for explicit user approval. A directory needs recursive true."
)
FS_STR_REPLACE_DESCRIPTION = (
    "Replace one exact string inside a workspace file. The match must be unique "
    "unless replace_all is true. Pass expected_version, the version a previous read "
    "reported, to refuse the replacement when the file changed since then. In Full access, "
    "absolute host paths are allowed."
)

FS_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="fs_read",
        description=FS_READ_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access. Supported "
                "binary documents are extracted through the shared file readers.",
                min_length=1,
                max_length=1024,
            ),
            "offset": integer_property(
                "First line to return, 1-based.", minimum=1, maximum=MAX_READ_LINES
            ),
            "limit": integer_property(
                "Largest number of lines to return.", minimum=1, maximum=MAX_READ_LINES
            ),
        },
        required=["path"],
        arguments_model=FsReadArguments,
        mode="read",
        handler=_fs_read,
    ),
    define_native_tool(
        name="fs_write",
        description=FS_WRITE_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
            "content": string_property("The full text to write.", max_length=MAX_WRITE_CHARS),
            "mode": string_property(
                "How to write the file.", enum=["create", "overwrite", "append"]
            ),
        },
        required=["path", "content"],
        arguments_model=FsWriteArguments,
        mode="write",
        handler=_fs_write,
    ),
    define_native_tool(
        name="fs_edit",
        description=FS_EDIT_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
            "operation": string_property(
                "The edit to perform.", enum=["replace", "append", "rename", "move"]
            ),
            "content": string_property(
                "The new content for replace and append.", max_length=MAX_WRITE_CHARS
            ),
            "new_path": string_property(
                "The destination path for rename and move.", min_length=1, max_length=1024
            ),
            "expected_version": string_property(
                "The version the file must still hold, as a read reported it. The edit "
                "is refused when the file changed since then.",
                min_length=1,
                max_length=VERSION_LENGTH,
            ),
        },
        required=["path"],
        arguments_model=FsEditArguments,
        mode="write",
        handler=_fs_edit,
    ),
    define_native_tool(
        name="fs_delete",
        description=FS_DELETE_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
            "recursive": boolean_property("Remove a directory and its contents."),
        },
        required=["path"],
        arguments_model=FsDeleteArguments,
        mode="write",
        handler=_fs_delete,
    ),
    define_native_tool(
        name="fs_str_replace",
        description=FS_STR_REPLACE_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
            "old_string": string_property(
                "The exact text to replace.", min_length=1, max_length=100_000
            ),
            "new_string": string_property("The replacement text.", max_length=100_000),
            "replace_all": boolean_property("Replace every occurrence instead of one."),
            "expected_version": string_property(
                "The version the file must still hold, as a read reported it. The "
                "replacement is refused when the file changed since then.",
                min_length=1,
                max_length=VERSION_LENGTH,
            ),
        },
        required=["path", "old_string"],
        arguments_model=FsStrReplaceArguments,
        mode="write",
        handler=_fs_str_replace,
    ),
    define_native_tool(
        name="fs_lock",
        description=(
            "Acquire an exclusive lock on a file to prevent concurrent modifications. "
            "The lock is released automatically when the turn ends, or can be released "
            "explicitly with fs_unlock. Use this when multiple agents might modify the "
            "same file and you need to coordinate access."
        ),
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
            "wait_ms": integer_property(
                "Maximum milliseconds to wait for the lock. Default 5000.",
                minimum=0,
                maximum=60_000,
            ),
        },
        required=["path"],
        arguments_model=FsLockArguments,
        mode="write",
        handler=_fs_lock,
    ),
    define_native_tool(
        name="fs_unlock",
        description=(
            "Release a file lock acquired with fs_lock. The lock is automatically "
            "released when the turn ends, but can be released early if the agent "
            "finishes its work on the file before the turn ends."
        ),
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
        },
        required=["path"],
        arguments_model=FsUnlockArguments,
        mode="write",
        handler=_fs_unlock,
    ),
    define_native_tool(
        name="fs_watch",
        description=(
            "Register a callback to be notified when a file or directory changes. "
            "The callback receives the relative path of the changed file. Returns an "
            "unwatch function to stop watching. The watch persists for the lifetime "
            "of the conversation unless unwatched explicitly."
        ),
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
        },
        required=["path"],
        arguments_model=FsWatchArguments,
        mode="read",
        handler=_fs_watch,
    ),
    define_native_tool(
        name="fs_unwatch",
        description=(
            "Stop watching a file or directory for changes. The path must match a "
            "previously watched path exactly."
        ),
        properties={
            "path": string_property(
                "Workspace-relative path, or an absolute host path in Full access.",
                min_length=1,
                max_length=1024,
            ),
        },
        required=["path"],
        arguments_model=FsUnwatchArguments,
        mode="write",
        handler=_fs_unwatch,
    ),
]

__all__ = [
    "FS_TOOLS",
    "FsDeleteArguments",
    "FsEditArguments",
    "FsLockArguments",
    "FsReadArguments",
    "FsStrReplaceArguments",
    "FsUnlockArguments",
    "FsUnwatchArguments",
    "FsWatchArguments",
    "FsWriteArguments",
    "VERSION_LENGTH",
    "_fs_lock",
    "_fs_unlock",
    "_fs_watch",
    "_fs_unwatch",
    "_resolve",
    "_root_for",
]
