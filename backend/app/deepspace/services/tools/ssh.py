"""The remote command tool for DeepSpace.

One tool runs a command on a reviewed remote host.  The host must be one
the tenant added and the deployment reviewed: the host key is checked, the
key stays on the server, and the command runs in the host's reviewed
directory.

A command can also stream.  A streamed command keeps running after the
call that started it returns, and the output arrives in chunks the caller
reads while the command is still going.  The streaming sessions are
declared in ``SSH_STREAM_TOOLS`` beside the command tool they continue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from app.deepspace.services.ssh import (
    SshError,
    SshExecutor,
    SshHost,
    SshHostNotFoundError,
    SshHostStore,
    SshTrustError,
)
from app.deepspace.services.ssh_runtime import (
    SshRuntimeError,
    SshTerminalNotFoundError,
    key_directory_for,
    ssh_stream_registry,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    boolean_property,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)

#: The message a caller receives when the remote runtime is switched off.
RUNTIME_DISABLED_MESSAGE = "The remote runtime is disabled on this deployment."

#: The message a caller receives for a session id that is not theirs.
NO_SESSION_MESSAGE = "No such remote session."

#: The longest a streamed command may be waited for, in seconds.
MAX_STREAM_WAIT_SECONDS = 30

#: The time a streamed command is waited for when the caller names none.
DEFAULT_STREAM_WAIT_SECONDS = 1


class SshRunArguments(ToolArguments):
    """Run one command on a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1, max_length=8_000)
    stream: bool = False
    wait_seconds: int | None = Field(default=None, ge=0, le=MAX_STREAM_WAIT_SECONDS)


class SshHostListArguments(ToolArguments):
    """List the reviewed remote hosts."""


class SshStreamReadArguments(ToolArguments):
    """Read the output a streamed remote command has produced."""

    session_id: str = Field(min_length=1, max_length=64)
    max_bytes: int | None = Field(default=None, ge=1, le=262_144)
    wait_seconds: int | None = Field(default=None, ge=0, le=MAX_STREAM_WAIT_SECONDS)


class SshStreamCloseArguments(ToolArguments):
    """Close a streamed remote command session."""


def _key_directory(context: ToolContext) -> Path:
    """Return the directory the deployment keeps its private keys in.

    Args:
        context: The calling request's tool context.

    Returns:
        The directory.
    """
    return key_directory_for(context.settings)


def resolve_host(context: ToolContext, name: str) -> SshHost:
    """Return one reviewed host the caller may reach.

    Args:
        context: The calling request's tool context.
        name: The host's name.

    Returns:
        The host.

    Raises:
        SshHostNotFoundError: When the caller has no such host.
    """
    store = SshHostStore(require_db(context))
    return store.get(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        name=str(name or "").strip(),
    )


def _runtime_disabled(context: ToolContext) -> dict[str, Any] | None:
    """Refuse a call the deployment has switched off.

    Args:
        context: The calling request's tool context.

    Returns:
        The refusal to return instead, or None when the call may proceed.
    """
    if not bool(getattr(context.settings, "deepspace_ssh_runtime_enabled", True)):
        return {"status": "error", "message": RUNTIME_DISABLED_MESSAGE}
    return None


async def _ssh_run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one command on a reviewed remote host."""
    stream = bool(arguments.get("stream"))
    if stream:
        refusal = _runtime_disabled(context)
        if refusal is not None:
            return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
    except SshHostNotFoundError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    if stream:
        return await _run_streamed(arguments, context, host)
    executor = SshExecutor(key_directory=_key_directory(context))
    try:
        result = await executor.run(host, command=str(arguments["command"]))
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except SshError as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    return {
        "status": "ok",
        "host": host.name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
    }


async def _run_streamed(
    arguments: dict[str, Any],
    context: ToolContext,
    host: SshHost,
) -> dict[str, Any]:
    """Start a command whose output arrives while it runs.

    Args:
        arguments: The validated arguments.
        context: The calling request's tool context.
        host: The host to run the command on.

    Returns:
        The first output the command produced.
    """
    registry = ssh_stream_registry()
    wait_seconds = arguments.get("wait_seconds")
    if wait_seconds is None:
        wait_seconds = DEFAULT_STREAM_WAIT_SECONDS
    try:
        session = await registry.open(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            host=host,
            command=str(arguments["command"]),
            key_directory=_key_directory(context),
        )
        result = await registry.read(
            session.id,
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            wait_seconds=float(wait_seconds),
        )
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    return {
        "status": "ok",
        "host": host.name,
        "stream": True,
        "session_id": session.id,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "running": result.running,
    }


async def _ssh_stream_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the output a streamed remote command has produced."""
    registry = ssh_stream_registry()
    wait_seconds = arguments.get("wait_seconds")
    if wait_seconds is None:
        wait_seconds = DEFAULT_STREAM_WAIT_SECONDS
    try:
        result = await registry.read(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            max_bytes=int(arguments.get("max_bytes") or 16_384),
            wait_seconds=float(wait_seconds),
        )
    except SshTerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    return {
        "status": "ok",
        "session_id": str(arguments["session_id"]),
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "running": result.running,
    }


