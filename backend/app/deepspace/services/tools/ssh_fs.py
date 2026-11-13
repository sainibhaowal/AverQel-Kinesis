"""The remote file system tools for DeepSpace.

These tools read, write, list, and remove files on a reviewed remote host.
They are the remote counterpart to the workspace file tools: the same
names, the same arguments, and the same result fields, with the workspace
root replaced by the host's reviewed remote directory.

Every path is resolved *on the host*, inside that directory, and a path
that leaves it is refused there.  The resolution happens on the host
because only the host can see the links: a link inside the directory that
points outside it is followed and refused on the host, not here.

The transfers use the same trust rules as the command runner: the reviewed
host key is checked, the private key never leaves the server, and the
client process receives the reduced environment.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic import Field

from app.deepspace.services.ssh import SshError, SshHostNotFoundError, SshTrustError
from app.deepspace.services.ssh_runtime import (
    RemoteNotFoundError,
    RemotePathRefusedError,
    SshRuntime,
    SshRuntimeError,
    key_directory_for,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.tools.ssh import RUNTIME_DISABLED_MESSAGE, resolve_host

#: The largest path one call may name, in characters.
MAX_PATH_LENGTH = 1024

#: The largest file one read may return, in characters.
MAX_READ_CHARS = 200_000

#: The largest file one write may create, in characters.
MAX_WRITE_CHARS = 400_000


class SshFsReadArguments(ToolArguments):
    """Read a text file from a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


class SshFsWriteArguments(ToolArguments):
    """Write a text file on a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    content: str = Field(max_length=MAX_WRITE_CHARS)


class SshFsListArguments(ToolArguments):
    """List a directory on a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    offset: int = Field(default=0, ge=0, le=10_000)
    limit: int = Field(default=200, ge=1, le=1_000)


class SshFsDeleteArguments(ToolArguments):
    """Remove a file from a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


class SshFsStatArguments(ToolArguments):
    """Inspect metadata for a remote path."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    follow_symlinks: bool = True


class SshFsReadRangeArguments(ToolArguments):
    """Read a bounded binary-safe range from a remote file."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    offset: int = Field(default=0, ge=0, le=2_000_000_000)
    max_bytes: int = Field(default=64_000, ge=1, le=180_000)


class SshFsReadTextArguments(ToolArguments):
    """Read bounded UTF-8 text chunks from a remote file."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    offset: int = Field(default=0, ge=0, le=2_000_000_000)
    max_bytes: int = Field(default=64_000, ge=1, le=180_000)


class SshFsWriteBytesArguments(ToolArguments):
    """Write bounded base64 data to a remote file."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    content_base64: str = Field(min_length=4, max_length=1_333_336)


class SshFsMoveArguments(ToolArguments):
    """Move one remote file or directory within the reviewed root."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    destination: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


class SshFsEditArguments(ToolArguments):
    """Replace exact text after checking the current remote file hash."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_text: str = Field(min_length=1, max_length=MAX_WRITE_CHARS)
    new_text: str = Field(max_length=MAX_WRITE_CHARS)
    expected_occurrences: int = Field(default=1, ge=1, le=1_000)


class SshFsPatchArguments(ToolArguments):
    """Apply exact line changes after checking the current remote file hash."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def _runtime(context: ToolContext) -> SshRuntime:
    """Return the remote runtime for the calling deployment.

    Args:
        context: The calling request's tool context.

    Returns:
        The runtime.
    """
    return SshRuntime(key_directory=key_directory_for(context.settings))


def _disabled(context: ToolContext) -> dict[str, Any] | None:
    """Refuse a call the deployment has switched off.

    Args:
        context: The calling request's tool context.

    Returns:
        The refusal to return instead, or None when the call may proceed.
    """
    if not bool(getattr(context.settings, "deepspace_ssh_runtime_enabled", True)):
        return {"status": "error", "message": RUNTIME_DISABLED_MESSAGE}
    return None


def _failure(exc: Exception) -> dict[str, Any]:
    """Return the result of a failed tool call.

    Args:
        exc: The failure the call ran into.

    Returns:
        The error result the model receives.
    """
    return {"status": "error", "message": str(exc)}


