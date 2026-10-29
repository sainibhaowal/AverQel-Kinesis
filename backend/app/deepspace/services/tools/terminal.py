"""Persistent terminal tools for DeepSpace.

These tools drive a long-lived child process the agent can write to and read
from across several calls, so a build, a REPL, or an interactive prompt
survives between turns.  The session layer lives in
:mod:`app.deepspace.services.terminals`; this module declares the
model-facing tools over it.

A session belongs to the conversation that opened it.  A session id that
belongs to another conversation is reported as missing, so a model cannot
learn that another tenant's session exists.

The session tools are declared in ``TERMINAL_TOOLS``, the signal tool in
``TERMINAL_SIGNAL_TOOLS``, and the reader for the background writes in
``TERMINAL_JOB_TOOLS``, so a deployment can register them beside the session
tools it already offers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from pydantic import Field

from app.deepspace.services.terminals import (
    DEFAULT_READ_BYTES,
    MAX_READ_BYTES,
    MAX_WAIT_SECONDS,
    SUPPORTED_SIGNALS,
    ShellKind,
    TerminalError,
    TerminalManager,
    TerminalNotFoundError,
    signal_number,
    terminal_manager,
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
from app.deepspace.services.tools.shell import _effective_mode
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    WorkspaceUnavailableError,
)

#: The message a caller receives for a session id that is not theirs.
NO_SESSION_MESSAGE = "No such terminal session."

#: The message a caller receives for a job id that is not theirs.
NO_JOB_MESSAGE = "No job with that id belongs to this conversation."

#: The message a caller receives when the conversation may not change anything.
READ_ONLY_MESSAGE = "This conversation runs in read-only mode and may not change anything."

#: The message a caller receives when the deployment has no local shell.
SHELL_DISABLED_MESSAGE = "The local shell tools are disabled on this deployment."

#: The largest input one write may send, in characters.
MAX_WRITE_CHARS = 65_536


class TerminalOpenArguments(ToolArguments):
    """Open a persistent terminal session."""

    shell: Literal["bash", "pwsh", "cmd"] = "bash"
    cwd: str | None = Field(default=None, max_length=1024)
    command: str | None = Field(default=None, max_length=20_000)


class TerminalReadArguments(ToolArguments):
    """Read the buffered output of a terminal session."""

    session_id: str = Field(min_length=1, max_length=64)
    max_bytes: int | None = Field(default=None, ge=1, le=MAX_READ_BYTES)


class TerminalWriteArguments(ToolArguments):
    """Send input to a terminal session."""

    session_id: str = Field(min_length=1, max_length=64)
    data: str = Field(max_length=MAX_WRITE_CHARS)
    background: bool = False
    wait_seconds: int = Field(default=0, ge=0, le=MAX_WAIT_SECONDS)


class TerminalCloseArguments(ToolArguments):
    """Close a terminal session."""

    session_id: str = Field(min_length=1, max_length=64)


class TerminalSignalArguments(ToolArguments):
    """Send a signal to a terminal session's foreground process group."""

    session_id: str = Field(min_length=1, max_length=64)
    signal: Literal[
        "SIGINT",
        "SIGTERM",
        "SIGHUP",
        "SIGQUIT",
        "SIGKILL",
        "SIGTSTP",
        "SIGCONT",
    ] = "SIGINT"


class TerminalListArguments(ToolArguments):
    """List the conversation's live terminal sessions."""


class TerminalJobArguments(ToolArguments):
    """Read one background terminal write."""

    job_id: str = Field(min_length=1, max_length=64)


def _failure(exc: Exception) -> dict[str, Any]:
    """Return the result of a failed tool call.

    Args:
        exc: The failure the call ran into.

    Returns:
        The error result the model receives.
    """
    return {"status": "error", "message": str(exc)}


def _read_only_refusal(context: ToolContext) -> dict[str, Any] | None:
    """Refuse a tool call that would change the conversation's state.

    Args:
        context: The calling request's tool context.

    Returns:
        The refusal to return instead, or None when the call may proceed.
    """
    if _effective_mode(context) == "read-only":
        return {"status": "error", "message": READ_ONLY_MESSAGE}
    return None


def _shell_disabled(context: ToolContext) -> dict[str, Any] | None:
    """Refuse a tool call on a deployment without local shell tools.

    Args:
        context: The calling request's tool context.

    Returns:
        The refusal to return instead, or None when the call may proceed.
    """
    if not bool(getattr(context.settings, "deepspace_shell_enabled", True)):
        return {"status": "error", "message": SHELL_DISABLED_MESSAGE}
    return None


def _resolve_signal(raw: Any) -> tuple[str, int | None]:
    """Return the name and the number of the signal a caller asked for.

    Args:
        raw: The signal name the caller supplied.

    Returns:
        The normalized name and its number, or None when the host has no
        such signal.
    """
    name = str(raw or "SIGINT").strip().upper()
    if not name.startswith("SIG"):
        name = f"SIG{name}"
    return name, signal_number(name)