async def _ssh_stream_close(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Close a streamed remote command session."""
    registry = ssh_stream_registry()
    try:
        closed = await registry.close(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "closed": closed}


async def _ssh_host_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the reviewed remote hosts."""
    from app.deepspace.services.ssh import host_summary

    try:
        store = SshHostStore(require_db(context))
        hosts = store.list(tenant_id=context.auth.tenant_id, user_id=context.auth.user_id)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "hosts": [host_summary(host) for host in hosts]}


SSH_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="ssh_run",
        description=(
            "Runs a command on a reviewed remote host over SSH. The host key is checked, "
            "the command runs in the host's reviewed directory, and the output is returned."
        ),
        properties={
            "host": string_property(
                "The name of the reviewed host to run the command on.",
                min_length=1,
                max_length=120,
            ),
            "command": string_property(
                "The command to run on the host.",
                min_length=1,
                max_length=8_000,
            ),
            "stream": boolean_property(
                "Keep the command running and return the output as it arrives. The "
                "result carries a session_id; read the rest with ssh_stream_read."
            ),
            "wait_seconds": integer_property(
                "How long to wait for the first output of a streamed command.",
                minimum=0,
                maximum=MAX_STREAM_WAIT_SECONDS,
            ),
        },
        arguments_model=SshRunArguments,
        mode="write",
        handler=_ssh_run,
        required=("host", "command"),
    ),
    define_native_tool(
        name="ssh_host_list",
        description="Lists the reviewed remote hosts this conversation may reach.",
        properties={},
        arguments_model=SshHostListArguments,
        mode="read",
        handler=_ssh_host_list,
    ),
]

#: The tools that read and close a streamed remote command.
SSH_STREAM_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="ssh_stream_read",
        description=(
            "Reads the output a streamed remote command has produced so far. The "
            "command keeps running; a read returns the output that arrived since "
            "the last read."
        ),
        properties={
            "session_id": string_property(
                "The identifier a streamed ssh_run returned.",
                min_length=1,
                max_length=64,
            ),
            "max_bytes": integer_property(
                "The largest number of bytes to return.",
                minimum=1,
                maximum=262_144,
            ),
            "wait_seconds": integer_property(
                "How long to wait for more output before the read returns.",
                minimum=0,
                maximum=MAX_STREAM_WAIT_SECONDS,
            ),
        },
        arguments_model=SshStreamReadArguments,
        mode="read",
        handler=_ssh_stream_read,
        required=("session_id",),
    ),
    define_native_tool(
        name="ssh_stream_close",
        description="Closes a streamed remote command and stops the command on the host.",
        properties={
            "session_id": string_property(
                "The identifier a streamed ssh_run returned.",
                min_length=1,
                max_length=64,
            ),
        },
        arguments_model=SshStreamCloseArguments,
        mode="write",
        handler=_ssh_stream_close,
        required=("session_id",),
    ),
]

__all__ = [
    "NO_SESSION_MESSAGE",
    "RUNTIME_DISABLED_MESSAGE",
    "SSH_STREAM_TOOLS",
    "SSH_TOOLS",
    "SshHostListArguments",
    "SshRunArguments",
    "SshStreamCloseArguments",
    "SshStreamReadArguments",
    "resolve_host",
]
