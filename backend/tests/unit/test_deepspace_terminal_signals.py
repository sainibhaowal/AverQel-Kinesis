"""Tests for the DeepSpace terminal signal, background write, and wait reason.

The checks cover the signal a session's process group receives, the reason a
write settles with, the job a background write runs in, and the refusal a
session id that belongs to somebody else receives.  The session checks run
against real child processes, so the reasons are the ones a live session
produces.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.services.terminals import (
    TerminalManager,
    TerminalSession,
    WaitObservation,
    classify_wait,
)
from app.deepspace.services.tools import terminal as terminal_tools
from app.deepspace.services.tools._base import ToolContext, ToolHandler

pytestmark = pytest.mark.unit_no_db

#: The exact input-wait probe reads the kernel's record of a blocked call.
EXACT_WAIT_AVAILABLE = Path("/proc/self/syscall").exists()


def _context(tmp_path: Path, **overrides: Any) -> ToolContext:
    """Build a tool context for the terminal tools.

    Args:
        tmp_path: The test's temporary directory, used as the workspace root.
        overrides: The context fields to replace.

    Returns:
        The tool context.
    """
    settings = SimpleNamespace(
        deepspace_workspace_root=str(tmp_path),
        deepspace_shell_enabled=True,
        deepspace_permission_mode="workspace-write",
        deepspace_sandbox_backend="none",
    )
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=settings,
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        **overrides,
    )


def _handler(name: str) -> ToolHandler:
    """Return the handler one terminal tool declares.

    Args:
        name: The tool's name.

    Returns:
        The tool's handler.
    """
    specs = [
        *terminal_tools.TERMINAL_TOOLS,
        *terminal_tools.TERMINAL_SIGNAL_TOOLS,
        *terminal_tools.TERMINAL_JOB_TOOLS,
    ]
    for spec in specs:
        if spec.name == name:
            return spec.handler
    raise AssertionError(f"no terminal tool named {name!r}")


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> TerminalManager:
    """Install a real terminal manager in the tool module.

    Args:
        monkeypatch: The test's monkeypatch fixture.

    Returns:
        The installed manager.
    """
    real = TerminalManager()
    monkeypatch.setattr(terminal_tools, "terminal_manager", lambda: real)
    return real


async def _open_session(
    manager: TerminalManager,
    context: ToolContext,
    *,
    command: str | None = None,
) -> TerminalSession:
    """Open one session for a test.

    Args:
        manager: The manager that owns the session.
        context: The tool context that owns the session.
        command: The command the session runs at startup.

    Returns:
        The live session.
    """
    return await manager.open(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        settings=context.settings,
        shell="bash",
        command=command,
    )


async def _close(manager: TerminalManager, session: TerminalSession) -> None:
    """Close one session and forget it.

    Args:
        manager: The manager that owns the session.
        session: The session to close.
    """
    await manager.close(
        session.id,
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        conversation_id=session.conversation_id,
    )


async def _wait_for_group_gone(pgid: int, timeout: float = 5.0) -> bool:
    """Wait until a process group no longer exists.

    Args:
        pgid: The process group to watch.
        timeout: The longest the wait lasts.

    Returns:
        True when the group is gone.
    """
    if pgid <= 0:
        return False
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def _wait_until_gone(session: TerminalSession, timeout: float = 5.0) -> bool:
    """Wait until a session's child is no longer running.

    Args:
        session: The session to watch.
        timeout: The longest the wait lasts.

    Returns:
        True when the child is gone.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while session.process.is_running:
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _observation(**overrides: Any) -> WaitObservation:
    """Build one observation of a session's foreground state.

    Args:
        overrides: The observation fields to replace.

    Returns:
        The observation.
    """
    fields: dict[str, Any] = {
        "alive": True,
        "waiting_for_input": False,
        "produced_output": False,
        "waiting_streak": 0,
        "elapsed_seconds": 0.0,
        "idle_seconds": 0.0,
    }
    fields.update(overrides)
    return WaitObservation(**fields)


# --------------------------------------------------------------------------
# The signal tool
# --------------------------------------------------------------------------


def test_the_signal_tool_declares_its_name_and_mode() -> None:
    modes = {spec.name: spec.mode for spec in terminal_tools.TERMINAL_SIGNAL_TOOLS}

    assert modes == {"terminal_signal": "write"}


def test_the_signal_tool_offers_the_supported_signals() -> None:
    spec = terminal_tools.TERMINAL_SIGNAL_TOOLS[0]
    properties = spec.definition["function"]["parameters"]["properties"]

    assert properties["signal"]["enum"] == [
        "SIGINT",
        "SIGTERM",
        "SIGHUP",
        "SIGQUIT",
        "SIGKILL",
        "SIGTSTP",
        "SIGCONT",
    ]
    assert spec.definition["function"]["parameters"]["required"] == ["session_id"]


def test_the_signal_arguments_reject_an_unsupported_signal() -> None:
    with pytest.raises(ValidationError):
        terminal_tools.TerminalSignalArguments(session_id="session-1", signal="SIGWINCH")


