"""The remote terminal tools for DeepSpace.

These tools drive a long-lived session on a reviewed remote host.  The
session keeps an ssh connection open with a pseudo-terminal on the host,
so a build, a REPL, or an interactive prompt survives between calls and
behaves the way it does for a person.

The remote terminal is not the local terminal.  It has its own registry,
its own session ids, and its own lifetime: a remote session id never
names a local session, and a remote session belongs to the tenant, user,
and conversation that opened it, exactly like a local one.  A remote
session that goes idle is closed.

A signal reaches a remote session the way it can reach a remote process
at all: an interrupt, a quit, and a stop travel through the remote
terminal's line discipline, and a termination ends the connection.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.ssh import SshError, SshHostNotFoundError, SshTrustError
from app.deepspace.services.ssh_runtime import (
    SUPPORTED_REMOTE_SIGNALS,
    SshRuntimeError,
    SshTerminalNotFoundError,
    key_directory_for,
    ssh_terminal_manager,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.tools.ssh import RUNTIME_DISABLED_MESSAGE, resolve_host

#: The message a caller receives for a session id that is not theirs.
NO_SESSION_MESSAGE = "No such remote terminal session."

#: The largest input one write may send, in characters.
MAX_WRITE_CHARS = 65_536

#: The largest output one read may return, in bytes.
MAX_READ_BYTES = 262_144


class SshTerminalOpenArguments(ToolArguments):
    """Open a persistent terminal session on a reviewed remote host."""

    host: str = Field(min_length=1, max_length=120)


class SshTerminalReadArguments(ToolArguments):
    """Read the buffered output of a remote terminal session."""

    session_id: str = Field(min_length=1, max_length=64)
    max_bytes: int | None = Field(default=None, ge=1, le=MAX_READ_BYTES)


class SshTerminalWriteArguments(ToolArguments):
    """Send input to a remote terminal session."""

    session_id: str = Field(min_length=1, max_length=64)
    data: str = Field(max_length=MAX_WRITE_CHARS)


class SshTerminalSignalArguments(ToolArguments):
    """Send a signal to a remote terminal session."""

    session_id: str = Field(min_length=1, max_length=64)
    signal: Literal[
        "SIGINT",
        "SIGTERM",
        "SIGHUP",
        "SIGQUIT",
        "SIGKILL",
        "SIGTSTP",
    ] = "SIGINT"


class SshTerminalCloseArguments(ToolArguments):
    """Close a remote terminal session."""

    session_id: str = Field(min_length=1, max_length=64)


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


def _idle_timeout(context: ToolContext) -> float | None:
    """Return the idle time after which a remote session is closed.

    Args:
        context: The calling request's tool context.

    Returns:
        The idle time in seconds, or None when the deployment sets none.
    """
    configured = getattr(context.settings, "deepspace_terminal_idle_timeout_seconds", None)
    if configured is None:
        return None
    try:
        return max(0.0, float(configured))
    except (TypeError, ValueError):
        return None


async def _ssh_terminal_open(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Open a persistent terminal session on a reviewed remote host."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        host = resolve_host(context, str(arguments["host"]))
        session = await ssh_terminal_manager().open(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            host=host,
            key_directory=key_directory_for(context.settings),
            idle_timeout_seconds=_idle_timeout(context),
        )
    except SshHostNotFoundError as exc:
        return _failure(exc)
    except SshTrustError as exc:
        return {"status": "error", "error_code": "SSH_HOST_UNTRUSTED", "message": str(exc)}
    except (SshError, SshRuntimeError) as exc:
        return {"status": "error", "error_code": "SSH_COMMAND_FAILED", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "session_id": session.id}


async def _ssh_terminal_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the buffered output of a remote terminal session."""
    try:
        result = await ssh_terminal_manager().read(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            max_bytes=int(arguments.get("max_bytes") or 16_384),
        )
    except SshTerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "output": result.output,
        "truncated": result.truncated,
        "dropped_bytes": result.dropped_bytes,
        "alive": result.alive,
    }


