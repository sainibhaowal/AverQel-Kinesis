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


class SshFsDeleteArguments(ToolArguments):
    """Remove a file from a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


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
        path, entries = await _runtime(context).list_directory(host, str(arguments["path"]))
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
        "path": path,
        "entries": [
            {"name": entry.name, "kind": entry.kind, "size_bytes": entry.size_bytes}
            for entry in entries
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
]

__all__ = [
    "SSH_FS_TOOLS",
    "SshFsDeleteArguments",
    "SshFsListArguments",
    "SshFsReadArguments",
    "SshFsWriteArguments",
]
