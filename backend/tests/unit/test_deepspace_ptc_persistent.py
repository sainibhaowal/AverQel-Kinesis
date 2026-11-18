"""Tests for retained programmatic tool calling environments.

The checks run against real child processes inside the host's operating-system
cage, so a retained environment keeps state across runs exactly as it would in
a deployed conversation.
"""

from __future__ import annotations

import asyncio
import shutil
import time
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
from app.deepspace.services.ptc_sessions import (
    PtcSessionManager,
    release_ptc_sessions,
)
from app.deepspace.services.tools._base import ToolContext

pytestmark = pytest.mark.unit_no_db

requires_cage = pytest.mark.skipif(
    not local_cage_available(),
    reason="The host has no operating-system cage for a confined program.",
)

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="The host has no Node.js executable for the JavaScript runtime.",
)


def _context(
    tmp_path: Path,
    executor: Any,
    *,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    **settings: Any,
) -> ToolContext:
    """Build a persistent tool context with one recording executor."""
    return ToolContext(
        auth=SimpleNamespace(
            tenant_id=tenant_id or uuid.uuid4(),
            user_id=user_id or uuid.uuid4(),
        ),
        conversation_id=conversation_id or uuid.uuid4(),
        settings=Settings(
            deepspace_workspace_root=str(tmp_path / "workspaces"),
            local_storage_path=str(tmp_path / "storage"),
            deepspace_ptc_execution_mode="persistent",
            **settings,
        ),
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
        native_executor=executor,
    )


async def _release(context: ToolContext) -> None:
    """Release the retained environments one context owns."""
    await release_ptc_sessions(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )


async def _with_release(context: ToolContext, work: Any) -> Any:
    """Run one coroutine and always release the context's environments."""
    try:
        return await work
    finally:
        await _release(context)


