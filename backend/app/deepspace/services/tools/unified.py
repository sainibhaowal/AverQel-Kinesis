"""The unified DeepSpace tools.

DeepSpace exposes one read tool and one write tool.  Each routes to the
capability that owns the requested resource:

* workspace paths use the bounded, versioned filesystem service;
* Library files and the active note use the conversation-scoped task store;
* durable memory uses the tenant/user-scoped memory service;
* public pages and images use the existing safe web readers.

The specialized tools (``glob``, ``grep``, ``lock``, ``unlock``, ``watch``,
``unwatch``, ``delete``, ``stat``) keep a single purpose and delegate to the
tested filesystem handlers rather than reimplementing them here.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.deepspace.memory.memory_service import MemoryService
from app.deepspace.services.filesystem import (
    FileSystemError,
    FileSystemRefusalError,
    get_filesystem_service,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    boolean_property,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.tools.fs import (
    _fs_delete,
    _fs_edit,
    _fs_lock,
    _fs_read,
    _fs_str_replace,
    _fs_unlock,
    _fs_unwatch,
    _fs_watch,
    _fs_write,
)
from app.deepspace.services.tools.fs_search import _fs_glob, _fs_grep
from app.deepspace.services.tools.web import _image_read, _web_fetch, _web_search

# ============================================================
# Constants
# ============================================================

MAX_READ_CHARS = 10_000_000
MAX_WRITE_CHARS = 10_000_000
MAX_READ_LINES = 100_000
VERSION_LENGTH = 64
MAX_SEARCH_RESULTS = 1_000
MAX_GLOB_RESULTS = 50_000


# ============================================================
# Shared helpers
# ============================================================


def _error(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error_code": error_code, "message": message, **extra}


def _task_store(context: ToolContext) -> Any | None:
    store = context.task_store
    if store is None:
        return None
    return store


def _memory_service(context: ToolContext) -> MemoryService | None:
    if context.db is None:
        return None
    return MemoryService(context.db, context.settings)


def _bounded_limit(value: int | None, default: int, maximum: int) -> int:
    return max(1, min(int(value or default), maximum))


# ============================================================
# Argument models
# ============================================================


class ReadArguments(ToolArguments):
    """Read one authorized resource: a workspace path, a data store, or the web."""

    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Workspace-relative or absolute path. A file returns its numbered text; "
            "a directory returns its immediate entries."
        ),
    )
    target: Literal["library", "memory", "note", "workspace", "web"] | None = Field(
        default=None,
        description="Data store to read. Use path alone for a workspace file.",
    )
    url: str | None = Field(
        default=None,
        min_length=8,
        max_length=2048,
        description="Public HTTP(S) URL to fetch as text or an image.",
    )
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Search query for Library files, memory, or the public web.",
    )

    file_id: str | None = Field(default=None, max_length=80)
    filename: str | None = Field(default=None, max_length=255)
    memory_key: str | None = Field(default=None, max_length=120)
    folder_id: str | None = Field(default=None, max_length=80)

    operation: Literal["read", "query", "search"] = "read"
    offset: int | None = Field(default=None, ge=1, le=MAX_READ_LINES)
    limit: int | None = Field(default=None, ge=1, le=5000)
    max_characters: int | None = Field(default=None, ge=100, le=MAX_READ_CHARS)
    max_results: int | None = Field(default=None, ge=1, le=MAX_SEARCH_RESULTS)
    domains: list[str] | None = Field(default=None, max_length=20)
    time_range: Literal["day", "week", "month", "year"] | None = None
    provider: Literal["searxng", "tavily"] | None = None
    mode: Literal["auto", "text", "image"] = "auto"

    @model_validator(mode="after")
    def validate_read_operation(self) -> ReadArguments:
        if self.target == "workspace":
            if not (self.path or "").strip():
                raise ValueError("read(target='workspace') requires path")
            return self
        if self.target is None and self.path:
            return self
        if self.target == "web":
            if not self.url and not self.query:
                raise ValueError("read(target='web') requires url or query")
            return self
        if self.target in ("library", "memory", "note"):
            return self
        if self.url or self.query:
            return self
        raise ValueError(
            "Specify path, target, url, or query. Use target='library', 'memory', "
            "'note', 'workspace', or 'web'."
        )


class WriteArguments(ToolArguments):
    """Create, update, rename, or replace one authorized resource."""

    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Workspace-relative or absolute path.",
    )
    target: Literal["library", "memory", "note", "workspace"] | None = Field(
        default=None,
        description="Data store to write. Use path alone for a workspace file.",
    )

    content: str | None = Field(
        default=None,
        max_length=MAX_WRITE_CHARS,
        description="Content to write, append, or use as a replacement.",
    )

    operation: Literal[
        "create", "replace", "overwrite", "append", "str_replace", "rename", "move"
    ] = "create"

    file_id: str | None = Field(default=None, max_length=80)
    filename: str | None = Field(default=None, max_length=255)
    memory_key: str | None = Field(default=None, max_length=120)
    folder_id: str | None = Field(default=None, max_length=80)
    memory_scope: Literal["user", "session"] | None = None

    old_string: str | None = Field(default=None, max_length=100_000)
    new_string: str | None = Field(default="", max_length=100_000)
    replace_all: bool = False
    expected_version: str | None = Field(default=None, min_length=1, max_length=VERSION_LENGTH)

    new_path: str | None = Field(default=None, min_length=1, max_length=1024)
    new_folder_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_write_operation(self) -> WriteArguments:
        if self.operation in ("create", "replace", "overwrite", "append") and self.content is None:
            if self.target == "workspace" or (self.target is None and (self.path or "").strip()):
                raise ValueError(
                    f"write(operation='{self.operation}') requires non-empty or explicit 'content' string"
                )
        if self.operation == "str_replace" and self.old_string is None:
            if self.target == "workspace" or (self.target is None and (self.path or "").strip()):
                raise ValueError("write(operation='str_replace') requires old_string")
        if self.target == "workspace":
            if not (self.path or "").strip():
                raise ValueError("write(target='workspace') requires path")
            return self
        if self.target is None:
            if (self.path or "").strip():
                return self
            if (self.filename or "").strip():
                self.target = "library"
                return self
            raise ValueError(
                "Specify 'path' (e.g. 'notes/report.md') for a workspace file, or "
                "'target=\"library\"' with 'filename'. Do not call write merely to reply in chat."
            )
        if self.target in ("library", "memory", "note"):
            if (
                self.operation in ("create", "replace", "overwrite", "append")
                and self.content is None
            ):
                raise ValueError(f"write(operation='{self.operation}') requires content")
            if self.operation == "str_replace" and self.old_string is None:
                raise ValueError("write(operation='str_replace') requires old_string")
            return self
        raise ValueError(f"Unsupported write target: {self.target}")


class GlobArguments(ToolArguments):
    """List paths under the selected, authorized search root."""

    pattern: str = Field(min_length=1, max_length=512)
    path: str | None = Field(default=None, max_length=1024)
    entire_system: bool = False
    max_results: int | None = Field(default=None, ge=1, le=MAX_GLOB_RESULTS)


class GrepArguments(ToolArguments):
    """Search workspace file contents with a regular expression."""

    pattern: str = Field(min_length=1, max_length=2000)
    path: str | None = Field(default=None, max_length=1024)
    entire_system: bool = False
    glob: str | None = Field(default=None, max_length=255)
    output_mode: Literal["content", "files_with_matches", "count"] = "content"
    case_sensitive: bool = True
    max_results: int | None = Field(default=None, ge=1, le=MAX_GLOB_RESULTS)


class LockArguments(ToolArguments):
    """Acquire an exclusive lock on a file."""

    path: str = Field(min_length=1, max_length=1024)
    wait_ms: int | None = Field(default=5000, ge=0, le=60_000)


class UnlockArguments(ToolArguments):
    """Release a file lock."""

    path: str = Field(min_length=1, max_length=1024)


class WatchArguments(ToolArguments):
    """Register a callback for file changes."""

    path: str = Field(min_length=1, max_length=1024)


class UnwatchArguments(ToolArguments):
    """Stop watching a file or directory for changes."""

    path: str = Field(min_length=1, max_length=1024)


class DeleteArguments(ToolArguments):
    """Delete a workspace path, a Library file, or one memory."""

    path: str | None = Field(default=None, min_length=1, max_length=1024)
    target: Literal["workspace", "library", "memory"] | None = None
    file_id: str | None = Field(default=None, max_length=80)
    memory_key: str | None = Field(default=None, max_length=120)
    recursive: bool = False


class StatArguments(ToolArguments):
    """Get file metadata."""

    path: str = Field(min_length=1, max_length=1024)


class WebSearchArguments(ToolArguments):
    """Search the public web through the configured provider."""

    query: str = Field(min_length=2, max_length=512)
    max_results: int | None = Field(default=None, ge=1, le=MAX_SEARCH_RESULTS)
    domains: list[str] | None = Field(default=None, max_length=20)
    time_range: Literal["day", "week", "month", "year"] | None = None
    provider: Literal["searxng", "tavily"] | None = None


class WebFetchArguments(ToolArguments):
    """Fetch one public web URL as text or as an image."""

    url: str = Field(min_length=8, max_length=2048)
    mode: Literal["auto", "text", "image"] = "auto"
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


class ImageReadArguments(ToolArguments):
    """Inspect a public raster or SVG image URL for dimensions and visual context."""

    url: str = Field(min_length=8, max_length=2048)
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


class GetToolResultArguments(ToolArguments):
    """Retrieve a bounded continuation of a previously referenced tool result."""

    result_ref: str = Field(min_length=8, max_length=128)


# ============================================================
# Read handlers
# ============================================================


def _read_library(context: ToolContext, args: ReadArguments) -> dict[str, Any]:
    store = _task_store(context)
    if store is None:
        return _error("STORE_UNAVAILABLE", "The Library store is not available in this runtime.")
    try:
        if args.operation == "query" or args.query:
            files = store.find_workspace_files(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                query=args.query or "",
                limit=_bounded_limit(args.limit, 10, 50),
                parent_folder_id=args.folder_id,
            )
            return {
                "status": "ok",
                "target": "library",
                "query": args.query or "",
                "files": files,
                "count": len(files),
            }
        if args.file_id or args.filename:
            file_id = args.file_id
            filename = args.filename
            if file_id and not filename:
                try:
                    uuid.UUID(file_id)
                except (ValueError, TypeError):
                    filename, file_id = file_id, None
            result = store.read_workspace_file(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                file_id=file_id,
                filename=filename,
            )
            payload = {"status": "ok", "target": "library", **result}
            payload["file"] = {
                key: result.get(key)
                for key in ("id", "name", "content_type", "size_bytes", "version")
                if key in result
            }
            text = str(result.get("extracted_text") or result.get("content") or "")
            if args.max_characters is not None and len(text) > args.max_characters:
                text = text[: args.max_characters]
                payload["truncated"] = True
            payload["text"] = text
            return payload
        return {
            "status": "ok",
            "target": "library",
            **store.list_workspace_entries(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                parent_folder_id=args.folder_id,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - return an actionable tool result
        return _error("LIBRARY_READ_FAILED", str(exc), target="library")


def _read_note(context: ToolContext, args: ReadArguments) -> dict[str, Any]:
    del args
    store = _task_store(context)
    if store is None:
        return _error("STORE_UNAVAILABLE", "The note store is not available in this runtime.")
    try:
        return {
            "status": "ok",
            "target": "note",
            **store.read_note(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return _error("NOTE_READ_FAILED", str(exc), target="note")


async def _read_memory(context: ToolContext, args: ReadArguments) -> dict[str, Any]:
    service = _memory_service(context)
    if service is None:
        return _error("STORE_UNAVAILABLE", "Memory is not available in this runtime.")
    tenant_id = str(context.auth.tenant_id)
    user_id = str(context.auth.user_id)
    conversation_id = str(context.conversation_id)
    try:
        if args.memory_key:
            value = await service.retrieve_fact(
                tenant_id=tenant_id,
                user_id=user_id,
                key=args.memory_key,
                conversation_id=conversation_id,
            )
            if value is None:
                return {
                    "status": "not_found",
                    "target": "memory",
                    "memory_key": args.memory_key,
                    "value": None,
                }
            return {
                "status": "ok",
                "target": "memory",
                "memory_key": args.memory_key,
                "value": value,
            }
        limit = _bounded_limit(args.limit, 20, 100)
        if args.query:
            memories = await service.search_memories(
                tenant_id=tenant_id,
                user_id=user_id,
                query=args.query,
                limit=limit,
                conversation_id=conversation_id,
            )
        else:
            memories = await service.list_all_memories(
                tenant_id=tenant_id,
                user_id=user_id,
            )
        memories = memories[:limit]
        return {
            "status": "ok",
            "target": "memory",
            "query": args.query or "",
            "memories": memories,
            "count": len(memories),
        }
    except Exception as exc:  # noqa: BLE001
        return _error("MEMORY_READ_FAILED", str(exc), target="memory")


async def _handle_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Route one read request to the capability that owns the resource."""
    args = ReadArguments(**arguments)

    if args.path or args.target == "workspace":
        if not args.path:
            return _error("WORKSPACE_PATH_REQUIRED", "A workspace path is required.")
        return await _fs_read(
            {"path": args.path, "offset": args.offset, "limit": args.limit},
            context,
        )
    if args.target == "library":
        return _read_library(context, args)
    if args.target == "note":
        return _read_note(context, args)
    if args.target == "memory":
        return await _read_memory(context, args)
    if args.url:
        return await _web_fetch(
            {
                "url": args.url,
                "mode": args.mode,
                "allowed_domains": args.domains,
            },
            context,
        )
    if args.query:
        return await _web_search(
            {
                "query": args.query,
                "max_results": args.max_results,
                "domains": args.domains,
                "time_range": args.time_range,
                "provider": args.provider,
            },
            context,
        )
    return _error("INVALID_ARGUMENTS", "No readable resource was specified.")


