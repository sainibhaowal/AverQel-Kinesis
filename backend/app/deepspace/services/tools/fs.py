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
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from app.core.errors import ApiError
from app.deepspace.services.filesystem import (
    DeepSpaceFileSystemService,
    FileSystemRefusal,
    get_filesystem_service,
)
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
from app.ingestion.services.extractors.router import ExtractorRouter

#: The largest file a read may return, in characters.
MAX_READ_CHARS = 200_000

# Binary files are handed to the same bounded ingestion readers used by the
# Library.  Keep the host-path reader within the deployment upload ceiling so
# a Full-access read cannot turn into an unbounded memory allocation.
DEFAULT_BINARY_READ_BYTES = 26_214_400

#: The largest file a write may create, in characters.
MAX_WRITE_CHARS = 400_000

#: The largest number of lines one read may return.
MAX_READ_LINES = 5_000

#: The largest number of lines one edit may add.
MAX_EDIT_LINES = 20_000

#: The number of hexadecimal characters in a file version.
VERSION_LENGTH = 64


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
    from app.deepspace.services.workspace_root import workspace_root_for

    if context.workspace_root is not None:
        return context.workspace_root
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _resolve(
    context: ToolContext, raw_path: str
) -> tuple[Any, dict[str, Any] | None]:
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
    except FileSystemRefusal as e:
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


async def _fs_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a workspace text or supported binary file with numbered lines."""
    fs = get_filesystem_service(context)
    try:
        result = await fs.read(
            str(arguments["path"]),
            offset=int(arguments.get("offset") or 1),
            limit=min(MAX_READ_LINES, int(arguments.get("limit") or 2_000)),
        )
        return {
            "status": "ok",
            "path": result.path,
            "content": result.content,
            "line_count": result.line_count,
            "returned_lines": result.returned_lines,
            "offset": result.offset,
            "has_more": result.has_more,
            "truncated": result.truncated,
            "size_bytes": result.size_bytes,
            "version": result.version,
        }
    except FileSystemRefusal as e:
        return {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }


async def _fs_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create, overwrite, or append to a workspace text file."""
    fs = get_filesystem_service(context)
    try:
        result = await fs.write(
            str(arguments["path"]),
            str(arguments.get("content") or ""),
            mode=str(arguments.get("mode") or "overwrite"),
        )
        return {
            "status": "ok",
            "path": result.path,
            "mode": result.mode,
            "created": result.created,
            "size_bytes": result.size_bytes,
            "line_count": result.line_count,
            "version": result.version,
        }
    except FileSystemRefusal as e:
        return {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }


async def _fs_edit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace, append, rename, or move a workspace file."""
    fs = get_filesystem_service(context)
    try:
        result = await fs.edit(
            str(arguments["path"]),
            str(arguments.get("operation") or "replace"),
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
        return response
    except FileSystemRefusal as e:
        return {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }


async def _fs_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delete a workspace file, or a directory tree when asked."""
    fs = get_filesystem_service(context)
    try:
        return await fs.delete(
            str(arguments["path"]),
            recursive=bool(arguments.get("recursive")),
        )
    except FileSystemRefusal as e:
        return {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }


async def _fs_str_replace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace an exact string inside a workspace file."""
    fs = get_filesystem_service(context)
    try:
        result = await fs.str_replace(
            str(arguments["path"]),
            str(arguments["old_string"]),
            str(arguments.get("new_string") or ""),
            replace_all=bool(arguments.get("replace_all")),
            expected_version=arguments.get("expected_version"),
        )
        return {
            "status": "ok",
            "path": result.path,
            "replaced": result.replaced,
            "size_bytes": result.size_bytes,
            "line_count": result.line_count,
            "version": result.version,
        }
    except FileSystemRefusal as e:
        return {
            "status": "error",
            "error_code": e.code,
            "path": e.path,
            "message": e.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }


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
    rendered, returned, total, has_more = _render_lines(
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
]

__all__ = [
    "FS_TOOLS",
    "FsDeleteArguments",
    "FsEditArguments",
    "FsReadArguments",
    "FsStrReplaceArguments",
    "FsWriteArguments",
    "VERSION_LENGTH",
    "describe_workspace",
    "file_version",
    "WorkspacePathError",
    "WorkspaceUnavailableError",
    "_resolve",
    "_root_for",
]