"""Tests for the DeepSpace programmatic tool calling runtime.

The check runs against a real child process inside the host's operating-system
cage, so the program's tool calls travel over the same channel a deployed
program would use.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.deepspace.services.ptc_runtime import (
    ProgramRuntimeError,
    local_cage_available,
    run_program,
)
from app.deepspace.services.tools._base import ToolContext

pytestmark = pytest.mark.unit_no_db

requires_cage = pytest.mark.skipif(
    not local_cage_available(),
    reason="The host has no operating-system cage for a confined program.",
)


def _context(tmp_path: Path, executor: Any) -> ToolContext:
    """Build a tool context with one recording executor."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=Settings(
            deepspace_workspace_root=str(tmp_path / "workspaces"),
            local_storage_path=str(tmp_path / "storage"),
        ),
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
        native_executor=executor,
    )


@requires_cage
def test_a_program_calls_a_host_tool_and_returns_its_result(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"status": "ok", "value": 42}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program=(
                "result = tool('web_search', {'query': 'deep space'})\n" "print('searched')\n"
            ),
            tool_names=["web_search"],
        )
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.sandboxed is True
    assert calls == [("web_search", {"query": "deep space"})]
    assert "searched" in outcome.output
    assert outcome.value == {"status": "ok", "value": 42}
    assert outcome.tool_calls[0].status == "ok"


@requires_cage
def test_a_program_cannot_call_a_tool_it_was_not_given(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program=(
                "try:\n"
                "    tool('delete', {'target': 'library'})\n"
                "    result = 'called'\n"
                "except RuntimeError as exc:\n"
                "    result = str(exc)\n"
            ),
            tool_names=["web_search"],
        )
    )

    assert outcome.status == "ok", outcome.error
    assert calls == []
    assert outcome.tool_calls[0].status == "refused"
    assert "not available" in str(outcome.value)


@requires_cage
def test_an_unhandled_refusal_fails_the_program(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program="tool('delete', {'target': 'library'})\n",
            tool_names=["web_search"],
        )
    )

    assert outcome.status == "failed"
    assert calls == []
    assert "not available" in str(outcome.error)


@requires_cage
def test_a_failing_program_reports_its_error_without_raising(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program="raise ValueError('the program failed')\n",
            tool_names=[],
        )
    )

    assert outcome.status == "failed"
    assert "ValueError" in str(outcome.error)
    assert "the program failed" in str(outcome.error)


@requires_cage
def test_a_program_cannot_reach_the_host_filesystem(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program=("import os\n" f"result = os.path.exists({str(secret)!r})\n"),
            tool_names=[],
        )
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.value is False


@requires_cage
def test_a_program_cannot_reach_the_network(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program=(
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
                "    result = 'connected'\n"
                "except OSError:\n"
                "    result = 'blocked'\n"
            ),
            tool_names=[],
        )
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.value == "blocked"


@requires_cage
def test_a_program_that_calls_too_many_tools_is_stopped(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program=("for index in range(5):\n" "    tool('web_search', {'query': str(index)})\n"),
            tool_names=["web_search"],
            max_tool_calls=2,
        )
    )

    assert len(calls) == 2
    assert outcome.tool_calls[-1].status == "refused"


@requires_cage
def test_a_program_may_return_a_value_with_a_top_level_return(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    outcome = asyncio.run(
        run_program(
            context=context,
            program="return {'total': 3}\n",
            tool_names=[],
        )
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.value == {"total": 3}


def test_a_disabled_deployment_refuses_to_run_a_program(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)
    context.settings.deepspace_ptc_enabled = False

    with pytest.raises(ProgramRuntimeError, match="disabled"):
        asyncio.run(
            run_program(context=context, program="result = 1\n", tool_names=[]),
        )


def test_an_empty_program_is_refused(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    with pytest.raises(ProgramRuntimeError, match="program is required"):
        asyncio.run(run_program(context=context, program="   ", tool_names=[]))