# ============================================================
# Write handlers
# ============================================================


async def _write_workspace(context: ToolContext, args: WriteArguments) -> dict[str, Any]:
    if not args.path:
        return _error("WORKSPACE_PATH_REQUIRED", "A workspace path is required.")
    if args.operation == "str_replace":
        if args.old_string is None:
            return _error("MISSING_OLD_STRING", "str_replace requires old_string.")
        return await _fs_str_replace(
            {
                "path": args.path,
                "old_string": args.old_string,
                "new_string": args.new_string or "",
                "replace_all": args.replace_all,
                "expected_version": args.expected_version,
            },
            context,
        )
    if args.operation == "create":
        if args.content is None:
            return _error("MISSING_CONTENT", "create requires content.")
        return await _fs_write(
            {"path": args.path, "content": args.content, "mode": "create"},
            context,
        )
    if args.operation == "overwrite":
        if args.content is None:
            return _error("MISSING_CONTENT", "overwrite requires content.")
        return await _fs_write(
            {"path": args.path, "content": args.content, "mode": "overwrite"},
            context,
        )
    if args.operation in ("replace", "append"):
        if args.content is None:
            return _error("MISSING_CONTENT", f"{args.operation} requires content.")
        return await _fs_edit(
            {
                "path": args.path,
                "operation": args.operation,
                "content": args.content,
                "expected_version": args.expected_version,
            },
            context,
        )
    if args.operation in ("rename", "move"):
        if not args.new_path:
            return _error("MISSING_NEW_PATH", f"{args.operation} requires new_path.")
        return await _fs_edit(
            {
                "path": args.path,
                "operation": args.operation,
                "new_path": args.new_path,
                "expected_version": args.expected_version,
            },
            context,
        )
    return _error("INVALID_OPERATION", f"Unsupported operation: {args.operation}")


