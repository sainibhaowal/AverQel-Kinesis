"""Tests for automatic parent wakeup when background subagents complete."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.auth.models.tenant import Tenant
from app.auth.models.user import User
from app.deepspace.models.agent_runtime import DeepSpaceAgentRun
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.workers.subagent_tasks import _maybe_wake_parent_on_child_completion
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@event.listens_for(Engine, "connect")
def _register_sqlite_helpers(dbapi_connection: Any, _record: Any) -> None:
    create_function = getattr(dbapi_connection, "create_function", None)
    if create_function is None:
        return
    create_function("greatest", -1, lambda *values: max(values))
    create_function("least", -1, lambda *values: min(values))


@pytest.fixture
def wakeup_env(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite:///{tmp_path / 'wakeup.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, name="tenant"))
    session.add(
        User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"{user_id.hex}@example.test",
            collection_code=f"C{user_id.hex[:10]}",
            password_hash="not-a-real-hash",
        )
    )
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Subagent wakeup",
        )
    )
    session.commit()
    return {
        "session": session,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _child(env: Any, **overrides: Any) -> DeepSpaceSubagentRun:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": env["tenant_id"],
        "user_id": env["user_id"],
        "conversation_id": env["conversation_id"],
        "label": "Worker A",
        "busy": False,
        "status": "idle",
        "turn_count": 1,
        "result_text": "All unit tests passed.",
    }
    fields.update(overrides)
    row = DeepSpaceSubagentRun(**fields)
    env["session"].add(row)
    env["session"].commit()
    return row


def test_wakes_idle_parent_with_background_result_when_last_child_finishes(
    wakeup_env: Any, monkeypatch: Any
) -> None:
    session = wakeup_env["session"]
    dispatched: list[dict[str, Any]] = []

    from app.deepspace.workers import tasks as ds_tasks

    monkeypatch.setattr(
        ds_tasks.dispatch_deepspace_turn_queue,
        "apply_async",
        lambda *, kwargs: dispatched.append(kwargs),
    )

    child = _child(wakeup_env)
    woke = _maybe_wake_parent_on_child_completion(session, child)

    assert woke is True
    assert len(dispatched) == 1
    assert dispatched[0]["conversation_id"] == str(wakeup_env["conversation_id"])

    queued = session.execute(select(DeepSpaceQueuedTurn)).scalars().all()
    assert len(queued) == 1
    assert '<background-result kind="subagent"' in queued[0].prompt
    assert "All unit tests passed." in queued[0].prompt

    # Idempotent: calling again for the same turn does not duplicate
    woke_again = _maybe_wake_parent_on_child_completion(session, child)
    assert woke_again is False
    assert len(session.execute(select(DeepSpaceQueuedTurn)).scalars().all()) == 1


def test_waits_for_all_parallel_siblings_before_waking_parent(wakeup_env: Any) -> None:
    session = wakeup_env["session"]
    child1 = _child(wakeup_env, label="Worker 1", busy=False, result_text="Part 1 done")
    _child2 = _child(wakeup_env, label="Worker 2", busy=True, result_text=None)

    assert _maybe_wake_parent_on_child_completion(session, child1, dispatch_queue=False) is False
    assert session.execute(select(DeepSpaceQueuedTurn)).scalars().all() == []

    # Once Worker 2 finishes, parent wakes with both results
    _child2.busy = False
    _child2.result_text = "Part 2 done"
    session.commit()

    assert _maybe_wake_parent_on_child_completion(session, _child2, dispatch_queue=False) is True
    queued = session.execute(select(DeepSpaceQueuedTurn)).scalars().all()
    assert len(queued) == 1
    assert "Part 1 done" in queued[0].prompt
    assert "Part 2 done" in queued[0].prompt


def test_skips_wakeup_when_parent_run_is_active_or_child_is_nested(wakeup_env: Any) -> None:
    session = wakeup_env["session"]

    # Grandchild (parent_agent_id set) never wakes the root conversation
    grandchild = _child(wakeup_env, parent_agent_id=uuid.uuid4())
    assert (
        _maybe_wake_parent_on_child_completion(session, grandchild, dispatch_queue=False) is False
    )

    # Active parent run: skips wakeup
    session.add(
        DeepSpaceAgentRun(
            tenant_id=wakeup_env["tenant_id"],
            user_id=wakeup_env["user_id"],
            conversation_id=wakeup_env["conversation_id"],
            status="running",
        )
    )
    session.commit()
    root_child = _child(wakeup_env)
    assert (
        _maybe_wake_parent_on_child_completion(session, root_child, dispatch_queue=False) is False
    )
    assert session.execute(select(DeepSpaceQueuedTurn)).scalars().all() == []