def test_the_write_arguments_reject_a_wait_beyond_the_cap() -> None:
    with pytest.raises(ValidationError):
        terminal_tools.TerminalWriteArguments(session_id="session-1", data="x", wait_seconds=301)


@pytest.mark.skipif(sys.platform == "win32", reason="This host runs the POSIX shell only.")
async def test_a_signal_reaches_the_sessions_process_group(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context, command="sleep 30")
    try:
        result = await _handler("terminal_signal")(
            {"session_id": session.id, "signal": "SIGTERM"}, context
        )

        assert result == {
            "status": "ok",
            "session_id": session.id,
            "signal": "SIGTERM",
            "delivered": True,
        }
        # The signal reaches the session's process group, and the session
        # ends with it.
        assert await _wait_until_gone(session) is True
    finally:
        await _close(manager, session)


@pytest.mark.skipif(sys.platform == "win32", reason="This host runs the POSIX shell only.")
async def test_a_signal_interrupts_a_command_that_hangs(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context)
    try:
        await _handler("terminal_write")({"session_id": session.id, "data": "sleep 30\n"}, context)
        await asyncio.sleep(0.5)

        result = await _handler("terminal_signal")(
            {"session_id": session.id, "signal": "SIGINT"}, context
        )

        assert result["delivered"] is True
        assert session.process.is_running is True

        await _handler("terminal_write")(
            {
                "session_id": session.id,
                "data": "printf 'DEEPSPACE-INTERRUPT-ok\\n'\n",
                "wait_seconds": 5,
            },
            context,
        )
        read = await _handler("terminal_read")({"session_id": session.id}, context)

        assert "DEEPSPACE-INTERRUPT-ok" in str(read["output"])
    finally:
        await _close(manager, session)


async def test_a_signal_for_an_unknown_session_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)

    result = await _handler("terminal_signal")({"session_id": "nope"}, context)

    assert result == {"status": "error", "message": "No such terminal session."}


async def test_a_signal_for_a_foreign_session_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    owner = _context(tmp_path)
    other = _context(tmp_path)
    session = await _open_session(manager, owner)
    try:
        result = await _handler("terminal_signal")({"session_id": session.id}, other)

        assert result == {"status": "error", "message": "No such terminal session."}
    finally:
        await _close(manager, session)


async def test_an_unsupported_signal_is_refused(manager: TerminalManager, tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = await _handler("terminal_signal")(
        {"session_id": "session-1", "signal": "SIGWINCH"}, context
    )

    assert result["status"] == "error"
    assert "SIGWINCH" in str(result["message"])


# --------------------------------------------------------------------------
# The wait reason
# --------------------------------------------------------------------------


def test_a_dead_session_settles_as_a_session_exit() -> None:
    reason = classify_wait(
        _observation(alive=False, elapsed_seconds=0.0, idle_seconds=0.0),
        idle_window_seconds=1.0,
        budget_seconds=5.0,
    )

    assert reason == "session_exit"


def test_a_session_that_asks_for_input_settles_as_a_stdin_read() -> None:
    reason = classify_wait(
        _observation(
            waiting_for_input=True,
            produced_output=True,
            waiting_streak=2,
            elapsed_seconds=0.2,
            idle_seconds=0.2,
        ),
        idle_window_seconds=1.0,
        budget_seconds=5.0,
    )

    assert reason == "stdin_read"


def test_a_quiet_session_settles_as_an_inferred_idle() -> None:
    reason = classify_wait(
        _observation(elapsed_seconds=1.5, idle_seconds=1.5),
        idle_window_seconds=1.0,
        budget_seconds=5.0,
    )

    assert reason == "inferred_idle"


def test_a_session_that_outlasts_the_budget_settles_as_a_timeout() -> None:
    reason = classify_wait(
        _observation(elapsed_seconds=5.0, idle_seconds=0.1),
        idle_window_seconds=1.0,
        budget_seconds=5.0,
    )

    assert reason == "timeout"


def test_a_session_that_still_works_keeps_the_write_waiting() -> None:
    reason = classify_wait(
        _observation(elapsed_seconds=0.2, idle_seconds=0.2),
        idle_window_seconds=1.0,
        budget_seconds=5.0,
    )

    assert reason is None


async def test_a_write_without_a_wait_reports_that_it_was_sent(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context)
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "echo hi\n"}, context
        )

        assert result == {
            "status": "ok",
            "written_bytes": 8,
            "wait_reason": "sent",
        }
    finally:
        await _close(manager, session)


async def test_a_write_to_a_finished_session_reports_the_session_exit(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context, command="exit")
    try:
        assert await _wait_until_gone(session) is True

        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "echo hi\n"}, context
        )

        assert result["status"] == "ok"
        assert result["wait_reason"] == "session_exit"
    finally:
        await _close(manager, session)


