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

import hashlib
import mimetypes
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
    ResolvedWorkspacePath,
    WorkspacePathError,
    WorkspaceUnavailableError,
    describe_workspace,
    remove_path,
    resolve_within_root,
    workspace_root_for,
)

#: The largest file a read may return, in characters.
MAX_READ_CHARS = 200_000

#: The largest file a write may create, in characters.
MAX_WRITE_CHARS = 400_000

#: The largest number of lines one read may return.
MAX_READ_LINES = 5_000

#: The largest number of lines one edit may add.
MAX_EDIT_LINES = 20_000

#: The number of hexadecimal characters in a file version.
VERSION_LENGTH = 64


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
    new_path: str | None = Field(default=None, max_length=1024)
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


def file_version(path: Path) -> str:
    """Return the version of one workspace file.

    The version is the lowercase hexadecimal SHA-256 digest of the file's
    bytes, the same hash the workspace snapshot records, so a version the
    agent read stays valid wherever it is used.

    Args:
        path: The absolute path of the file.

    Returns:
        The file's version.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version_refusal(expected: str, current: str | None, relative: str) -> dict[str, Any] | None:
    """Refuse an edit whose expected version no longer matches the file.

    Args:
        expected: The version the caller expected the file to hold.
        current: The version the file holds now, or None when the file is
            gone.
        relative: The file's path relative to the workspace root.

    Returns:
        The refusal to return instead of the edit, or None when the file
        still holds the expected version.
    """
    if current is None:
        return {
            "status": "error",
            "error_code": "FS_AMBIGUOUS_EDIT",
            "path": relative,
            "message": (
                "The file is gone, so its version cannot be checked. Read the "
                "workspace again, then retry."
            ),
        }
    if current != expected:
        return {
            "status": "error",
            "error_code": "FS_STALE_VERSION",
            "path": relative,
            "current_version": current,
            "message": (
                "The file changed since it was read. Read it again, then retry "
                "with the version it holds now."
            ),
        }
    return None


def _resolve(
    context: ToolContext, raw_path: str
) -> tuple[ResolvedWorkspacePath | None, dict[str, Any] | None]:
    """Resolve one path inside the caller's workspace.

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
        return resolve_within_root(root, raw_path), None
    except WorkspacePathError as exc:
        return None, {
            "status": "error",
            "message": str(exc),
            "path": str(raw_path)[:1_024],
        }
    except WorkspaceUnavailableError as exc:
        return None, {"status": "unavailable", "message": str(exc)}


def _is_binary(path: Path) -> bool:
    """Report whether a file looks like binary content."""
    try:
        chunk = path.open("rb").read(4096)
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


def _render_lines(text: str, offset: int, limit: int) -> tuple[str, int, int, bool]:
    """Render numbered lines for a read result.

    Args:
        text: The file content.
        offset: The first line to return, 1-based.
        limit: The largest number of lines to return.

    Returns:
        The rendered block, the number of lines returned, the total line
        count, and whether more lines follow.
    """
    lines = text.splitlines()
    total = len(lines)
    start = max(1, offset) - 1
    selected = lines[start : start + limit]
    rendered = "\n".join(f"{start + index + 1}\t{line}" for index, line in enumerate(selected))
    return rendered, len(selected), total, start + len(selected) < total


async def _fs_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a workspace text file with numbered lines."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    target = resolved.absolute
    if not target.exists():
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "No such file in the project workspace.",
        }
    if target.is_dir():
        return {
            "status": "is_directory",
            "path": resolved.relative,
            "message": "The path is a directory; use fs_glob to list it.",
        }
    if _is_binary(target):
        return {
            "status": "binary",
            "path": resolved.relative,
            "size_bytes": target.stat().st_size,
            "message": "The file holds binary content and cannot be read as text.",
        }
    text = target.read_text(encoding="utf-8", errors="replace")
    offset = int(arguments.get("offset") or 1)
    limit = min(MAX_READ_LINES, int(arguments.get("limit") or 2_000))
    rendered, returned, total, has_more = _render_lines(text, offset, limit)
    truncated = len(text) > MAX_READ_CHARS
    return {
        "status": "ok",
        "path": resolved.relative,
        "content": rendered,
        "line_count": total,
        "returned_lines": returned,
        "offset": offset,
        "has_more": has_more,
        "truncated": truncated,
        "size_bytes": target.stat().st_size,
        "version": file_version(target),
    }


async def _fs_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create, overwrite, or append to a workspace text file."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    target = resolved.absolute
    mode = str(arguments.get("mode") or "overwrite")
    content = str(arguments.get("content") or "")
    if target.exists() and target.is_dir():
        return {
            "status": "is_directory",
            "path": resolved.relative,
            "message": "The path is a directory; use fs_edit to move it instead.",
        }
    existed = target.exists()
    if mode == "create" and existed:
        return {
            "status": "exists",
            "path": resolved.relative,
            "message": "The file already exists; use mode 'overwrite' or 'append'.",
        }
    if mode == "append" and not existed:
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "Append needs an existing file; use mode 'create' instead.",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    line_count = len(content.splitlines())
    if line_count > MAX_EDIT_LINES:
        return {
            "status": "warning",
            "path": resolved.relative,
            "line_count": line_count,
            "message": f"The file now holds {line_count} lines, above the {MAX_EDIT_LINES} guideline.",
        }
    return {
        "status": "ok",
        "path": resolved.relative,
        "mode": mode,
        "created": not existed,
        "size_bytes": target.stat().st_size,
        "line_count": line_count,
        "version": file_version(target),
    }