def _library_entries(store: Any, context: ToolContext, folder_id: str | None) -> dict[str, Any]:
    """Return the exact immediate Library entries for one folder."""
    result = store.list_workspace_entries(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        parent_folder_id=folder_id,
    )
    if not isinstance(result, dict):
        return {"files": [], "folders": []}
    return result


def _write_library(context: ToolContext, args: WriteArguments) -> dict[str, Any]:
    store = _task_store(context)
    if store is None:
        return _error("STORE_UNAVAILABLE", "The Library store is not available in this runtime.")
    tenant_id = context.auth.tenant_id
    user_id = context.auth.user_id
    conversation_id = context.conversation_id
    try:
        if args.operation in ("create", "replace", "overwrite", "append"):
            if not args.filename:
                return _error(
                    "MISSING_FILENAME",
                    f"write(target='library', operation='{args.operation}') requires filename.",
                )
            if args.content is None:
                return _error("MISSING_CONTENT", f"{args.operation} requires content.")
            existing = _library_entries(store, context, args.folder_id)
            exists = any(
                str(item.get("name") or "") == args.filename for item in existing.get("files", [])
            )
            if args.operation == "create" and exists:
                return _error(
                    "ALREADY_EXISTS",
                    f"A Library file named '{args.filename}' already exists here.",
                    target="library",
                )
            if args.operation in ("replace", "append") and not exists:
                return _error(
                    "NOT_FOUND",
                    f"No Library file named '{args.filename}' exists here; use operation='create'.",
                    target="library",
                )
            result = store.write_workspace_file(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                filename=args.filename,
                content=args.content,
                mode="append" if args.operation == "append" else "replace",
                parent_folder_id=args.folder_id,
            )
            payload = {"status": "ok", "target": "library", **result}
            payload["file"] = {
                key: result.get(key)
                for key in ("id", "name", "content_type", "size_bytes", "version")
                if key in result
            }
            return payload

        if args.operation in ("rename", "move"):
            if not args.file_id:
                return _error(
                    "MISSING_FILE_ID",
                    f"write(target='library', operation='{args.operation}') requires file_id.",
                )
            new_name = args.filename or args.new_path
            if not new_name and not args.folder_id:
                return _error(
                    "MISSING_DESTINATION",
                    "Library rename/move requires filename or folder_id.",
                )
            result = store.edit_workspace_file(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                file_id=args.file_id,
                name=new_name,
                parent_folder_id=args.folder_id,
            )
            return {"status": "ok", "target": "library", **result}

        if args.operation == "str_replace":
            if not args.file_id or args.old_string is None:
                return _error(
                    "MISSING_ARGUMENTS",
                    "Library str_replace requires file_id and old_string.",
                )
            current = store.read_workspace_file(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                file_id=args.file_id,
            )
            if current.get("is_binary"):
                return _error(
                    "BINARY_FILE",
                    "Binary Library files must be replaced through file upload.",
                    target="library",
                )
            text = str(current.get("content") or "")
            occurrences = text.count(args.old_string)
            if occurrences == 0:
                return _error(
                    "NOT_FOUND",
                    "old_string does not occur in this Library file.",
                    target="library",
                )
            if occurrences > 1 and not args.replace_all:
                return {
                    "status": "ambiguous",
                    "target": "library",
                    "path": current.get("name"),
                    "message": "old_string occurs multiple times; use replace_all=true.",
                    "occurrences": occurrences,
                }
            updated = text.replace(
                args.old_string,
                args.new_string or "",
                -1 if args.replace_all else 1,
            )
            result = store.edit_workspace_file(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                file_id=args.file_id,
                content=updated,
            )
            return {"status": "ok", "target": "library", **result}
    except Exception as exc:  # noqa: BLE001
        return _error("LIBRARY_WRITE_FAILED", str(exc), target="library")
    return _error("INVALID_OPERATION", f"Unsupported operation: {args.operation}")