async def _ssh_terminal_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Send input to a remote terminal session."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    try:
        written = await ssh_terminal_manager().write(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            data=str(arguments.get("data") or ""),
        )
    except SshTerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "written_bytes": written}


async def _ssh_terminal_signal(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Send a signal to a remote terminal session."""
    refusal = _disabled(context)
    if refusal is not None:
        return refusal
    name = str(arguments.get("signal") or "SIGINT").strip().upper()
    if name not in SUPPORTED_REMOTE_SIGNALS:
        return {"status": "error", "message": f"Unsupported signal: {name}"}
    try:
        delivered = await ssh_terminal_manager().signal(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            name=name,
        )
    except SshTerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "delivered": delivered}


async def _ssh_terminal_close(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Close a remote terminal session."""
    try:
        closed = await ssh_terminal_manager().close(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "closed": closed}


SSH_TERMINAL_OPEN_DESCRIPTION = (
    "Open a persistent terminal session on a reviewed remote host and return its "
    "identifier. The session keeps a terminal open on the host, so a long build, "
    "a REPL, or an interactive prompt survives between calls."
)
SSH_TERMINAL_READ_DESCRIPTION = (
    "Read the output a remote terminal session has produced so far. The output is "
    "buffered and bounded; a read returns the oldest pending bytes."
)
SSH_TERMINAL_WRITE_DESCRIPTION = (
    "Send input to a running remote terminal session, such as a command to run or "
    "an answer to a prompt the session is waiting for."
)
SSH_TERMINAL_SIGNAL_DESCRIPTION = (
    "Send a signal to a remote terminal session. SIGINT, SIGQUIT, and SIGTSTP "
    "reach the remote foreground process group through the remote terminal; "
    "SIGTERM, SIGHUP, and SIGKILL end the connection and with it the session."
)
SSH_TERMINAL_CLOSE_DESCRIPTION = "Close a remote terminal session and end the connection it holds."

SSH_TERMINAL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="ssh_terminal_open",
        description=SSH_TERMINAL_OPEN_DESCRIPTION,
        properties={
            "host": string_property(
                "The name of the reviewed host to open the session on.",
                min_length=1,
                max_length=120,
            ),
        },
        arguments_model=SshTerminalOpenArguments,
        mode="write",
        handler=_ssh_terminal_open,
        required=("host",),
    ),
    define_native_tool(
        name="ssh_terminal_read",
        description=SSH_TERMINAL_READ_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier ssh_terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "max_bytes": integer_property(
                "The largest number of bytes to return.",
                minimum=1,
                maximum=MAX_READ_BYTES,
            ),
        },
        arguments_model=SshTerminalReadArguments,
        mode="read",
        handler=_ssh_terminal_read,
        required=("session_id",),
    ),
    define_native_tool(
        name="ssh_terminal_write",
        description=SSH_TERMINAL_WRITE_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier ssh_terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "data": string_property(
                "The input to send to the session.",
                max_length=MAX_WRITE_CHARS,
            ),
        },
        arguments_model=SshTerminalWriteArguments,
        mode="write",
        handler=_ssh_terminal_write,
        required=("session_id", "data"),
    ),
    define_native_tool(
        name="ssh_terminal_signal",
        description=SSH_TERMINAL_SIGNAL_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier ssh_terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "signal": string_property(
                "The signal to deliver to the remote session.",
                enum=list(SUPPORTED_REMOTE_SIGNALS),
            ),
        },
        arguments_model=SshTerminalSignalArguments,
        mode="write",
        handler=_ssh_terminal_signal,
        required=("session_id",),
    ),
    define_native_tool(
        name="ssh_terminal_close",
        description=SSH_TERMINAL_CLOSE_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier ssh_terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
        },
        arguments_model=SshTerminalCloseArguments,
        mode="write",
        handler=_ssh_terminal_close,
        required=("session_id",),
    ),
]

__all__ = [
    "NO_SESSION_MESSAGE",
    "SSH_TERMINAL_TOOLS",
    "SshTerminalCloseArguments",
    "SshTerminalOpenArguments",
    "SshTerminalReadArguments",
    "SshTerminalSignalArguments",
    "SshTerminalWriteArguments",
]