async def _fs_edit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace, append, rename, or move a workspace file."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    target = resolved.absolute
    operation = str(arguments.get("operation") or "replace")
    expected = arguments.get("expected_version")
    if not target.exists():
        if expected:
            version_refusal = _version_refusal(str(expected), None, resolved.relative)
            assert version_refusal is not None
            return version_refusal
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "No such file in the project workspace.",
        }
    if target.is_dir():
        return {
            "status": "is_directory",
            "path": resolved.relative,
            "message": "The path is a directory; use fs_write to create a file instead.",
        }
    if expected:
        version_refusal = _version_refusal(str(expected), file_version(target), resolved.relative)
        if version_refusal is not None:
            return version_refusal
    if operation in {"rename", "move"}:
        destination, destination_refusal = _resolve(context, str(arguments.get("new_path") or ""))
        if destination_refusal is not None:
            return destination_refusal
        assert destination is not None
        if destination.absolute.exists():
            return {
                "status": "exists",
                "path": destination.relative,
                "message": "The destination already exists.",
            }
        destination.absolute.parent.mkdir(parents=True, exist_ok=True)
        target.replace(destination.absolute)
        return {
            "status": "ok",
            "operation": operation,
            "path": resolved.relative,
            "new_path": destination.relative,
            "version": file_version(destination.absolute),
        }
    content = str(arguments.get("content") or "")
    if operation == "append":
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    return {
        "status": "ok",
        "operation": operation,
        "path": resolved.relative,
        "size_bytes": target.stat().st_size,
        "line_count": len(content.splitlines()),
        "version": file_version(target),
    }


async def _fs_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delete a workspace file, or a directory tree when asked."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    target = resolved.absolute
    if not target.exists():
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "No such file in the project workspace.",
        }
    if target.is_dir():
        if not bool(arguments.get("recursive")):
            return {
                "status": "not_empty",
                "path": resolved.relative,
                "message": "The path is a directory; pass recursive true to remove it and its contents.",
            }
        remove_path(target)
        return {"status": "ok", "path": resolved.relative, "deleted": "directory"}
    remove_path(target)
    return {"status": "ok", "path": resolved.relative, "deleted": "file"}


async def _fs_str_replace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Replace an exact string inside a workspace file."""
    resolved, refusal = _resolve(context, str(arguments["path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    target = resolved.absolute
    expected = arguments.get("expected_version")
    if not target.exists():
        if expected:
            version_refusal = _version_refusal(str(expected), None, resolved.relative)
            assert version_refusal is not None
            return version_refusal
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "No such file in the project workspace.",
        }
    if target.is_dir():
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "No such file in the project workspace.",
        }
    if expected:
        version_refusal = _version_refusal(str(expected), file_version(target), resolved.relative)
        if version_refusal is not None:
            return version_refusal
    old_string = str(arguments["old_string"])
    new_string = str(arguments.get("new_string") or "")
    text = target.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old_string)
    if occurrences == 0:
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "The exact text to replace does not occur in the file.",
        }
    replace_all = bool(arguments.get("replace_all"))
    if occurrences > 1 and not replace_all:
        return {
            "status": "ambiguous",
            "path": resolved.relative,
            "occurrences": occurrences,
            "message": (
                "The text occurs more than once. Supply more surrounding context, "
                "or set replace_all true."
            ),
        }
    updated = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    target.write_text(updated, encoding="utf-8")
    return {
        "status": "ok",
        "path": resolved.relative,
        "replacements": occurrences if replace_all else 1,
        "size_bytes": target.stat().st_size,
        "version": file_version(target),
    }


FS_READ_DESCRIPTION = (
    "Read a text file from the project workspace. Returns the content with line "
    "numbers. Use offset and limit for a large file."
)
FS_WRITE_DESCRIPTION = (
    "Create or update a text file in the project workspace. mode 'create' fails "
    "when the file exists, 'overwrite' replaces it, and 'append' adds to it."
)
FS_EDIT_DESCRIPTION = (
    "Replace the whole content of a workspace file, append to it, rename it, or "
    "move it to another path inside the workspace. Pass expected_version, the version "
    "a previous read reported, to refuse the edit when the file changed since then."
)
FS_DELETE_DESCRIPTION = "Delete a file in the project workspace. A directory needs recursive true."
FS_STR_REPLACE_DESCRIPTION = (
    "Replace one exact string inside a workspace file. The match must be unique "
    "unless replace_all is true. Pass expected_version, the version a previous read "
    "reported, to refuse the replacement when the file changed since then."
)

FS_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="fs_read",
        description=FS_READ_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path of the file to read.", min_length=1, max_length=1024
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
                "Workspace-relative path of the file to write.", min_length=1, max_length=1024
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
                "Workspace-relative path of the file to edit.", min_length=1, max_length=1024
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
                "Workspace-relative path to delete.", min_length=1, max_length=1024
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
                "Workspace-relative path of the file to change.", min_length=1, max_length=1024
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
]