def _write_note(context: ToolContext, args: WriteArguments) -> dict[str, Any]:
    store = _task_store(context)
    if store is None:
        return _error("STORE_UNAVAILABLE", "The note store is not available in this runtime.")
    if args.operation == "str_replace":
        return _error(
            "UNSUPPORTED_OPERATION",
            "The active note does not support str_replace. Use replace with the full note.",
        )
    if args.content is None:
        return _error("MISSING_CONTENT", f"{args.operation} requires content.")
    try:
        result = store.write_note(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            markdown=args.content,
            mode="append" if args.operation == "append" else "replace",
        )
        return {"status": "ok", "target": "note", **result}
    except Exception as exc:  # noqa: BLE001
        return _error("NOTE_WRITE_FAILED", str(exc), target="note")


async def _write_memory(context: ToolContext, args: WriteArguments) -> dict[str, Any]:
    service = _memory_service(context)
    if service is None:
        return _error("STORE_UNAVAILABLE", "Memory is not available in this runtime.")
    if not args.memory_key:
        return _error("MISSING_MEMORY_KEY", "write(target='memory') requires memory_key.")
    if args.content is None:
        return _error("MISSING_CONTENT", f"{args.operation} requires content.")
    tenant_id = str(context.auth.tenant_id)
    user_id = str(context.auth.user_id)
    conversation_id = str(context.conversation_id)
    value = args.content
    try:
        if args.operation == "append":
            existing = await service.retrieve_fact(
                tenant_id=tenant_id,
                user_id=user_id,
                key=args.memory_key,
                conversation_id=conversation_id,
            )
            if existing:
                value = f"{existing}\n{value}"
        await service.store_fact(
            tenant_id=tenant_id,
            user_id=user_id,
            key=args.memory_key,
            value=value,
            scope=args.memory_scope or "user",
            conversation_id=conversation_id,
        )
        return {
            "status": "ok",
            "target": "memory",
            "memory_key": args.memory_key,
            "message": "Memory saved.",
        }
    except Exception as exc:  # noqa: BLE001
        return _error("MEMORY_WRITE_FAILED", str(exc), target="memory")