@requires_cage
def test_python_state_is_retained_between_runs(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    async def run() -> tuple[Any, Any]:
        first = await run_program(context=context, program="counter = 41\n", tool_names=[])
        second = await run_program(context=context, program="counter + 1\n", tool_names=[])
        return first, second

    first, second = asyncio.run(_with_release(context, run()))

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert second.value == 42
    assert first.persistent is True
    assert second.persistent is True
    assert second.sandboxed is True


@requires_cage
def test_retained_state_is_scoped_to_the_conversation(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    first_context = _context(tmp_path, executor, tenant_id=tenant_id, user_id=user_id)
    second_context = _context(tmp_path, executor, tenant_id=tenant_id, user_id=user_id)

    async def run() -> tuple[Any, Any]:
        first = await run_program(context=first_context, program="secret = 7\n", tool_names=[])
        second = await run_program(
            context=second_context, program="result = secret\n", tool_names=[]
        )
        await _release(first_context)
        await _release(second_context)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "ok", first.error
    assert second.status == "failed"
    assert "secret" in str(second.error)


@requires_cage
def test_release_clears_the_retained_environment(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    async def run() -> tuple[Any, Any]:
        first = await run_program(context=context, program="remembered = 'yes'\n", tool_names=[])
        released = await release_ptc_sessions(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
        second = await run_program(context=context, program="result = remembered\n", tool_names=[])
        return first, (released, second)

    first, (released, second) = asyncio.run(run())

    assert first.status == "ok", first.error
    assert released == 1
    assert second.status == "failed"
    assert "remembered" in str(second.error)


@requires_cage
def test_tool_bindings_are_rechecked_on_every_run(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    async def run() -> tuple[Any, Any]:
        first = await run_program(
            context=context,
            program="prepared = 'ready'\n",
            tool_names=["web_search"],
        )
        second = await run_program(
            context=context,
            program="result = tool('web_search', {'query': prepared})\n",
            tool_names=[],
        )
        return first, second

    first, second = asyncio.run(_with_release(context, run()))

    assert first.status == "ok", first.error
    assert second.status == "failed"
    assert calls == []
    assert second.tool_calls[0].status == "refused"
    assert "not available" in str(second.error)


@requires_cage
def test_a_timeout_clears_the_retained_environment(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    async def run() -> tuple[Any, Any]:
        first = await run_program(
            context=context,
            program="forgotten = 'yes'\nimport time\ntime.sleep(5)\n",
            tool_names=[],
            timeout_seconds=1,
        )
        second = await run_program(context=context, program="result = forgotten\n", tool_names=[])
        return first, second

    first, second = asyncio.run(_with_release(context, run()))

    assert first.status == "timeout"
    assert "cleared" in str(first.error)
    assert second.status == "failed"
    assert "forgotten" in str(second.error)


@requires_cage
@requires_node
def test_javascript_state_is_retained_between_runs(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor, deepspace_ptc_node_enabled=True)

    async def run() -> tuple[Any, Any]:
        first = await run_program(
            context=context,
            program="let total = 10;\n",
            tool_names=[],
            language="javascript",
        )
        second = await run_program(
            context=context,
            program="result = total + 5;\n",
            tool_names=[],
            language="javascript",
        )
        return first, second

    first, second = asyncio.run(_with_release(context, run()))

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert second.value == 15
    assert second.persistent is True


def test_javascript_persistent_mode_respects_the_node_gate(tmp_path: Path) -> None:
    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    context = _context(tmp_path, executor)

    with pytest.raises(ProgramRuntimeError, match="JavaScript PTC runtime is disabled"):
        asyncio.run(
            run_program(
                context=context,
                program="result = 1;\n",
                tool_names=[],
                language="javascript",
            )
        )


class _FakeSession:
    """A retained environment stand-in with a close counter."""

    def __init__(self, loop: asyncio.AbstractEventLoop, last_used: float) -> None:
        self.loop = loop
        self.last_used = last_used
        self.closed = False
        self.close_count = 0

    async def close(self) -> None:
        """Record one close."""
        self.closed = True
        self.close_count += 1

    def kill(self) -> None:
        """Record one forced stop."""
        self.closed = True


def test_release_matches_tenant_user_and_conversation() -> None:
    async def run() -> tuple[int, list[_FakeSession], _FakeSession, _FakeSession]:
        loop = asyncio.get_running_loop()
        manager = PtcSessionManager()
        tenant = uuid.uuid4()
        user = uuid.uuid4()
        conversation = uuid.uuid4()
        matching = _FakeSession(loop, time.monotonic())
        other_user = _FakeSession(loop, time.monotonic())
        manager._sessions[  # noqa: SLF001 - the release selection is the behavior under test
            (str(tenant), str(user), str(conversation), "python", "/tmp/ws", "workspace-write")
        ] = matching
        manager._sessions[  # noqa: SLF001 - a different user must survive the release
            (
                str(tenant),
                str(uuid.uuid4()),
                str(conversation),
                "python",
                "/tmp/ws",
                "workspace-write",
            )
        ] = other_user
        released = await manager.release(
            tenant_id=tenant, user_id=user, conversation_id=conversation
        )
        return released, [matching, other_user], matching, other_user

    released, sessions, matching, other_user = asyncio.run(run())

    assert released == 1
    assert matching.closed is True
    assert other_user.closed is False
    assert sessions == [matching, other_user]


def test_the_session_cap_closes_the_least_recently_used_environment() -> None:
    async def run() -> tuple[_FakeSession, _FakeSession]:
        loop = asyncio.get_running_loop()
        manager = PtcSessionManager()
        oldest = _FakeSession(loop, 1.0)
        newest = _FakeSession(loop, 2.0)
        manager._sessions[("t", "u", "old", "python", "/tmp/ws", "workspace-write")] = oldest
        manager._sessions[("t", "u", "new", "python", "/tmp/ws", "workspace-write")] = newest
        await manager._make_room(  # noqa: SLF001 - the cap is the behavior under test
            SimpleNamespace(deepspace_ptc_persistent_max_sessions=2)
        )
        return oldest, newest

    oldest, newest = asyncio.run(run())

    assert oldest.closed is True
    assert newest.closed is False