async def _ssh_fs_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a text file from a reviewed remote host."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        remote = await _runtime(context).read_file(host, str(arguments["path"]))
    except SshHostNotFoundError as exc:
        return _failure(exc)
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except RemotePathRefusedError as exc:
        return {"status": "error", "error_code": "SSH_PATH_REFUSED", "message": str(exc)}
    except RemoteNotFoundError as exc:
        return {"status": "error", "error_code": "SSH_NOT_FOUND", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    content = remote.content[:MAX_READ_CHARS]
    return {
        "status": "ok",
        "path": remote.path,
        "content": content,
        "size_bytes": remote.size_bytes,
        "sha256": remote.sha256,
        "truncated": len(remote.content) > MAX_READ_CHARS,
    }


async def _ssh_fs_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Write a text file on a reviewed remote host."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        remote = await _runtime(context).write_file(
            host,
            str(arguments["path"]),
            str(arguments.get("content") or ""),
        )
    except SshHostNotFoundError as exc:
        return _failure(exc)
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except RemotePathRefusedError as exc:
        return {"status": "error", "error_code": "SSH_PATH_REFUSED", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "path": remote.path,
        "size_bytes": remote.size_bytes,
    }


async def _ssh_fs_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List a directory on a reviewed remote host."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        page = await _runtime(context).list_directory_page(
            host,
            str(arguments["path"]),
            offset=int(arguments.get("offset", 0)),
            limit=int(arguments.get("limit", 200)),
        )
    except SshHostNotFoundError as exc:
        return _failure(exc)
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except RemotePathRefusedError as exc:
        return {"status": "error", "error_code": "SSH_PATH_REFUSED", "message": str(exc)}
    except RemoteNotFoundError as exc:
        return {"status": "error", "error_code": "SSH_NOT_FOUND", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "path": page["path"],
        "offset": page["offset"],
        "next_offset": page["next_offset"],
        "total_entries": page["total_entries"],
        "truncated": page["truncated"],
        "entries": [
            {"name": entry.name, "kind": entry.kind, "size_bytes": entry.size_bytes}
            for entry in page["entries"]
        ],
    }


async def _ssh_fs_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Remove a file from a reviewed remote host."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        path = await _runtime(context).delete_file(host, str(arguments["path"]))
    except SshHostNotFoundError as exc:
        return _failure(exc)
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except RemotePathRefusedError as exc:
        return {"status": "error", "error_code": "SSH_PATH_REFUSED", "message": str(exc)}
    except RemoteNotFoundError as exc:
        return {"status": "error", "error_code": "SSH_NOT_FOUND", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "path": path, "deleted": True}