async def _wait_reason(
    manager: TerminalManager,
    session_id: str,
    context: ToolContext,
    wait_seconds: int,
) -> str | None:
    """Return the reason a write settled with, when the manager reports one.

    A manager that observes a session's foreground state reports the reason
    with every write.  A manager that only carries the written bytes reports
    no reason, and the write result then carries none.

    Args:
        manager: The manager that ran the write.
        session_id: The session the write went to.
        context: The calling request's tool context.
        wait_seconds: How long the write waits for the session to settle.

    Returns:
        The reason, or None when the manager reports none.
    """
    reporter = cast(
        "Callable[..., Awaitable[str | None]] | None",
        getattr(manager, "wait_for_reason", None),
    )
    if reporter is None:
        return None
    return await reporter(
        session_id,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        wait_seconds=wait_seconds,
    )


async def _terminal_open(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Open a persistent terminal session."""
    refusal = _read_only_refusal(context) or _shell_disabled(context)
    if refusal is not None:
        return refusal
    raw_shell = str(arguments.get("shell") or "bash")
    if raw_shell not in {"bash", "pwsh", "cmd"}:
        return {"status": "error", "message": f"Unknown shell: {raw_shell}"}
    shell = cast(ShellKind, raw_shell)
    manager = terminal_manager()
    try:
        session = await manager.open(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            settings=context.settings,
            shell=shell,
            cwd=arguments.get("cwd"),
            command=arguments.get("command"),
            workspace_root=context.workspace_root,
        )
    except (TerminalError, WorkspacePathError, WorkspaceUnavailableError) as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "session_id": session.id,
        "shell": session.shell,
        "pty": session.pty,
        "output": session.process.peek(DEFAULT_READ_BYTES),
    }


async def _terminal_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the buffered output of a terminal session."""
    manager = terminal_manager()
    try:
        result = await manager.read(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            max_bytes=int(arguments.get("max_bytes") or DEFAULT_READ_BYTES),
        )
    except TerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "output": result.output,
        "truncated": result.truncated,
        "dropped_bytes": result.dropped_bytes,
        "alive": result.alive,
    }


async def _terminal_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Send input to a terminal session."""
    refusal = _read_only_refusal(context)
    if refusal is not None:
        return refusal
    manager = terminal_manager()
    session_id = str(arguments["session_id"])
    data = str(arguments.get("data") or "")
    background = bool(arguments.get("background"))
    wait_seconds = int(arguments.get("wait_seconds") or 0)
    try:
        if background:
            job = await manager.start_background_write(
                session_id,
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                data=data,
            )
            return {
                "status": "ok",
                "session_id": session_id,
                "background": True,
                "job_id": job.job_id,
            }
        written = await manager.write(
            session_id,
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            data=data,
        )
        wait_reason = await _wait_reason(manager, session_id, context, wait_seconds)
    except TerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    result: dict[str, Any] = {"status": "ok", "written_bytes": written}
    if wait_reason is not None:
        result["wait_reason"] = wait_reason
    return result


async def _terminal_signal(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Send a signal to a terminal session's foreground process group."""
    refusal = _read_only_refusal(context)
    if refusal is not None:
        return refusal
    manager = terminal_manager()
    session_id = str(arguments["session_id"])
    name, number = _resolve_signal(arguments.get("signal"))
    if number is None:
        return {"status": "error", "message": f"Unsupported signal: {name}"}
    try:
        delivered = await manager.signal(
            session_id,
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            sig=number,
        )
    except TerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "session_id": session_id,
        "signal": name,
        "delivered": delivered,
    }


