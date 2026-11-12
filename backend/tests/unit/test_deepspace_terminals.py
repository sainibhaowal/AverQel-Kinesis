"""Tests for the DeepSpace terminal sessions and the managed process layer.

The checks cover the shell resolver, the ownership boundary between tenants,
the bounded output buffer, the idle sweeper, and the workspace rule that
keeps a session inside the conversation's directory.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.terminal_backend import (
    TerminalBackend,
    TerminalBackendRegistry,
    TerminalBackendSpawnSpec,
)
from app.deepspace.services.terminals import (
    NativeTerminalBackend,
    OutputBuffer,
    ShellUnavailableError,
    TerminalError,
    TerminalLimitError,
    TerminalManager,
    TerminalNotFoundError,
    TerminalSession,
    _build_argv,
    available_shells,
    build_session_environment,
    resolve_shell,
    terminal_manager,
)
from app.deepspace.services.workspace_root import WorkspacePathError

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path) -> SimpleNamespace:
    """Build the deployment settings one test needs."""
    return SimpleNamespace(deepspace_workspace_root=str(tmp_path / "workspaces"))


async def _open_session(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    command: str | None = "printf hi",
) -> TerminalSession:
    """Open one short-lived session for a test.

    Args:
        manager: The manager that owns the session.
        tmp_path: The test's temporary directory.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The owning conversation.
        command: The command the session runs at startup.

    Returns:
        The live session.
    """
    return await manager.open(
        tenant_id=tenant_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        conversation_id=conversation_id or uuid.uuid4(),
        settings=_settings(tmp_path),
        shell="bash",
        command=command,
        permission_mode="danger-full-access",
    )


# --------------------------------------------------------------------------
# The shell resolver
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe exists on Windows.")
def test_cmd_is_unavailable_off_windows() -> None:
    with pytest.raises(ShellUnavailableError) as failure:
        resolve_shell("cmd")

    assert str(failure.value) == "cmd.exe is only available on Windows."


@pytest.mark.skipif(sys.platform == "win32", reason="This host has no bash.")
def test_bash_resolves_on_this_host() -> None:
    argv = resolve_shell("bash")

    assert len(argv) == 1
    assert argv[0].endswith("bash")


def test_available_shells_reports_what_the_host_can_run() -> None:
    kinds = available_shells()

    assert ("cmd" in kinds) is (sys.platform == "win32")
    if sys.platform != "win32":
        assert "bash" in kinds


def test_interactive_bash_does_not_load_the_host_prompt_configuration() -> None:
    # Interactive mode is required for readline/job control on the PTY; the
    # profile switches keep the host's arbitrary prompt configuration out.
    assert _build_argv("bash", None)[1:] == ["--noprofile", "--norc", "-i"]


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


async def test_a_session_of_one_tenant_cannot_be_read_by_another_tenant(tmp_path: Path) -> None:
    manager = TerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await _open_session(
        manager,
        tmp_path,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    other_tenant = uuid.uuid4()
    try:
        with pytest.raises(TerminalNotFoundError):
            manager.get(
                session.id,
                tenant_id=other_tenant,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        with pytest.raises(TerminalNotFoundError):
            await manager.read(
                session.id,
                tenant_id=other_tenant,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        with pytest.raises(TerminalNotFoundError):
            await manager.write(
                session.id,
                tenant_id=other_tenant,
                user_id=user_id,
                conversation_id=conversation_id,
                data="ls",
            )
        closed = await manager.close(
            session.id,
            tenant_id=other_tenant,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        assert closed is False
    finally:
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


async def test_a_session_of_one_user_cannot_be_read_by_another_user(tmp_path: Path) -> None:
    manager = TerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await _open_session(
        manager,
        tmp_path,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    try:
        with pytest.raises(TerminalNotFoundError):
            manager.get(
                session.id,
                tenant_id=tenant_id,
                user_id=uuid.uuid4(),
                conversation_id=conversation_id,
            )
    finally:
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


async def test_a_session_of_one_conversation_is_not_listed_by_another(tmp_path: Path) -> None:
    manager = TerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await _open_session(
        manager,
        tmp_path,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    try:
        listed = manager.list(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.uuid4(),
        )

        assert listed == []
    finally:
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


async def test_agent_scoped_sessions_are_private_to_the_opening_agent(tmp_path: Path) -> None:
    manager = TerminalManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    child_agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    session = TerminalSession(
        id="agent-owned-session",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        shell="bash",
        cwd=tmp_path,
        process=SimpleNamespace(),  # This test exercises ownership, not process I/O.
        created_at=now,
        last_used_at=now,
        pty=False,
        agent_id=child_agent_id,
    )
    manager._sessions[session.id] = session
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    try:
        assert manager.get(session.id, **owner, agent_id=child_agent_id) is session
        assert manager.list(**owner, agent_id=child_agent_id) == [session]
        assert manager.list(**owner, agent_id=None) == []
        with pytest.raises(TerminalNotFoundError):
            manager.get(session.id, **owner, agent_id=None)
        with pytest.raises(TerminalNotFoundError):
            manager.get(session.id, **owner, agent_id=uuid.uuid4())
        # Authenticated user-facing routes intentionally retain conversation
        # access so the user can inspect and close an agent's terminal.
        assert manager.get(session.id, **owner) is session
    finally:
        manager._sessions.clear()


async def test_background_terminal_jobs_keep_their_agent_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.terminals as terminals_module

    async def settled(*_args: object, **_kwargs: object) -> str:
        return "inferred_idle"

    monkeypatch.setattr(terminals_module, "_wait_for_reason", settled)
    manager = TerminalManager()
    tenant_id, user_id, conversation_id, agent_id = (uuid.uuid4() for _ in range(4))

    class Process:
        is_running = True

        async def write(self, data: str) -> int:
            return len(data.encode())

        def touch(self) -> None:
            return None

    now = datetime.now(UTC)
    session = TerminalSession(
        id="agent-background-session",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        shell="bash",
        cwd=Path("."),
        process=Process(),  # type: ignore[arg-type]
        created_at=now,
        last_used_at=now,
        pty=False,
        agent_id=agent_id,
    )
    manager._sessions[session.id] = session
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "agent_id": agent_id,
    }
    job = await manager.start_background_write(session.id, **owner, data="echo ok")
    await asyncio.gather(*manager._jobs._tasks)

    assert manager.job(job.job_id, **owner) is not None
    assert manager.job(job.job_id, **owner).status == "completed"
    assert (
        manager.job(
            job.job_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            agent_id=None,
        )
        is None
    )


async def test_terminal_manager_dispatches_through_registered_backend(tmp_path: Path) -> None:
    captured: list[TerminalBackendSpawnSpec] = []

    class RecordingBackend(TerminalBackend):
        async def spawn(self, spec: TerminalBackendSpawnSpec):
            captured.append(spec)
            return await NativeTerminalBackend().spawn(spec)

    registry = TerminalBackendRegistry()
    registry.register("recording", RecordingBackend(), default=True)
    manager = TerminalManager(backend_registry=registry)
    tenant_id, user_id, conversation_id, agent_id = (uuid.uuid4() for _ in range(4))
    containment_range = None
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            settings=_settings(tmp_path),
            shell="bash",
            command="printf backend-registry",
            workspace_root=tmp_path,
            permission_mode="danger-full-access",
            rows=33,
            columns=111,
        )

        assert registry.list_backends() == ["recording"]
        assert len(captured) == 1
        spec = captured[0]
        assert spec.owner == {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
        }
        assert spec.rows == 33
        assert spec.cols == 111
        assert spec.shell == "bash"
        assert spec.command == "printf backend-registry"
        assert spec.cwd == str(tmp_path)
        assert spec.argv
        assert spec.env is not None
        assert session.agent_id == agent_id
        if os.name == "posix":
            assert session.process.containment_range is not None
            containment_range = session.process.containment_range
    finally:
        for session in manager.list(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            await manager.close(
                session.id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
            )
        if containment_range is not None:
            assert containment_range.stopped is True


async def test_terminal_manager_rejects_invalid_initial_terminal_dimensions(
    tmp_path: Path,
) -> None:
    manager = TerminalManager()
    with pytest.raises(TerminalError, match="Terminal dimensions"):
        await manager.open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            settings=_settings(tmp_path),
            shell="bash",
            workspace_root=tmp_path,
            permission_mode="danger-full-access",
            rows=0,
            columns=80,
        )


# --------------------------------------------------------------------------
# The bounded output buffer
# --------------------------------------------------------------------------


def test_the_output_buffer_keeps_the_tail_and_reports_the_dropped_bytes() -> None:
    buffer = OutputBuffer(max_bytes=1024)

    buffer.append(b"a" * 512)
    buffer.append(b"b" * 1024)

    output = buffer.read(4096)

    assert output == "b" * 1024
    assert buffer.dropped_bytes == 512
    assert buffer.pending_bytes == 0


def test_a_read_returns_at_most_the_requested_bytes() -> None:
    buffer = OutputBuffer(max_bytes=4096)
    buffer.append(b"x" * 1000)

    assert buffer.read(100) == "x" * 100
    assert buffer.pending_bytes == 900


def test_a_peek_leaves_the_buffer_untouched() -> None:
    buffer = OutputBuffer(max_bytes=4096)
    buffer.append(b"hello")

    assert buffer.peek(4096) == "hello"
    assert buffer.pending_bytes == 5


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


async def test_the_sweeper_closes_an_idle_session(tmp_path: Path) -> None:
    manager = TerminalManager(idle_timeout_seconds=0.0)
    session = await _open_session(manager, tmp_path)

    closed = await manager.sweep()

    assert session.id in closed
    assert (
        manager.list(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
        )
        == []
    )


async def test_a_live_session_survives_the_sweeper(tmp_path: Path) -> None:
    manager = TerminalManager(idle_timeout_seconds=1800.0)
    session = await _open_session(manager, tmp_path)
    try:
        closed = await manager.sweep()

        assert closed == []
        assert (
            len(
                manager.list(
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    conversation_id=session.conversation_id,
                )
            )
            == 1
        )
    finally:
        await manager.close(
            session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
        )


async def test_a_conversation_cannot_hold_more_sessions_than_its_limit(tmp_path: Path) -> None:
    manager = TerminalManager(max_sessions_per_conversation=1)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await _open_session(
        manager,
        tmp_path,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    try:
        with pytest.raises(TerminalLimitError):
            await _open_session(
                manager,
                tmp_path,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
    finally:
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


# --------------------------------------------------------------------------
# The workspace rule and the environment
# --------------------------------------------------------------------------


async def test_a_session_may_not_start_outside_the_workspace(tmp_path: Path) -> None:
    manager = TerminalManager()

    with pytest.raises(WorkspacePathError):
        await manager.open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            settings=_settings(tmp_path),
            shell="bash",
            cwd="../outside",
        )


def test_the_session_environment_omits_the_deployment_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKS_JWT_SECRET", "secret-value")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    env = build_session_environment(workspace=workspace, shell="bash", mode="workspace-write")

    assert "AKS_JWT_SECRET" not in env
    assert all(not name.startswith("AKS_") for name in env)
    assert env["HOME"] == str(workspace)
    assert env["TMPDIR"].startswith(str(workspace))
    assert env["PS1"] == r"\u@\h:$(pwd)\$ "
    assert env["DEEPSPACE_SHELL"] == "1"
    assert env["DEEPSPACE_SANDBOX"] == "1"


def test_windows_session_environment_keeps_required_os_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.terminals as terminals_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    windows_values = {
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
    }
    for name, value in windows_values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(terminals_module.sys, "platform", "win32")

    env = build_session_environment(workspace=workspace, shell="cmd", mode="workspace-write")

    assert {name: env[name] for name in windows_values} == windows_values
    assert env["TEMP"] == str(workspace / ".deepspace-tmp")
    assert env["TMP"] == str(workspace / ".deepspace-tmp")
    assert "AKS_JWT_SECRET" not in env


async def test_a_session_starts_inside_the_workspace_root(tmp_path: Path) -> None:
    manager = TerminalManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session = await manager.open(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        settings=_settings(tmp_path),
        shell="bash",
        command="pwd",
        workspace_root=workspace,
        permission_mode="danger-full-access",
    )
    try:
        output = ""
        for _ in range(20):
            output += (
                await manager.read(
                    session.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            ).output
            if str(workspace) in output:
                break
            await asyncio.sleep(0.01)
        assert str(workspace) in output
        assert session.cwd == workspace
    finally:
        await manager.close(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )


# --------------------------------------------------------------------------
# A real child process
# --------------------------------------------------------------------------


async def test_a_session_reads_the_output_of_its_startup_command(tmp_path: Path) -> None:
    manager = TerminalManager()
    session = await _open_session(manager, tmp_path)
    output = ""
    try:
        for _ in range(50):
            result = await manager.read(
                session.id,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                conversation_id=session.conversation_id,
            )
            output += result.output
            if output:
                break
            await asyncio.sleep(0.1)

        assert output == "hi"
    finally:
        await manager.close(
            session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
        )


async def test_a_session_runs_behind_a_pseudo_terminal_on_this_host(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows has no pseudo-terminal in the standard library.")
    manager = TerminalManager()
    session = await _open_session(manager, tmp_path)
    try:
        assert session.pty is True
    finally:
        await manager.close(
            session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
        )


async def test_a_session_writes_input_to_its_child(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("This host runs the POSIX shell only.")
    manager = TerminalManager()
    session = await manager.open(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        settings=_settings(tmp_path),
        shell="bash",
        permission_mode="danger-full-access",
    )
    output = ""
    try:
        written = await manager.write(
            session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            data="printf deepspace-terminal-ok\n",
        )
        assert written == len("printf deepspace-terminal-ok\n")
        for _ in range(50):
            await asyncio.sleep(0.1)
            result = await manager.read(
                session.id,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                conversation_id=session.conversation_id,
            )
            output += result.output
            if "deepspace-terminal-ok" in output:
                break

        assert "deepspace-terminal-ok" in output
    finally:
        await manager.close(
            session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
        )


def test_the_manager_is_a_process_wide_singleton() -> None:
    assert terminal_manager() is terminal_manager()


def test_the_output_buffer_is_bounded_by_its_cap() -> None:
    buffer = OutputBuffer(max_bytes=256)

    for _ in range(10):
        buffer.append(b"z" * 100)

    assert buffer.pending_bytes == 256
    assert buffer.dropped_bytes == 744