async def _handle_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Route one write request to the capability that owns the resource."""
    args = WriteArguments(**arguments)

    if args.path or args.target == "workspace":
        return await _write_workspace(context, args)
    if args.target == "library":
        return _write_library(context, args)
    if args.target == "note":
        return _write_note(context, args)
    if args.target == "memory":
        return await _write_memory(context, args)
    return _error("INVALID_ARGUMENTS", "Specify path or target.")


# ============================================================
# Specialized filesystem handlers
# ============================================================


async def _handle_glob(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_glob(arguments, context)


async def _handle_grep(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_grep(arguments, context)


async def _handle_lock(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_lock(arguments, context)


async def _handle_unlock(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_unlock(arguments, context)


async def _handle_watch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_watch(arguments, context)


async def _handle_unwatch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _fs_unwatch(arguments, context)


async def _handle_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    args = DeleteArguments(**arguments)
    target = args.target or "workspace"
    if target == "library":
        store = _task_store(context)
        if store is None:
            return _error("STORE_UNAVAILABLE", "The Library store is not available.")
        if not args.file_id:
            return _error("MISSING_FILE_ID", "delete(target='library') requires file_id.")
        try:
            return {
                "status": "ok",
                "target": "library",
                **store.delete_workspace_file(
                    tenant_id=context.auth.tenant_id,
                    user_id=context.auth.user_id,
                    conversation_id=context.conversation_id,
                    file_id=args.file_id,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return _error("LIBRARY_DELETE_FAILED", str(exc), target="library")
    if target == "memory":
        service = _memory_service(context)
        if service is None:
            return _error("STORE_UNAVAILABLE", "Memory is not available.")
        if not args.memory_key:
            return _error("MISSING_MEMORY_KEY", "delete(target='memory') requires memory_key.")
        try:
            forgotten = await service.forget_memory(
                tenant_id=str(context.auth.tenant_id),
                user_id=str(context.auth.user_id),
                key=args.memory_key,
            )
            return {
                "status": "ok" if forgotten else "not_found",
                "target": "memory",
                "memory_key": args.memory_key,
            }
        except Exception as exc:  # noqa: BLE001
            return _error("MEMORY_DELETE_FAILED", str(exc), target="memory")
    if not args.path:
        return _error("WORKSPACE_PATH_REQUIRED", "delete(target='workspace') requires path.")
    return await _fs_delete({"path": args.path, "recursive": args.recursive}, context)


async def _handle_stat(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    args = StatArguments(**arguments)
    try:
        stat = get_filesystem_service(context).stat(args.path)
        return {"status": "ok", **dataclasses.asdict(stat)}
    except FileSystemRefusalError as exc:
        return {
            "status": "error",
            "error_code": exc.code,
            "path": exc.path,
            "message": exc.message,
            "capability_failure": True,
            "stop_turn": True,
            "no_retry": True,
            "retryable": False,
        }
    except FileSystemError as exc:
        return _error(exc.code, exc.message, path=exc.path)


# ============================================================
# Web handlers
# ============================================================


async def _handle_web_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _web_search(arguments, context)


async def _handle_web_fetch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _web_fetch(arguments, context)


async def _handle_image_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _image_read(arguments, context)


# ============================================================
# Result-reference handler
# ============================================================


async def _handle_get_tool_result(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    args = GetToolResultArguments(**arguments)
    store = context.tool_result_store
    if store is None:
        return _error("STORE_UNAVAILABLE", "The tool result store is not available.")
    result = store.get(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        ref=args.result_ref,
    )
    if result is None:
        return _error(
            "NOT_FOUND",
            "The tool result reference is missing or expired.",
            result_ref=args.result_ref,
        )
    return {"status": "ok", "result": result}


# ============================================================
# Tool definitions
# ============================================================

UNIFIED_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="read",
        description=(
            "Read one authorized resource. Use path alone for a workspace file or directory; "
            "a directory returns its immediate entries, not a recursive walk. Use target='library' "
            "to list, search, or read a Library file; target='note' for the active note; "
            "target='memory' for durable memories; or url/query for public web content. "
            "Set operation='query' with target='library' or 'memory' to search. "
            "This is the only read tool; do not look for fs_read, deep_read, or url_read."
        ),
        properties={
            "path": string_property(
                "Workspace-relative or absolute path. Files return numbered text; directories return immediate entries.",
                min_length=1,
                max_length=1024,
            ),
            "target": string_property(
                "Data store: library, memory, note, workspace, or web.",
                enum=["library", "memory", "note", "workspace", "web"],
            ),
            "url": string_property("Public HTTP(S) URL to fetch.", min_length=8, max_length=2048),
            "query": string_property(
                "Search query for Library files, memory, or the public web.",
                min_length=1,
                max_length=1000,
            ),
            "file_id": string_property("Library file ID.", max_length=80),
            "filename": string_property("Library filename.", max_length=255),
            "memory_key": string_property("Memory key.", max_length=120),
            "folder_id": string_property("Library folder ID.", max_length=80),
            "operation": string_property(
                "Operation: read, query, or search.",
                enum=["read", "query", "search"],
            ),
            "offset": integer_property("First line (1-based).", minimum=1, maximum=MAX_READ_LINES),
            "limit": integer_property("Max lines or results.", minimum=1, maximum=5000),
            "max_characters": integer_property(
                "Max characters to return.", minimum=100, maximum=MAX_READ_CHARS
            ),
            "max_results": integer_property(
                "Max search results.", minimum=1, maximum=MAX_SEARCH_RESULTS
            ),
            "domains": array_property("Restrict web results to domains.", max_items=20),
            "time_range": string_property(
                "Web time range filter.", enum=["day", "week", "month", "year"]
            ),
            "provider": string_property("Web search provider.", enum=["searxng", "tavily"]),
            "mode": string_property("Web fetch mode.", enum=["auto", "text", "image"]),
        },
        required=[],
        arguments_model=ReadArguments,
        mode="read",
        handler=_handle_read,
    ),
    define_native_tool(
        name="write",
        description=(
            "Create, replace, append, rename, or string-replace one authorized resource. "
            "Every call must specify a destination: pass 'path' for a workspace file, "
            "target='library' with 'filename' and 'content' for a Library file, target='note' "
            "for the active note, or target='memory' with 'memory_key' and 'content' for durable "
            "memory. Workspace writes return a reviewable diff. Only use this tool to persist "
            "files, notes, or memory — never call write with only 'content' just to answer in chat."
        ),
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
            "target": string_property(
                "Data store: library, memory, note, or workspace.",
                enum=["library", "memory", "note", "workspace"],
            ),
            "content": string_property("Content to write.", max_length=MAX_WRITE_CHARS),
            "operation": string_property(
                "Operation: create, replace, append, str_replace, rename, or move.",
                enum=["create", "replace", "append", "str_replace", "rename", "move"],
            ),
            "file_id": string_property(
                "Library file ID for rename, move, or str_replace.", max_length=80
            ),
            "filename": string_property(
                "Library filename, or the new name for rename.", max_length=255
            ),
            "memory_key": string_property("Memory key.", max_length=120),
            "folder_id": string_property("Library folder ID.", max_length=80),
            "memory_scope": string_property("Memory scope.", enum=["user", "session"]),
            "old_string": string_property(
                "Exact string to replace (str_replace).", max_length=100_000
            ),
            "new_string": string_property("Replacement string (str_replace).", max_length=100_000),
            "replace_all": boolean_property("Replace all occurrences (str_replace)."),
            "expected_version": string_property(
                "Expected workspace file version.", min_length=1, max_length=VERSION_LENGTH
            ),
            "new_path": string_property(
                "New workspace path for rename or move.", min_length=1, max_length=1024
            ),
            "new_folder_name": string_property(
                "New Library folder name for rename.", max_length=255
            ),
        },
        required=[],
        arguments_model=WriteArguments,
        mode="write",
        handler=_handle_write,
    ),
    define_native_tool(
        name="glob",
        description=(
            "List paths matching a pattern under the authorized search root. Without '**' the "
            "search lists the immediate directory only; use '**' to recurse."
        ),
        properties={
            "pattern": string_property(
                "Name pattern (for example *.ts or **/*.ts).", min_length=1, max_length=512
            ),
            "path": string_property("Search root path.", max_length=1024),
            "entire_system": boolean_property("Search the entire host filesystem."),
            "max_results": integer_property("Max results.", minimum=1, maximum=MAX_GLOB_RESULTS),
        },
        required=["pattern"],
        arguments_model=GlobArguments,
        mode="read",
        handler=_handle_glob,
    ),
    define_native_tool(
        name="grep",
        description="Search workspace file contents with a regular expression.",
        properties={
            "pattern": string_property("Regex pattern.", min_length=1, max_length=2000),
            "path": string_property("Search root path.", max_length=1024),
            "entire_system": boolean_property("Search the entire host filesystem."),
            "glob": string_property("File name pattern filter (for example *.py).", max_length=255),
            "output_mode": string_property(
                "Output mode.", enum=["content", "files_with_matches", "count"]
            ),
            "case_sensitive": boolean_property("Case sensitive match."),
            "max_results": integer_property("Max results.", minimum=1, maximum=MAX_GLOB_RESULTS),
        },
        required=["pattern"],
        arguments_model=GrepArguments,
        mode="read",
        handler=_handle_grep,
    ),
    define_native_tool(
        name="lock",
        description="Acquire an exclusive lock on a workspace file.",
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
            "wait_ms": integer_property("Max milliseconds to wait.", minimum=0, maximum=60_000),
        },
        required=["path"],
        arguments_model=LockArguments,
        mode="write",
        handler=_handle_lock,
    ),
    define_native_tool(
        name="unlock",
        description="Release a workspace file lock.",
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
        },
        required=["path"],
        arguments_model=UnlockArguments,
        mode="write",
        handler=_handle_unlock,
    ),
    define_native_tool(
        name="watch",
        description="Register a watch on a workspace path for change reporting.",
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
        },
        required=["path"],
        arguments_model=WatchArguments,
        mode="read",
        handler=_handle_watch,
    ),
    define_native_tool(
        name="unwatch",
        description="Stop watching a workspace path.",
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
        },
        required=["path"],
        arguments_model=UnwatchArguments,
        mode="write",
        handler=_handle_unwatch,
    ),
    define_native_tool(
        name="delete",
        description=(
            "Delete one authorized resource. Use path for a workspace file or directory, "
            "target='library' with file_id for one Library file, or target='memory' with "
            "memory_key for one durable memory. Use only on explicit user request."
        ),
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
            "target": string_property(
                "Resource kind: workspace, library, or memory.",
                enum=["workspace", "library", "memory"],
            ),
            "file_id": string_property("Library file ID.", max_length=80),
            "memory_key": string_property("Memory key.", max_length=120),
            "recursive": boolean_property("Delete a workspace directory recursively."),
        },
        required=[],
        arguments_model=DeleteArguments,
        mode="write",
        handler=_handle_delete,
    ),
    define_native_tool(
        name="stat",
        description="Get metadata for a workspace path.",
        properties={
            "path": string_property(
                "Workspace-relative or absolute path.", min_length=1, max_length=1024
            ),
        },
        required=["path"],
        arguments_model=StatArguments,
        mode="read",
        handler=_handle_stat,
    ),
    define_native_tool(
        name="web_search",
        description=(
            "Search the public web through the configured provider. Read a promising result "
            "with web_fetch before summarizing."
        ),
        properties={
            "query": string_property("Search query.", min_length=2, max_length=512),
            "max_results": integer_property("Max results.", minimum=1, maximum=MAX_SEARCH_RESULTS),
            "domains": array_property("Restrict results to domains.", max_items=20),
            "time_range": string_property("Time range.", enum=["day", "week", "month", "year"]),
            "provider": string_property("Provider.", enum=["searxng", "tavily"]),
        },
        required=["query"],
        arguments_model=WebSearchArguments,
        mode="read",
        handler=_handle_web_search,
    ),
    define_native_tool(
        name="web_fetch",
        description="Fetch a public web URL as text or an image.",
        properties={
            "url": string_property("Public HTTPS URL.", min_length=8, max_length=2048),
            "mode": string_property("Fetch mode.", enum=["auto", "text", "image"]),
            "allowed_domains": array_property("Allowed domains.", max_items=20),
        },
        required=["url"],
        arguments_model=WebFetchArguments,
        mode="read",
        handler=_handle_web_fetch,
    ),
    define_native_tool(
        name="image_read",
        description=(
            "Inspect a public raster or SVG image URL. Use read_image for a local workspace "
            "image file."
        ),
        properties={
            "url": string_property("Public HTTPS URL.", min_length=8, max_length=2048),
            "allowed_domains": array_property("Allowed domains.", max_items=20),
        },
        required=["url"],
        arguments_model=ImageReadArguments,
        mode="read",
        handler=_handle_image_read,
    ),
    define_native_tool(
        name="get_tool_result",
        description="Retrieve a bounded continuation of a previously referenced tool result.",
        properties={
            "result_ref": string_property("Result reference ID.", min_length=8, max_length=128),
        },
        required=["result_ref"],
        arguments_model=GetToolResultArguments,
        mode="read",
        handler=_handle_get_tool_result,
    ),
]

__all__ = [
    "UNIFIED_TOOLS",
    "ReadArguments",
    "WriteArguments",
    "GlobArguments",
    "GrepArguments",
    "LockArguments",
    "UnlockArguments",
    "WatchArguments",
    "UnwatchArguments",
    "DeleteArguments",
    "StatArguments",
    "WebSearchArguments",
    "WebFetchArguments",
    "ImageReadArguments",
    "GetToolResultArguments",
    "_handle_read",
    "_handle_write",
    "_handle_glob",
    "_handle_grep",
    "_handle_lock",
    "_handle_unlock",
    "_handle_watch",
    "_handle_unwatch",
    "_handle_delete",
    "_handle_stat",
    "_handle_web_search",
    "_handle_web_fetch",
    "_handle_image_read",
    "_handle_get_tool_result",
]