async def _ssh_fs_stat(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        info = await _runtime(context).stat_path(
            host,
            str(arguments["path"]),
            follow_symlinks=bool(arguments.get("follow_symlinks", True)),
        )
        return {"status": "ok", "path": info.path, "kind": info.kind, "size_bytes": info.size_bytes}
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - the tool boundary returns failures as data.
        return _failure(exc)


async def _ssh_fs_read_range(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        result = await _runtime(context).read_range(
            host,
            str(arguments["path"]),
            offset=int(arguments.get("offset", 0)),
            max_bytes=int(arguments.get("max_bytes", 64_000)),
        )
        return {
            "status": "ok",
            "path": result["path"],
            "offset": result["offset"],
            "total_bytes": result["total_bytes"],
            "content_base64": base64.b64encode(result["content"]).decode("ascii"),
            "sha256": result["sha256"],
            "next_offset": result["next_offset"],
        }
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def _ssh_fs_read_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read UTF-8-safe text chunks and expose their continuation offset."""
    result = await _ssh_fs_read_range(arguments, context)
    if result.get("status") != "ok":
        return result
    chunk = base64.b64decode(str(result.pop("content_base64")), validate=True)
    start = 0
    end = len(chunk)
    while start < end and chunk[start] & 0xC0 == 0x80:
        start += 1
    while end > start:
        candidate = chunk[start:end]
        try:
            content = candidate.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(candidate):
                return {
                    "status": "binary",
                    "message": "The selected range is not valid UTF-8 text; use ssh_fs_read_range.",
                }
            end -= 1
    else:
        content = ""
    offset = int(arguments.get("offset", 0))
    next_offset = offset + end
    result["offset"] = offset + start
    result["content"] = content
    result["next_offset"] = next_offset if next_offset < int(result["total_bytes"]) else None
    return result


async def _ssh_fs_write_bytes(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        content = base64.b64decode(str(arguments["content_base64"]), validate=True)
        result = await _runtime(context).write_bytes(host, str(arguments["path"]), content)
        return {"status": "ok", "path": result.path, "size_bytes": result.size_bytes}
    except (ValueError, binascii.Error):
        return {"status": "error", "message": "content_base64 must be valid base64 text."}
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def _ssh_fs_move(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        source, destination = await _runtime(context).move_file(
            host, str(arguments["path"]), str(arguments["destination"])
        )
        return {"status": "ok", "path": source, "destination": destination}
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def _ssh_fs_edit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        result = await _runtime(context).edit_file(
            host,
            str(arguments["path"]),
            expected_sha256=str(arguments["expected_sha256"]),
            old_text=str(arguments["old_text"]),
            new_text=str(arguments.get("new_text") or ""),
            expected_occurrences=int(arguments.get("expected_occurrences", 1)),
        )
        return {"status": "ok", **result}
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def _ssh_fs_patch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        result = await _runtime(context).patch_file(
            host,
            str(arguments["path"]),
            expected_sha256=str(arguments["expected_sha256"]),
            changes=list(arguments["changes"]),
        )
        return {"status": "ok", **result}
    except (SshError, SshRuntimeError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


SSH_FS_READ_DESCRIPTION = (
    "Read a text file from a reviewed remote host. The path is relative to the "
    "host's reviewed directory; a path that leaves it is refused."
)
SSH_FS_WRITE_DESCRIPTION = (
    "Write a text file on a reviewed remote host. The file is written to a "
    "temporary name and renamed over the target, so a reader never sees half a "
    "file. The path is relative to the host's reviewed directory."
)
SSH_FS_LIST_DESCRIPTION = (
    "List a directory on a reviewed remote host. The result carries one entry "
    "per file, with the entry's name, its kind, and its size in bytes. The path "
    "is relative to the host's reviewed directory."
)
SSH_FS_DELETE_DESCRIPTION = (
    "Remove a file from a reviewed remote host. The path is relative to the "
    "host's reviewed directory."
)
SSH_FS_STAT_DESCRIPTION = "Inspect a file or directory on a reviewed remote host. Paths stay inside the host's reviewed directory."
SSH_FS_READ_RANGE_DESCRIPTION = (
    "Read up to 180,000 raw bytes from a remote file at a byte offset. The result is base64, "
    "so binary data is preserved. Continue with next_offset when present."
)
SSH_FS_READ_TEXT_DESCRIPTION = (
    "Read up to 180,000 bytes from a UTF-8 remote text file. The response gives a continuation offset; "
    "repeat with that offset to read the next text chunk. Use ssh_fs_read_range for binary files."
)
SSH_FS_WRITE_BYTES_DESCRIPTION = "Write up to 1 MB of binary data encoded as base64 to a file inside the reviewed remote directory."
SSH_FS_MOVE_DESCRIPTION = "Move a remote file or directory to a destination inside the reviewed directory. The destination must not exist."
SSH_FS_EDIT_DESCRIPTION = (
    "Replace exact UTF-8 text in a remote file only when expected_sha256 still matches. "
    "Read the file first and use its returned sha256."
)
SSH_FS_PATCH_DESCRIPTION = (
    "Apply one or more exact, one-based line changes to a UTF-8 remote file only when its expected_sha256 matches. "
    "Read the file first and use its returned sha256. Each change has start_line, delete_lines, and insert_lines."
)

SSH_FS_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="ssh_fs_read",
        description=SSH_FS_READ_DESCRIPTION,
        properties={
            "host": string_property(
                "The name of the reviewed host to read on.",
                min_length=1,
                max_length=120,
            ),
            "path": string_property(
                "The path of the file, relative to the host's reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "offset": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1_000},
        },
        arguments_model=SshFsReadArguments,
        mode="read",
        handler=_ssh_fs_read,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_write",
        description=SSH_FS_WRITE_DESCRIPTION,
        properties={
            "host": string_property(
                "The name of the reviewed host to write on.",
                min_length=1,
                max_length=120,
            ),
            "path": string_property(
                "The path of the file, relative to the host's reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "content": string_property("The full text to write.", max_length=MAX_WRITE_CHARS),
        },
        arguments_model=SshFsWriteArguments,
        mode="write",
        handler=_ssh_fs_write,
        required=("host", "path", "content"),
    ),
    define_native_tool(
        name="ssh_fs_list",
        description=SSH_FS_LIST_DESCRIPTION,
        properties={
            "host": string_property(
                "The name of the reviewed host to list on.",
                min_length=1,
                max_length=120,
            ),
            "path": string_property(
                "The path of the directory, relative to the host's reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "offset": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1_000},
        },
        arguments_model=SshFsListArguments,
        mode="read",
        handler=_ssh_fs_list,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_delete",
        description=SSH_FS_DELETE_DESCRIPTION,
        properties={
            "host": string_property(
                "The name of the reviewed host to remove the file from.",
                min_length=1,
                max_length=120,
            ),
            "path": string_property(
                "The path of the file, relative to the host's reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
        },
        arguments_model=SshFsDeleteArguments,
        mode="write",
        handler=_ssh_fs_delete,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_stat",
        description=SSH_FS_STAT_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The path inside the reviewed directory.", min_length=1, max_length=MAX_PATH_LENGTH
            ),
            "follow_symlinks": {
                "type": "boolean",
                "description": "Resolve the final symbolic link before inspecting its target.",
            },
        },
        arguments_model=SshFsStatArguments,
        mode="read",
        handler=_ssh_fs_stat,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_read_range",
        description=SSH_FS_READ_RANGE_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The file path inside the reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "offset": {"type": "integer", "minimum": 0, "maximum": 2_000_000_000},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 180_000},
        },
        arguments_model=SshFsReadRangeArguments,
        mode="read",
        handler=_ssh_fs_read_range,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_read_text",
        description=SSH_FS_READ_TEXT_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The UTF-8 text file inside the reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "offset": {"type": "integer", "minimum": 0, "maximum": 2_000_000_000},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 180_000},
        },
        arguments_model=SshFsReadTextArguments,
        mode="read",
        handler=_ssh_fs_read_text,
        required=("host", "path"),
    ),
    define_native_tool(
        name="ssh_fs_write_bytes",
        description=SSH_FS_WRITE_BYTES_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The file path inside the reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "content_base64": string_property(
                "The file bytes encoded as base64.", min_length=4, max_length=1_333_336
            ),
        },
        arguments_model=SshFsWriteBytesArguments,
        mode="write",
        handler=_ssh_fs_write_bytes,
        required=("host", "path", "content_base64"),
    ),
    define_native_tool(
        name="ssh_fs_move",
        description=SSH_FS_MOVE_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The source path inside the reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
            "destination": string_property(
                "A new destination path inside the reviewed directory.",
                min_length=1,
                max_length=MAX_PATH_LENGTH,
            ),
        },
        arguments_model=SshFsMoveArguments,
        mode="write",
        handler=_ssh_fs_move,
        required=("host", "path", "destination"),
    ),
    define_native_tool(
        name="ssh_fs_edit",
        description=SSH_FS_EDIT_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The path inside the reviewed directory.", min_length=1, max_length=MAX_PATH_LENGTH
            ),
            "expected_sha256": string_property(
                "The file SHA-256 from its latest read.", min_length=64, max_length=64
            ),
            "old_text": string_property(
                "The exact nonempty text to replace.", min_length=1, max_length=MAX_WRITE_CHARS
            ),
            "new_text": string_property("The replacement text.", max_length=MAX_WRITE_CHARS),
            "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 1_000},
        },
        arguments_model=SshFsEditArguments,
        mode="write",
        handler=_ssh_fs_edit,
        required=("host", "path", "expected_sha256", "old_text", "new_text"),
    ),
    define_native_tool(
        name="ssh_fs_patch",
        description=SSH_FS_PATCH_DESCRIPTION,
        properties={
            "host": string_property("The reviewed remote host.", min_length=1, max_length=120),
            "path": string_property(
                "The path inside the reviewed directory.", min_length=1, max_length=MAX_PATH_LENGTH
            ),
            "expected_sha256": string_property(
                "The file SHA-256 from its latest read.", min_length=64, max_length=64
            ),
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer", "minimum": 1},
                        "delete_lines": {"type": "integer", "minimum": 0},
                        "insert_lines": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["start_line", "delete_lines", "insert_lines"],
                    "additionalProperties": False,
                },
            },
        },
        arguments_model=SshFsPatchArguments,
        mode="write",
        handler=_ssh_fs_patch,
        required=("host", "path", "expected_sha256", "changes"),
    ),
]

__all__ = [
    "SSH_FS_TOOLS",
    "SshFsDeleteArguments",
    "SshFsListArguments",
    "SshFsReadArguments",
    "SshFsStatArguments",
    "SshFsReadRangeArguments",
    "SshFsReadTextArguments",
    "SshFsMoveArguments",
    "SshFsEditArguments",
    "SshFsPatchArguments",
    "SshFsWriteArguments",
    "SshFsWriteBytesArguments",
]
