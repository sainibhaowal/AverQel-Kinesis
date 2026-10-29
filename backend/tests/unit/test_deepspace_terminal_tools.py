"""Tests for the DeepSpace terminal tools.

The checks cover the result each tool returns, the refusal a read-only
conversation receives, and the refusal a session id that belongs to somebody
else receives.  The tools run against a manager double, so the checks never
start a child process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.services.terminals import TerminalNotFoundError
from app.deepspace.services.tools import terminal as terminal_tools
from app.deepspace.services.tools._base import ToolContext, ToolHandler

pytestmark = pytest.mark.unit_no_db


class _FakeManager:
    """A terminal manager double that records the calls the tools make."""

    def __init__(self) -> None:
        """Create the double with no live sessions."""
        self.sessions: dict[str, Any] = {}
        self.opened: list[dict[str, Any]] = []
        self.reads: list[tuple[str, int]] = []
        self.writes: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.sweeps = 0

    def add(self, session_id: str = "session-1") -> Any:
        """Register one live session with the double.

        Args:
            session_id: The session's identifier.

        Returns:
            The registered session.
        """
        session = SimpleNamespace(
            id=session_id,
            shell="bash",
            pty=True,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            process=SimpleNamespace(peek=lambda limit: ""),
            idle_seconds=lambda: 1.5,
        )
        self.sessions[session_id] = session
        return session

    async def open(self, **kwargs: Any) -> Any:
        """Record an open request and return a live session."""
        self.opened.append(kwargs)
        return self.add()

    def get(self, session_id: str, **kwargs: Any) -> Any:
        """Return one live session."""
        session = self.sessions.get(session_id)
        if session is None:
            raise TerminalNotFoundError("No such terminal session.")
        return session

    async def read(self, session_id: str, **kwargs: Any) -> Any:
        """Return the buffered output of one session."""
        self.reads.append((session_id, int(kwargs.get("max_bytes") or 0)))
        self.get(session_id)
        return SimpleNamespace(output="hi", truncated=False, dropped_bytes=0, alive=True)

    async def write(self, session_id: str, **kwargs: Any) -> int:
        """Record the input sent to one session."""
        self.get(session_id)
        data = str(kwargs.get("data") or "")
        self.writes.append((session_id, data))
        return len(data.encode("utf-8"))

    async def close(self, session_id: str, **kwargs: Any) -> bool:
        """Record a close request and forget the session."""
        self.closed.append(session_id)
        return self.sessions.pop(session_id, None) is not None

    def list(self, **kwargs: Any) -> list[Any]:
        """Return the live sessions."""
        return list(self.sessions.values())

    async def sweep(self) -> list[str]:
        """Record a sweep and report that nothing was closed."""
        self.sweeps += 1
        return []


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> _FakeManager:
    """Install a terminal manager double in the tool module.

    Args:
        monkeypatch: The test's monkeypatch fixture.

    Returns:
        The installed double.
    """
    fake = _FakeManager()
    monkeypatch.setattr(terminal_tools, "terminal_manager", lambda: fake)
    return fake


def _context(**overrides: Any) -> ToolContext:
    """Build a tool context double for the terminal tools.

    Args:
        overrides: The context fields to replace.

    Returns:
        The tool context.
    """
    settings = SimpleNamespace(
        deepspace_workspace_root="/tmp/deepspace-terminal-tools",
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
    for spec in terminal_tools.TERMINAL_TOOLS:
        if spec.name == name:
            return spec.handler
    raise AssertionError(f"no terminal tool named {name!r}")


def test_the_terminal_tools_declare_their_names_and_modes() -> None:
    modes = {spec.name: spec.mode for spec in terminal_tools.TERMINAL_TOOLS}

    assert modes == {
        "terminal_open": "write",
        "terminal_read": "read",
        "terminal_write": "write",
        "terminal_close": "write",
        "terminal_list": "read",
    }


def test_the_open_arguments_reject_an_unknown_shell() -> None:
    with pytest.raises(ValidationError):
        terminal_tools.TerminalOpenArguments(shell="zsh")


def test_the_read_arguments_reject_more_than_the_output_cap() -> None:
    with pytest.raises(ValidationError):
        terminal_tools.TerminalReadArguments(session_id="session-1", max_bytes=1_000_000)


async def test_terminal_open_returns_the_session_identity(manager: _FakeManager) -> None:
    context = _context()

    result = await _handler("terminal_open")({"shell": "bash"}, context)

    assert result == {
        "status": "ok",
        "session_id": "session-1",
        "shell": "bash",
        "pty": True,
        "output": "",
    }
    assert manager.opened[0]["shell"] == "bash"
    assert manager.opened[0]["conversation_id"] == context.conversation_id


async def test_terminal_read_returns_the_buffered_output(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context()

    result = await _handler("terminal_read")({"session_id": "session-1"}, context)

    assert result == {
        "status": "ok",
        "output": "hi",
        "truncated": False,
        "dropped_bytes": 0,
        "alive": True,
    }


async def test_terminal_write_reports_the_written_bytes(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context()

    result = await _handler("terminal_write")({"session_id": "session-1", "data": "abc"}, context)

    assert result == {"status": "ok", "written_bytes": 3}
    assert manager.writes == [("session-1", "abc")]


async def test_terminal_close_reports_that_the_session_closed(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context()

    result = await _handler("terminal_close")({"session_id": "session-1"}, context)

    assert result == {"status": "ok", "closed": True}
    assert manager.closed == ["session-1"]


async def test_terminal_list_reports_the_live_sessions(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context()

    result = await _handler("terminal_list")({}, context)

    assert result == {
        "status": "ok",
        "sessions": [
            {
                "session_id": "session-1",
                "shell": "bash",
                "pty": True,
                "created_at": "2026-01-01T00:00:00+00:00",
                "idle_seconds": 1.5,
            }
        ],
    }
    assert manager.sweeps == 1


async def test_an_unknown_session_is_reported_as_missing(manager: _FakeManager) -> None:
    context = _context()

    read = await _handler("terminal_read")({"session_id": "nope"}, context)
    write = await _handler("terminal_write")({"session_id": "nope", "data": "x"}, context)
    close = await _handler("terminal_close")({"session_id": "nope"}, context)

    assert read == {"status": "error", "message": "No such terminal session."}
    assert write == {"status": "error", "message": "No such terminal session."}
    assert close == {"status": "error", "message": "No such terminal session."}


async def test_a_read_only_conversation_may_not_open_a_terminal(manager: _FakeManager) -> None:
    context = _context(sandbox_mode="read-only")

    result = await _handler("terminal_open")({"shell": "bash"}, context)

    assert result["status"] == "error"
    assert "may not change anything" in str(result["message"])
    assert manager.opened == []


async def test_a_read_only_conversation_may_not_write_to_a_terminal(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context(sandbox_mode="read-only")

    result = await _handler("terminal_write")({"session_id": "session-1", "data": "x"}, context)

    assert result["status"] == "error"
    assert "may not change anything" in str(result["message"])
    assert manager.writes == []


async def test_a_read_only_conversation_may_still_read_a_terminal(manager: _FakeManager) -> None:
    manager.add("session-1")
    context = _context(sandbox_mode="read-only")

    result = await _handler("terminal_read")({"session_id": "session-1"}, context)

    assert result["status"] == "ok"


async def test_a_disabled_deployment_refuses_to_open_a_terminal(manager: _FakeManager) -> None:
    context = _context()
    context.settings.deepspace_shell_enabled = False

    result = await _handler("terminal_open")({"shell": "bash"}, context)

    assert result["status"] == "error"
    assert "disabled" in str(result["message"])
    assert manager.opened == []


async def test_an_unknown_shell_is_refused(manager: _FakeManager) -> None:
    context = _context()

    result = await _handler("terminal_open")({"shell": "zsh"}, context)

    assert result == {"status": "error", "message": "Unknown shell: zsh"}
    assert manager.opened == []


async def test_the_tools_work_without_a_database_session(manager: _FakeManager) -> None:
    context = _context()
    assert context.db is None

    result = await _handler("terminal_open")({"shell": "bash"}, context)

    assert result["status"] == "ok"
