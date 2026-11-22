"""Universal workspace operations for DeepSpace.

Provides unified read, find, write, edit, delete across notes, Library, memory, chat, and workspace.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)

# ============================================================
# Argument Models
# ============================================================


class UniversalReadArguments(ToolArguments):
    """Read authorized DeepSpace data without changing it."""

    target: Literal["note", "library", "memory", "chat", "project", "workspace"]
    operation: Literal["read", "query"] = "read"
    file_id: str | None = Field(default=None, max_length=80)
    filename: str | None = Field(default=None, max_length=255)
    memory_key: str | None = Field(default=None, max_length=120)
    folder_id: str | None = Field(default=None, max_length=80)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    offset: int | None = Field(default=None, ge=1, le=5_000)
    line_limit: int | None = Field(default=None, ge=1, le=5_000)
    query: str | None = Field(default=None, min_length=1, max_length=1_000)
    limit: int | None = Field(default=None, ge=1, le=20)
    max_characters: int | None = Field(default=None, ge=100, le=200_000)

    @model_validator(mode="after")
    def validate_read_operation(self) -> UniversalReadArguments:
        if self.operation == "query" and (self.target != "library" or not self.query):
            raise ValueError("read(query) requires target='library' and a query")
        if self.target == "workspace" and not (self.path or "").strip():
            raise ValueError("read(target='workspace') requires a non-empty path")
        return self


class UniversalFindArguments(ToolArguments):
    """Find authorized Library files, memories, or chat messages."""

    target: Literal["library", "memory", "chat"]
    query: str | None = Field(default=None, min_length=1, max_length=1_000)
    limit: int | None = Field(default=None, ge=1, le=1000)
    folder_id: str | None = Field(default=None, max_length=80)


class UniversalWriteArguments(ToolArguments):
    """Create or update authorized DeepSpace content."""

    target: Literal["note", "library", "memory"]
    content: str | None = Field(default=None, max_length=100_000)
    mode: Literal["replace", "append"] | None = None
    source: Literal["previous_assistant", "message"] | None = None
    source_message_id: str | None = Field(default=None, max_length=80)
    filename: str | None = Field(default=None, max_length=255)
    folder_name: str | None = Field(default=None, max_length=255)
    memory_key: str | None = Field(default=None, max_length=120)
    memory_scope: Literal["user", "session"] | None = None
    folder_id: str | None = Field(default=None, max_length=80)


class UniversalEditArguments(ToolArguments):
    """Modify an authorized note, Library file, or project workspace file."""

    target: Literal["note", "library", "workspace"]
    operation: Literal["replace", "append", "rename", "move"]
    file_id: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=100_000)
    folder_id: str | None = Field(default=None, max_length=80)
    new_folder_name: str | None = Field(default=None, max_length=255)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    new_path: str | None = Field(default=None, min_length=1, max_length=1024)


class UniversalDeleteArguments(ToolArguments):
    """Delete one authorized Library file, memory, or project workspace file."""

    target: Literal["library", "memory", "workspace"]
    file_id: str | None = Field(default=None, max_length=80)
    memory_key: str | None = Field(default=None, max_length=120)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    recursive: bool = False


# ============================================================
# Handlers
# ============================================================


async def _universal_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read data from various DeepSpace stores."""
    target = arguments["target"]
    operation = arguments.get("operation", "read")

    try:
        if target == "library":
            from app.deepspace.services.library_storage import query_library, read_library_file

            if operation == "query":
                return await query_library(
                    query=arguments["query"],
                    limit=arguments.get("limit"),
                    folder_id=arguments.get("folder_id"),
                    tenant_id=context.auth.tenant_id,
                )
            return await read_library_file(
                file_id=arguments.get("file_id"),
                filename=arguments.get("filename"),
                folder_id=arguments.get("folder_id"),
                offset=arguments.get("offset"),
                line_limit=arguments.get("line_limit"),
                max_chars=arguments.get("max_characters"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "note":
            from app.deepspace.services.library_storage import read_note

            return await read_note(
                file_id=arguments.get("file_id"),
                filename=arguments.get("filename"),
                folder_id=arguments.get("folder_id"),
                max_chars=arguments.get("max_characters"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "memory":
            from app.deepspace.services.library_storage import read_memory

            return await read_memory(
                key=arguments.get("memory_key"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        elif target == "chat":
            from app.deepspace.services.chat_service import read_chat_history

            return await read_chat_history(
                conversation_id=context.conversation_id,
                limit=arguments.get("limit"),
                offset=arguments.get("offset"),
            )
        elif target == "project":
            from app.deepspace.services.library_storage import read_project_file

            return await read_project_file(
                file_id=arguments.get("file_id"),
                path=arguments.get("path"),
                max_chars=arguments.get("max_characters"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "workspace":
            from app.deepspace.services.filesystem import read_workspace_file

            return await read_workspace_file(
                path=arguments["path"],
                max_chars=arguments.get("max_characters"),
                workspace_root=context.workspace_root,
            )
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": f"Unknown target: {target}",
        }
    except Exception as e:
        return {"status": "error", "error_code": "READ_FAILED", "message": str(e)}


async def _universal_find(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Find items in DeepSpace stores."""
    target = arguments["target"]

    try:
        if target == "library":
            from app.deepspace.services.library_storage import find_library_files

            return await find_library_files(
                query=arguments.get("query"),
                limit=arguments.get("limit"),
                folder_id=arguments.get("folder_id"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "memory":
            from app.deepspace.services.library_storage import find_memory

            return await find_memory(
                query=arguments.get("query"),
                limit=arguments.get("limit"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        elif target == "chat":
            from app.deepspace.services.chat_service import search_chat_history

            return await search_chat_history(
                conversation_id=context.conversation_id,
                query=arguments.get("query"),
                limit=arguments.get("limit"),
            )
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": f"Unknown target: {target}",
        }
    except Exception as e:
        return {"status": "error", "error_code": "FIND_FAILED", "message": str(e)}


async def _universal_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Write data to DeepSpace stores."""
    target = arguments["target"]

    try:
        if target == "library":
            from app.deepspace.services.library_storage import write_library_file

            return await write_library_file(
                content=arguments.get("content", ""),
                mode=arguments.get("mode"),
                filename=arguments.get("filename"),
                folder_id=arguments.get("folder_id"),
                folder_name=arguments.get("folder_name"),
                source=arguments.get("source"),
                source_message_id=arguments.get("source_message_id"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        elif target == "note":
            from app.deepspace.services.library_storage import write_note

            return await write_note(
                content=arguments.get("content", ""),
                mode=arguments.get("mode"),
                filename=arguments.get("filename"),
                folder_id=arguments.get("folder_id"),
                folder_name=arguments.get("folder_name"),
                source=arguments.get("source"),
                source_message_id=arguments.get("source_message_id"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        elif target == "memory":
            from app.deepspace.services.library_storage import write_memory

            return await write_memory(
                content=arguments.get("content", ""),
                key=arguments.get("memory_key"),
                scope=arguments.get("memory_scope", "user"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": f"Unknown target: {target}",
        }
    except Exception as e:
        return {"status": "error", "error_code": "WRITE_FAILED", "message": str(e)}


async def _universal_edit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Edit data in DeepSpace stores."""
    target = arguments["target"]
    operation = arguments["operation"]

    try:
        if target == "library":
            from app.deepspace.services.library_storage import edit_library_file

            return await edit_library_file(
                operation=operation,
                file_id=arguments.get("file_id"),
                name=arguments.get("name"),
                content=arguments.get("content"),
                folder_id=arguments.get("folder_id"),
                new_folder_name=arguments.get("new_folder_name"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "note":
            from app.deepspace.services.library_storage import edit_note

            return await edit_note(
                operation=operation,
                file_id=arguments.get("file_id"),
                name=arguments.get("name"),
                content=arguments.get("content"),
                folder_id=arguments.get("folder_id"),
                new_folder_name=arguments.get("new_folder_name"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "workspace":
            from app.deepspace.services.filesystem import edit_workspace_file

            return await edit_workspace_file(
                operation=operation,
                path=arguments["path"],
                new_path=arguments.get("new_path"),
                content=arguments.get("content"),
                workspace_root=context.workspace_root,
            )
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": f"Unknown target: {target}",
        }
    except Exception as e:
        return {"status": "error", "error_code": "EDIT_FAILED", "message": str(e)}


async def _universal_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delete data from DeepSpace stores."""
    target = arguments["target"]

    try:
        if target == "library":
            from app.deepspace.services.library_storage import delete_library_file

            return await delete_library_file(
                file_id=arguments.get("file_id"),
                tenant_id=context.auth.tenant_id,
            )
        elif target == "memory":
            from app.deepspace.services.library_storage import delete_memory

            return await delete_memory(
                key=arguments.get("memory_key"),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
            )
        elif target == "workspace":
            from app.deepspace.services.filesystem import delete_workspace_file

            return await delete_workspace_file(
                path=arguments["path"],
                recursive=arguments.get("recursive", False),
                workspace_root=context.workspace_root,
            )
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": f"Unknown target: {target}",
        }
    except Exception as e:
        return {"status": "error", "error_code": "DELETE_FAILED", "message": str(e)}


# ============================================================
# Tool Specs
# ============================================================

UNIVERSAL_READ_TOOL = define_native_tool(
    name="deep_read",
    description=(
        "Read authorized DeepSpace data stores (notes, library documents, memories, chat history, project configs). "
        "This is NOT for workspace files - use fs_read for that. "
        "Choose exactly one target: note, library, memory, chat, project, or workspace. "
        "For target='workspace', path is required; use a path returned by fs_glob or another authorized workspace listing. "
        "For Library documents, operation='query' searches document content with grounded passages and citations."
    ),
    properties={
        "target": string_property(
            "The data store to read from.",
            enum=["note", "library", "memory", "chat", "project", "workspace"],
        ),
        "operation": string_property(
            "Use query only with target='library' and a query string; otherwise use read.",
            enum=["read", "query"],
        ),
        "file_id": string_property("Library file ID.", max_length=80),
        "filename": string_property("Filename for note/library.", max_length=255),
        "memory_key": string_property("Memory key for memory target.", max_length=120),
        "folder_id": string_property("Folder ID for library/note.", max_length=80),
        "path": string_property(
            "Workspace-relative path. Required when target='workspace'.",
            min_length=1,
            max_length=1024,
        ),
        "offset": {"type": "integer", "minimum": 1, "maximum": 5_000},
        "line_limit": {"type": "integer", "minimum": 1, "maximum": 5_000},
        "query": string_property(
            "Search query for library query operation.", min_length=1, max_length=1_000
        ),
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "max_characters": {"type": "integer", "minimum": 100, "maximum": 200_000},
    },
    required=["target"],
    arguments_model=UniversalReadArguments,
    mode="read",
    handler=_universal_read,
)

UNIVERSAL_FIND_TOOL = define_native_tool(
    name="find",
    description=(
        "Find authorized Library files, memories, or chat messages. " "Choose exactly one target."
    ),
    properties={
        "target": string_property("The data store to search.", enum=["library", "memory", "chat"]),
        "query": string_property("Search query.", min_length=1, max_length=1_000),
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "folder_id": string_property("Folder ID for library search.", max_length=80),
    },
    required=["target"],
    arguments_model=UniversalFindArguments,
    mode="read",
    handler=_universal_find,
)

UNIVERSAL_WRITE_TOOL = define_native_tool(
    name="write",
    description=(
        "Create or update authorized DeepSpace content. "
        "Choose exactly one target: note, library, or memory."
    ),
    properties={
        "target": string_property(
            "The data store to write to.", enum=["note", "library", "memory"]
        ),
        "content": string_property("The content to write.", max_length=100_000),
        "mode": string_property("Write mode.", enum=["replace", "append"]),
        "source": string_property("Source of the content.", enum=["previous_assistant", "message"]),
        "source_message_id": string_property("Source message ID.", max_length=80),
        "filename": string_property("Filename for library/note.", max_length=255),
        "folder_name": string_property("Folder name to create/use.", max_length=255),
        "memory_key": string_property("Memory key for memory target.", max_length=120),
        "memory_scope": string_property("Memory scope.", enum=["user", "session"]),
        "folder_id": string_property("Folder ID for library/note.", max_length=80),
    },
    required=["target"],
    arguments_model=UniversalWriteArguments,
    mode="write",
    handler=_universal_write,
)

UNIVERSAL_EDIT_TOOL = define_native_tool(
    name="edit",
    description=(
        "Modify an authorized note, Library file, or project workspace file. "
        "Use an explicit operation and file id for Library changes, and a path for workspace changes."
    ),
    properties={
        "target": string_property("The data store to edit.", enum=["note", "library", "workspace"]),
        "operation": string_property(
            "The edit operation.", enum=["replace", "append", "rename", "move"]
        ),
        "file_id": string_property("File ID for library/note.", max_length=80),
        "name": string_property("New name for rename operation.", max_length=255),
        "content": string_property("New content for replace/append.", max_length=100_000),
        "folder_id": string_property("Folder ID for library/note.", max_length=80),
        "new_folder_name": string_property("New folder name for move operation.", max_length=255),
        "path": string_property(
            "Workspace-relative path. Required when target='workspace'.",
            min_length=1,
            max_length=1024,
        ),
        "new_path": string_property(
            "Workspace-relative destination path for move.", min_length=1, max_length=1024
        ),
    },
    required=["target", "operation"],
    arguments_model=UniversalEditArguments,
    mode="write",
    handler=_universal_edit,
)

UNIVERSAL_DELETE_TOOL = define_native_tool(
    name="delete",
    description=(
        "Delete one authorized Library file, memory, or project workspace file. "
        "Use only when the user explicitly requests deletion."
    ),
    properties={
        "target": string_property(
            "The data store to delete from.", enum=["library", "memory", "workspace"]
        ),
        "file_id": string_property("File ID for library.", max_length=80),
        "memory_key": string_property("Memory key for memory target.", max_length=120),
        "path": string_property(
            "Workspace-relative path. Required when target='workspace'.",
            min_length=1,
            max_length=1024,
        ),
        "recursive": {
            "type": "boolean",
            "description": "Remove a workspace directory and its contents.",
        },
    },
    required=["target"],
    arguments_model=UniversalDeleteArguments,
    mode="write",
    handler=_universal_delete,
)

UNIVERSAL_TOOLS: list[NativeToolSpec] = [
    UNIVERSAL_READ_TOOL,
    UNIVERSAL_FIND_TOOL,
    UNIVERSAL_WRITE_TOOL,
    UNIVERSAL_EDIT_TOOL,
    UNIVERSAL_DELETE_TOOL,
]

__all__ = [
    "UNIVERSAL_TOOLS",
    "UNIVERSAL_READ_TOOL",
    "UNIVERSAL_FIND_TOOL",
    "UNIVERSAL_WRITE_TOOL",
    "UNIVERSAL_EDIT_TOOL",
    "UNIVERSAL_DELETE_TOOL",
    "UniversalReadArguments",
    "UniversalFindArguments",
    "UniversalWriteArguments",
    "UniversalEditArguments",
    "UniversalDeleteArguments",
    "_universal_read",
    "_universal_find",
    "_universal_write",
    "_universal_edit",
    "_universal_delete",
]