async def _terminal_job(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one background terminal write."""
    manager = terminal_manager()
    try:
        job = manager.job(
            str(arguments["job_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    if job is None:
        return {"status": "error", "message": NO_JOB_MESSAGE}
    return {
        "status": "ok",
        "job": {
            "job_id": job.job_id,
            "session_id": job.session_id,
            "status": job.status,
            "written_bytes": job.written_bytes,
            "wait_reason": job.wait_reason,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        },
    }


async def _terminal_close(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Close a terminal session."""
    manager = terminal_manager()
    try:
        manager.get(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except TerminalNotFoundError:
        return {"status": "error", "message": NO_SESSION_MESSAGE}
    try:
        closed = await manager.close(
            str(arguments["session_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {"status": "ok", "closed": closed}


async def _terminal_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the conversation's live terminal sessions."""
    manager = terminal_manager()
    try:
        await manager.sweep()
        sessions = manager.list(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except TerminalError as exc:
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - a tool call never raises out of its handler.
        return _failure(exc)
    return {
        "status": "ok",
        "sessions": [
            {
                "session_id": session.id,
                "shell": session.shell,
                "pty": session.pty,
                "created_at": session.created_at.isoformat(),
                "idle_seconds": round(session.idle_seconds(), 3),
            }
            for session in sessions
        ],
    }


TERMINAL_OPEN_DESCRIPTION = (
    "Open a persistent terminal session in the project workspace and return its "
    "identifier. The session keeps running between calls, so a long build, a "
    "REPL, or an interactive prompt survives. Pass a command to run at startup, "
    "or omit it to get an interactive shell."
)
TERMINAL_READ_DESCRIPTION = (
    "Read the output a terminal session has produced so far. The output is "
    "buffered and bounded; a read returns the oldest pending bytes."
)
TERMINAL_WRITE_DESCRIPTION = (
    "Send input to a running terminal session, such as a command to run or an "
    "answer to a prompt the session is waiting for. The result carries a "
    "wait_reason: 'sent' when the input was delivered, 'stdin_read' when the "
    "session asked for more input, 'inferred_idle' when the session went "
    "quiet, 'timeout' when the wait ran out, and 'session_exit' when the "
    "session ended. Pass wait_seconds to wait for that reason, and pass "
    "background to run a long write as a job that returns a job id."
)
TERMINAL_CLOSE_DESCRIPTION = "Close a terminal session and stop the process it runs."
TERMINAL_LIST_DESCRIPTION = "List this conversation's live terminal sessions."
TERMINAL_SIGNAL_DESCRIPTION = (
    "Send a signal to the foreground process group of a running terminal "
    "session, such as SIGINT to interrupt a command that hangs or SIGTERM to "
    "stop it. The signal reaches the command the session runs and the shell "
    "that started it."
)
TERMINAL_JOB_DESCRIPTION = (
    "Read the outcome of a background terminal write, such as the wait reason it settled with."
)

TERMINAL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="terminal_open",
        description=TERMINAL_OPEN_DESCRIPTION,
        properties={
            "shell": string_property(
                "The shell to run.",
                enum=["bash", "pwsh", "cmd"],
            ),
            "cwd": string_property(
                "Workspace-relative directory to run in. Defaults to the workspace root.",
                min_length=1,
                max_length=1024,
            ),
            "command": string_property(
                "The command to run at startup. Omit it for an interactive shell.",
                min_length=1,
                max_length=20_000,
            ),
        },
        arguments_model=TerminalOpenArguments,
        mode="write",
        handler=_terminal_open,
    ),
    define_native_tool(
        name="terminal_read",
        description=TERMINAL_READ_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "max_bytes": integer_property(
                "The largest number of bytes to return.",
                minimum=1,
                maximum=MAX_READ_BYTES,
            ),
        },
        required=["session_id"],
        arguments_model=TerminalReadArguments,
        mode="read",
        handler=_terminal_read,
    ),
    define_native_tool(
        name="terminal_write",
        description=TERMINAL_WRITE_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "data": string_property(
                "The input to send to the session.",
                max_length=MAX_WRITE_CHARS,
            ),
            "background": boolean_property(
                "Run the write as a background job and return a job id instead of "
                "waiting for the session."
            ),
            "wait_seconds": integer_property(
                "How long to wait for the session to settle before the write "
                "reports its wait_reason.",
                minimum=0,
                maximum=MAX_WAIT_SECONDS,
            ),
        },
        required=["session_id", "data"],
        arguments_model=TerminalWriteArguments,
        mode="write",
        handler=_terminal_write,
    ),
    define_native_tool(
        name="terminal_close",
        description=TERMINAL_CLOSE_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
        },
        required=["session_id"],
        arguments_model=TerminalCloseArguments,
        mode="write",
        handler=_terminal_close,
    ),
    define_native_tool(
        name="terminal_list",
        description=TERMINAL_LIST_DESCRIPTION,
        properties={},
        arguments_model=TerminalListArguments,
        mode="read",
        handler=_terminal_list,
    ),
]

#: The signal tool, declared apart from the session tools it interrupts.
TERMINAL_SIGNAL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="terminal_signal",
        description=TERMINAL_SIGNAL_DESCRIPTION,
        properties={
            "session_id": string_property(
                "The identifier terminal_open returned.",
                min_length=1,
                max_length=64,
            ),
            "signal": string_property(
                "The signal to deliver to the session's foreground process group.",
                enum=list(SUPPORTED_SIGNALS),
            ),
        },
        required=["session_id"],
        arguments_model=TerminalSignalArguments,
        mode="write",
        handler=_terminal_signal,
    ),
]

#: The reader for the background writes, declared apart from the session tools.
TERMINAL_JOB_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="terminal_job",
        description=TERMINAL_JOB_DESCRIPTION,
        properties={
            "job_id": string_property(
                "The id the background terminal write returned.",
                min_length=1,
                max_length=64,
            ),
        },
        required=["job_id"],
        arguments_model=TerminalJobArguments,
        mode="read",
        handler=_terminal_job,
    ),
]

__all__ = [
    "NO_JOB_MESSAGE",
    "NO_SESSION_MESSAGE",
    "READ_ONLY_MESSAGE",
    "TERMINAL_JOB_TOOLS",
    "TERMINAL_SIGNAL_TOOLS",
    "TERMINAL_TOOLS",
    "TerminalCloseArguments",
    "TerminalJobArguments",
    "TerminalListArguments",
    "TerminalOpenArguments",
    "TerminalReadArguments",
    "TerminalSignalArguments",
    "TerminalWriteArguments",
]