@pytest.mark.skipif(
    not EXACT_WAIT_AVAILABLE,
    reason="This host does not publish the blocked system call of a process.",
)
async def test_a_write_to_a_shell_that_asks_for_input_reports_a_stdin_read(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context)
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "echo deepspace-stdin-ok\n", "wait_seconds": 5},
            context,
        )

        assert result["status"] == "ok"
        assert result["wait_reason"] == "stdin_read"
    finally:
        await _close(manager, session)


async def test_a_write_to_a_quiet_session_reports_an_inferred_idle(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context, command="sleep 5")
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "", "wait_seconds": 3}, context
        )

        assert result["status"] == "ok"
        assert result["wait_reason"] == "inferred_idle"
    finally:
        await _close(manager, session)


async def test_a_write_to_a_busy_session_reports_a_timeout(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(
        manager, context, command="while true; do echo x; sleep 0.05; done"
    )
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "", "wait_seconds": 1}, context
        )

        assert result["status"] == "ok"
        assert result["wait_reason"] == "timeout"
    finally:
        await _close(manager, session)


async def test_a_write_to_an_unknown_session_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)

    result = await _handler("terminal_write")(
        {"session_id": "nope", "data": "x", "wait_seconds": 1}, context
    )

    assert result == {"status": "error", "message": "No such terminal session."}


# --------------------------------------------------------------------------
# The background write
# --------------------------------------------------------------------------


async def test_a_background_write_returns_a_job_id_and_does_not_block(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context, command="sleep 5")
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "echo background\n", "background": True},
            context,
        )

        assert result == {
            "status": "ok",
            "session_id": session.id,
            "background": True,
            "job_id": result["job_id"],
        }
        job = manager.job(
            str(result["job_id"]),
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
        assert job is not None
        assert job.status == "running"

        deadline = asyncio.get_running_loop().time() + 10.0
        while job.status == "running":
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
            job = manager.job(
                str(result["job_id"]),
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
            )
            assert job is not None

        assert job.status == "completed"
        assert job.written_bytes == len("echo background\n")
        assert job.wait_reason == "inferred_idle"
        assert job.error is None
    finally:
        await _close(manager, session)


async def test_a_background_write_to_an_unknown_session_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)

    result = await _handler("terminal_write")(
        {"session_id": "nope", "data": "x", "background": True}, context
    )

    assert result == {"status": "error", "message": "No such terminal session."}


async def test_a_background_write_of_a_foreign_session_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    owner = _context(tmp_path)
    other = _context(tmp_path)
    session = await _open_session(manager, owner)
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "x", "background": True}, other
        )

        assert result == {"status": "error", "message": "No such terminal session."}
    finally:
        await _close(manager, session)


async def test_a_job_of_another_conversation_is_not_reported(
    manager: TerminalManager, tmp_path: Path
) -> None:
    owner = _context(tmp_path)
    other = _context(tmp_path)
    session = await _open_session(manager, owner, command="sleep 5")
    try:
        result = await _handler("terminal_write")(
            {"session_id": session.id, "data": "x", "background": True}, owner
        )

        assert (
            manager.job(
                str(result["job_id"]),
                tenant_id=other.auth.tenant_id,
                user_id=other.auth.user_id,
                conversation_id=other.conversation_id,
            )
            is None
        )
    finally:
        await _close(manager, session)


async def test_a_background_write_is_readable_through_the_job_tool(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    session = await _open_session(manager, context, command="sleep 5")
    try:
        started = await _handler("terminal_write")(
            {"session_id": session.id, "data": "x", "background": True}, context
        )

        deadline = asyncio.get_running_loop().time() + 10.0
        job = await _handler("terminal_job")({"job_id": str(started["job_id"])}, context)
        while job["job"]["status"] == "running":
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
            job = await _handler("terminal_job")({"job_id": str(started["job_id"])}, context)

        assert job["status"] == "ok"
        assert job["job"]["job_id"] == str(started["job_id"])
        assert job["job"]["session_id"] == session.id
        assert job["job"]["status"] == "completed"
        assert job["job"]["written_bytes"] == 1
        assert job["job"]["wait_reason"] == "inferred_idle"
        assert job["job"]["error"] is None
    finally:
        await _close(manager, session)


async def test_an_unknown_job_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    context = _context(tmp_path)

    result = await _handler("terminal_job")({"job_id": "nope"}, context)

    assert result == {
        "status": "error",
        "message": "No job with that id belongs to this conversation.",
    }


async def test_a_job_of_another_conversation_is_reported_as_missing(
    manager: TerminalManager, tmp_path: Path
) -> None:
    owner = _context(tmp_path)
    other = _context(tmp_path)
    session = await _open_session(manager, owner, command="sleep 5")
    try:
        started = await _handler("terminal_write")(
            {"session_id": session.id, "data": "x", "background": True}, owner
        )

        result = await _handler("terminal_job")({"job_id": str(started["job_id"])}, other)

        assert result == {
            "status": "error",
            "message": "No job with that id belongs to this conversation.",
        }
    finally:
        await _close(manager, session)
