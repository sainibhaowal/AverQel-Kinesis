"""Tests for the DeepSpace named agent team.

The team runs against a self-contained SQLite database, so the durable
records, the conversation scope, the queued messages, and the shared task
board are all exercised without a database server.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.models.agent_team import TeamMember, TeamMessage, TeamTask
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.agent_team import (
    AgentTeamError,
    AgentTeamService,
    StaleTaskRevisionError,
)
from app.deepspace.services.session_events import (
    TEAM_MEMBER_SPAWNED,
    TEAM_MEMBER_STATE,
    TEAM_MESSAGE,
    TEAM_TASK,
    SessionEventStore,
)
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.team import TEAM_TOOLS
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


def _tool(name: str) -> Any:
    """Read one team tool by its name.

    Args:
        name: The tool name.

    Returns:
        The tool spec.
    """
    for spec in TEAM_TOOLS:
        if spec.name == name:
            return spec
    raise AssertionError(f"{name} is not a team tool.")


@pytest.fixture
def team_environment(tmp_path: Path) -> Any:
    """Build a self-contained database, a service, and a tool context."""
    settings = Settings(
        deepspace_agent_team_enabled=True,
        deepspace_agent_team_max_members=3,
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'team.db'}",
        connect_args={"timeout": 10},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            TeamMember.__table__,
            TeamMessage.__table__,
            TeamTask.__table__,
            Conversation.__table__,
            DeepSpaceSessionEvent.__table__,
            # A teammate is run by a child agent run, so the table the
            # teammate points at has to exist.
            DeepSpaceSubagentRun.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    other_conversation_id = uuid.uuid4()
    for identifier in (conversation_id, other_conversation_id):
        session.add(
            Conversation(
                id=identifier,
                tenant_id=tenant_id,
                user_id=user_id,
                kind="deepspace",
                title="Team test",
            )
        )
    session.commit()
    service = AgentTeamService(session)
    context = ToolContext(
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=frozenset(),
            token_id="team-test",
        ),
        conversation_id=conversation_id,
        settings=settings,
        db=session,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )
    yield {
        "engine": engine,
        "session": session,
        "service": service,
        "context": context,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "other_conversation_id": other_conversation_id,
    }
    session.close()
    engine.dispose()


def test_a_member_is_spawned_with_its_descriptor_and_the_spawn_is_recorded(
    team_environment: Any,
) -> None:
    service = team_environment["service"]

    member = service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="scout",
        provider="deepspace",
        model="deepseek-chat",
        persona="You look things up.",
        tool_filter=["fs_read", "web_search"],
    )

    assert member.name == "scout"
    assert member.provider == "deepspace"
    assert member.model == "deepseek-chat"
    assert member.persona == "You look things up."
    assert member.tool_filter == {"allow": ["fs_read", "web_search"], "deny": []}
    assert member.state == "idle"
    # The descriptor is the one the delegation machinery writes, so a later
    # turn rebuilds the teammate from it.
    assert member.descriptor["version"] == 3
    assert member.descriptor["label"] == "scout"
    assert member.descriptor["agentProvider"] == "deepspace"
    assert member.descriptor["agentModel"] == "deepseek-chat"
    assert member.descriptor["persona"] == "You look things up."
    assert member.descriptor["toolFilter"] == {
        "allow": ["fs_read", "web_search"],
        "deny": [],
    }

    events = SessionEventStore(team_environment["session"]).events(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
    )
    kinds = [event.event_type for event in events]
    assert TEAM_MEMBER_SPAWNED in kinds
    payloads = [event.payload for event in events if event.event_type == TEAM_MEMBER_SPAWNED]
    assert payloads[0]["name"] == "scout"


def test_a_message_to_a_running_member_is_delivered(team_environment: Any) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )
    service.set_member_state(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        member_name="bob",
        state="running",
    )

    message = service.send_message(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        to_name="bob",
        body="Collect the sources.",
    )

    assert message.state == "delivered"
    assert message.recipient == "bob"
    assert message.sender == "agent"
    assert (
        service.pending_messages(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            member_name="bob",
        )
        == []
    )


def test_a_message_to_an_idle_member_is_queued_and_delivered_when_it_resumes(
    team_environment: Any,
) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )

    message = service.send_message(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        to_name="bob",
        body="Collect the sources.",
    )

    assert message.state == "queued"
    waiting = service.pending_messages(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        member_name="bob",
    )
    assert [item.body for item in waiting] == ["Collect the sources."]

    member, delivered = service.resume_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        member_name="bob",
    )

    assert member.state == "running"
    assert [item.body for item in delivered] == ["Collect the sources."]
    assert delivered[0].state == "delivered"
    assert (
        service.pending_messages(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            member_name="bob",
        )
        == []
    )


def test_a_task_is_created_and_updated_at_its_current_revision(team_environment: Any) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )

    created = service.create_task(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        title="Collect the sources",
        body="Find three of them.",
    )

    assert created.revision == 1
    assert created.state == "pending"
    assert created.owner is None

    updated = service.update_task(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        task_id=str(created.id),
        revision=1,
        status="in_progress",
        owner="bob",
    )

    assert updated.revision == 2
    assert updated.state == "in_progress"
    assert updated.owner == "bob"

    events = SessionEventStore(team_environment["session"]).events(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
    )
    kinds = [event.event_type for event in events]
    assert TEAM_TASK in kinds
    assert TEAM_TASK in kinds


def test_a_stale_revision_is_refused_and_leaves_the_task_alone(team_environment: Any) -> None:
    service = team_environment["service"]
    created = service.create_task(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        title="Collect the sources",
    )
    service.update_task(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        task_id=str(created.id),
        revision=1,
        status="in_progress",
    )

    with pytest.raises(StaleTaskRevisionError):
        service.update_task(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            task_id=str(created.id),
            revision=1,
            status="completed",
        )

    tasks = service.list_tasks(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
    )
    assert len(tasks) == 1
    assert tasks[0].revision == 2
    assert tasks[0].state == "in_progress"


def test_wait_agent_returns_when_the_team_changes(team_environment: Any) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )

    def change() -> None:
        time.sleep(0.2)
        with sessionmaker(bind=team_environment["engine"])() as other:
            AgentTeamService(other).set_member_state(
                tenant_id=team_environment["tenant_id"],
                user_id=team_environment["user_id"],
                conversation_id=team_environment["conversation_id"],
                member_name="bob",
                state="running",
            )

    worker = threading.Thread(target=change)
    worker.start()
    try:
        result = service.wait_for_change(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            timeout_seconds=10,
            poll_interval=0.05,
        )
    finally:
        worker.join()

    assert not result.timed_out
    assert result.changed
    assert result.changed[0]["kind"] == "member"
    assert result.changed[0]["name"] == "bob"
    assert result.changed[0]["state"] == "running"
    assert [member.name for member in result.members] == ["bob"]


def test_wait_agent_gives_up_after_the_timeout(team_environment: Any) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )

    started = time.monotonic()
    result = service.wait_for_change(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        timeout_seconds=0.3,
        poll_interval=0.05,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.changed == []
    assert [member.name for member in result.members] == ["bob"]
    assert elapsed < 10


def test_team_wait_reports_a_durable_revision_after_a_committed_change(
    team_environment: Any,
) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )

    def change() -> None:
        time.sleep(0.05)
        with sessionmaker(bind=team_environment["engine"])() as other:
            AgentTeamService(other).set_member_state(
                tenant_id=team_environment["tenant_id"],
                user_id=team_environment["user_id"],
                conversation_id=team_environment["conversation_id"],
                member_name="bob",
                state="running",
            )

    worker = threading.Thread(target=change)
    worker.start()
    try:
        result = service.wait_for_change(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            timeout_seconds=2,
        )
    finally:
        worker.join()

    assert result.changed
    assert result.no_progress is False
    assert result.revision > 0


def test_team_writes_replay_by_idempotency_key_without_duplicate_rows(
    team_environment: Any,
) -> None:
    service = team_environment["service"]
    kwargs = {
        "tenant_id": team_environment["tenant_id"],
        "user_id": team_environment["user_id"],
        "conversation_id": team_environment["conversation_id"],
    }
    first_member = service.spawn_member(name="bob", idempotency_key="member-1", **kwargs)
    replayed_member = service.spawn_member(name="bob", idempotency_key="member-1", **kwargs)
    assert replayed_member.id == first_member.id
    assert replayed_member.replayed is True

    first_message = service.send_message(
        to_name="bob", body="Do the thing.", idempotency_key="message-1", **kwargs
    )
    replayed_message = service.send_message(
        to_name="bob", body="Do the thing again.", idempotency_key="message-1", **kwargs
    )
    assert replayed_message.id == first_message.id
    assert replayed_message.replayed is True

    first_task = service.create_task(title="Task", idempotency_key="task-1", **kwargs)
    replayed_task = service.create_task(title="Task", idempotency_key="task-1", **kwargs)
    assert replayed_task.id == first_task.id

    updated = service.update_task(
        task_id=str(first_task.id),
        revision=1,
        status="completed",
        idempotency_key="update-1",
        **kwargs,
    )
    replayed_update = service.update_task(
        task_id=str(first_task.id),
        revision=1,
        status="completed",
        idempotency_key="update-1",
        **kwargs,
    )
    assert replayed_update.id == updated.id
    assert replayed_update.revision == updated.revision


def test_stale_team_worker_is_released_and_its_queue_can_resume(team_environment: Any) -> None:
    service = team_environment["service"]
    session = team_environment["session"]
    kwargs = {
        "tenant_id": team_environment["tenant_id"],
        "user_id": team_environment["user_id"],
        "conversation_id": team_environment["conversation_id"],
    }
    member = service.spawn_member(name="bob", state="running", **kwargs)
    child = session.get(DeepSpaceSubagentRun, member.child_id)
    row = session.get(TeamMember, member.id)
    assert child is not None
    assert row is not None
    child.busy = True
    child.status = "running"
    child.pending_messages = [{"message": "resume this work"}]
    child.updated_at = datetime.now(UTC) - timedelta(hours=1)
    row.updated_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    recovered = service.recover_stale_members(stale_after_seconds=60, **kwargs)

    assert [item.name for item in recovered] == ["bob"]
    assert recovered[0].state == "idle"
    session.expire_all()
    recovered_row = session.get(TeamMember, member.id)
    recovered_child = session.get(DeepSpaceSubagentRun, member.child_id)
    assert recovered_row is not None and recovered_row.state == "idle"
    assert recovered_child is not None and recovered_child.busy is False
    events = SessionEventStore(session).events(**kwargs)
    assert any(
        event.event_type == TEAM_MEMBER_STATE
        and event.payload.get("stop_reason") == "lease_expired"
        for event in events
    )


def test_another_conversation_s_members_and_tasks_are_not_reachable(
    team_environment: Any,
) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )
    created = service.create_task(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        title="Collect the sources",
    )

    other = team_environment["other_conversation_id"]

    assert (
        service.list_members(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=other,
        )
        == []
    )
    assert (
        service.list_tasks(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=other,
        )
        == []
    )
    with pytest.raises(AgentTeamError):
        service.send_message(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=other,
            to_name="bob",
            body="Collect the sources.",
        )
    with pytest.raises(AgentTeamError):
        service.update_task(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=other,
            task_id=str(created.id),
            revision=1,
            status="completed",
        )


def test_a_teammate_name_is_refused_when_it_cannot_be_addressed(team_environment: Any) -> None:
    service = team_environment["service"]

    with pytest.raises(AgentTeamError):
        service.spawn_member(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            name="two words",
        )
    with pytest.raises(AgentTeamError):
        service.spawn_member(
            tenant_id=team_environment["tenant_id"],
            user_id=team_environment["user_id"],
            conversation_id=team_environment["conversation_id"],
            name="bob",
            max_members=0,
        )


def test_the_team_tools_declare_their_names_and_modes() -> None:
    assert [(spec.name, spec.mode) for spec in TEAM_TOOLS] == [
        ("spawn_teammate", "write"),
        ("team_send_message", "write"),
        ("list_teammates", "read"),
        ("team_list_members", "read"),
        ("team_get_member", "read"),
        ("wait_agent", "read"),
        ("interrupt_agent", "write"),
        ("team_task_create", "write"),
        ("team_task_get", "read"),
        ("team_task_list", "read"),
        ("team_task_update", "write"),
    ]


def test_the_spawn_teammate_tool_returns_the_member(team_environment: Any) -> None:
    result = asyncio.run(
        _tool("spawn_teammate").handler(
            {"name": "scout", "persona": "You look things up."}, team_environment["context"]
        )
    )

    assert result["status"] == "ok"
    assert result["member"]["name"] == "scout"
    assert result["member"]["state"] == "idle"
    assert result["member"]["persona"] == "You look things up."


def test_the_team_send_message_tool_reports_whether_the_message_was_queued(
    team_environment: Any,
) -> None:
    context = team_environment["context"]
    asyncio.run(_tool("spawn_teammate").handler({"name": "bob"}, context))

    queued = asyncio.run(
        _tool("team_send_message").handler({"to": "bob", "body": "Do the thing."}, context)
    )
    assert queued["status"] == "ok"
    assert queued["queued"] is True
    # The teammate's turn is dispatched when it was running.
    assert "dispatched" in queued

    listed = asyncio.run(_tool("list_teammates").handler({}, context))
    assert listed["status"] == "ok"
    assert [member["name"] for member in listed["members"]] == ["bob"]

    roster = asyncio.run(_tool("team_list_members").handler({}, context))
    assert roster["status"] == "ok"
    assert [member["name"] for member in roster["roster"]] == ["bob"]
    assert "outcome" in roster["roster"][0]
    detail = asyncio.run(_tool("team_get_member").handler({"name": "bob"}, context))
    assert detail["status"] == "ok"
    assert detail["member"]["name"] == "bob"


def test_the_team_task_update_tool_refuses_a_stale_revision(team_environment: Any) -> None:
    context = team_environment["context"]
    created = asyncio.run(
        _tool("team_task_create").handler({"title": "Collect the sources"}, context)
    )
    assert created["status"] == "ok"
    assert created["task"]["revision"] == 1

    updated = asyncio.run(
        _tool("team_task_update").handler(
            {"task_id": created["task"]["id"], "revision": 1, "status": "in_progress"},
            context,
        )
    )
    assert updated["status"] == "ok"
    assert updated["task"]["revision"] == 2

    stale = asyncio.run(
        _tool("team_task_update").handler(
            {"task_id": created["task"]["id"], "revision": 1, "status": "completed"},
            context,
        )
    )
    assert stale["status"] == "error"
    assert "revision" in stale["message"]

    listed = asyncio.run(_tool("team_task_list").handler({}, context))
    assert [task["status"] for task in listed["tasks"]] == ["in_progress"]


def test_the_wait_agent_tool_returns_the_members(team_environment: Any) -> None:
    context = team_environment["context"]
    asyncio.run(_tool("spawn_teammate").handler({"name": "bob"}, context))

    result = asyncio.run(_tool("wait_agent").handler({"timeout_seconds": 1}, context))

    assert result["status"] == "ok"
    assert result["changed"] == []
    assert result["timed_out"] is False
    assert result["no_progress"] is True
    assert result["active_members"] == []
    assert result["all_tasks_completed"] is False
    assert result["tasks"] == []
    assert [member["name"] for member in result["members"]] == ["bob"]


def test_a_message_is_recorded_in_the_typed_log(team_environment: Any) -> None:
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="bob",
    )
    service.send_message(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        to_name="bob",
        body="Collect the sources.",
    )

    events = SessionEventStore(team_environment["session"]).events(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
    )
    kinds = [event.event_type for event in events]

    assert TEAM_MESSAGE in kinds


def test_the_interrupt_agent_tool_stops_a_teammate(team_environment: Any) -> None:
    context = team_environment["context"]
    service = team_environment["service"]
    service.spawn_member(
        tenant_id=team_environment["tenant_id"],
        user_id=team_environment["user_id"],
        conversation_id=team_environment["conversation_id"],
        name="alice",
    )
    result = asyncio.run(_tool("interrupt_agent").handler({"target": "alice"}, context))

    assert result["status"] == "ok"
    assert result["member"]["name"] == "alice"
    assert result["member"]["state"] == "finished"

    # Also test interrupting by agent_id
    child_id = result["member"]["child_id"]
    if child_id:
        result2 = asyncio.run(_tool("interrupt_agent").handler({"agent_id": child_id}, context))
        assert result2["status"] == "accepted"


def test_the_team_task_get_and_write_scopes(team_environment: Any) -> None:
    context = team_environment["context"]
    created_parent = asyncio.run(
        _tool("team_task_create").handler(
            {
                "title": "Task 1",
                "body": "Parent task",
                "write_scopes": ["research/scout/"],
            },
            context,
        )
    )
    task1_id = created_parent["task"]["id"]
    assert created_parent["task"]["write_scopes"] == ["research/scout/"]
    assert created_parent["task"]["ready"] is True
    assert created_parent["task"]["blocked_by"] == []

    # Create task 2 depending on task 1 with overlapping write scope
    created_child = asyncio.run(
        _tool("team_task_create").handler(
            {
                "title": "Task 2",
                "body": "Child task",
                "depends_on": [task1_id],
                "write_scopes": ["research/scout/data.json"],
            },
            context,
        )
    )
    task2_id = created_child["task"]["id"]
    assert created_child["task"]["ready"] is False
    assert task1_id in created_child["task"]["blocked_by"]

    # Verify team_task_get returns the task with projected properties
    fetched = asyncio.run(_tool("team_task_get").handler({"task_id": task2_id}, context))
    assert fetched["status"] == "ok"
    assert fetched["task"]["id"] == task2_id
    assert fetched["task"]["title"] == "Task 2"
    assert fetched["task"]["ready"] is False
    assert fetched["task"]["write_scopes"] == ["research/scout/data.json"]

    # Update task 1 to completed, verify task 2 becomes ready
    asyncio.run(
        _tool("team_task_update").handler(
            {"task_id": task1_id, "revision": 1, "status": "completed"},
            context,
        )
    )
    fetched2 = asyncio.run(_tool("team_task_get").handler({"task_id": task2_id}, context))
    assert fetched2["task"]["ready"] is True
    assert fetched2["task"]["blocked_by"] == []


def test_spawn_teammate_with_task_and_mode(team_environment: Any) -> None:
    context = team_environment["context"]
    result = asyncio.run(
        _tool("spawn_teammate").handler(
            {
                "name": "scout",
                "task": "Perform initial scouting",
                "mode": "fresh",
            },
            context,
        )
    )
    assert result["status"] == "ok"
    assert result["member"]["name"] == "scout"
    assert result["task_queued"] is True
