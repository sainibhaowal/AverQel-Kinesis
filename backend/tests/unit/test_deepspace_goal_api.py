"""Tests for the DeepSpace goal API routes.

The routes are the human-facing half of the goal surface: one reads a
conversation's goals for the goal bar, and one carries out a ``/goal``
command without spending a model turn.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.dependencies import AuthContext
from app.auth.models.tenant import Tenant
from app.auth.models.user import User
from app.deepspace.api.chats import (
    get_conversation_goals,
    run_conversation_goal_command,
)
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture()
def db() -> Any:
    """Build an in-memory database with the goal tables."""
    engine = create_engine("sqlite://")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Conversation.__table__,
            DeepSpaceGoal.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def scope(db: Session) -> dict[str, Any]:
    """Create one tenant, user, and conversation for the routes to act on."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    db.add(Tenant(id=tenant_id, name="tenant"))
    db.add(
        User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"{user_id.hex}@example.test",
            collection_code=f"C{user_id.hex[:10]}",
            password_hash="not-a-real-hash",
        )
    )
    db.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Goal API",
            kind="deepspace",
        )
    )
    db.commit()
    auth = AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=frozenset({"user"}),
        token_id="token",
    )
    return {
        "db": db,
        "auth": auth,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _command(scope: dict[str, Any], raw_input: str) -> dict[str, Any]:
    """Carry out one goal command through the route."""
    return run_conversation_goal_command(
        scope["conversation_id"],
        {"input": raw_input},
        scope["auth"],
        scope["db"],
    )


# ── the command route ───────────────────────────────────────────────────


def test_a_command_records_a_goal(scope: dict[str, Any]) -> None:
    response = _command(scope, "ship the migration")

    assert response["success"] is True
    assert response["error_code"] is None
    assert response["data"]["action"] == "goal_create"


def test_a_command_reads_the_current_goal(scope: dict[str, Any]) -> None:
    _command(scope, "ship the migration")

    response = _command(scope, "")

    assert response["success"] is True
    assert response["data"]["action"] == "goal_status"
    assert response["data"]["goal"]["name"] == "ship the migration"


def test_a_refused_command_reports_its_code(scope: dict[str, Any]) -> None:
    response = _command(scope, "edit something")

    assert response["success"] is False
    assert response["error_code"] == "GOAL_COMMAND_NO_CURRENT_GOAL"
    assert response["data"] == {}


def test_the_command_route_accepts_the_command_key(scope: dict[str, Any]) -> None:
    response = run_conversation_goal_command(
        scope["conversation_id"],
        {"command": "ship the migration"},
        scope["auth"],
        scope["db"],
    )

    assert response["success"] is True
    assert response["data"]["action"] == "goal_create"


def test_the_command_route_names_the_goal_a_control_word_acts_on(scope: dict[str, Any]) -> None:
    _command(scope, "ship the migration")

    response = run_conversation_goal_command(
        scope["conversation_id"],
        {"input": "pause", "goal_name": "ship the migration"},
        scope["auth"],
        scope["db"],
    )

    assert response["success"] is True
    assert response["data"]["action"] == "goal_pause"


def test_the_command_route_refuses_an_unknown_named_goal(scope: dict[str, Any]) -> None:
    _command(scope, "ship the migration")

    response = run_conversation_goal_command(
        scope["conversation_id"],
        {"input": "pause", "goal_name": "never-recorded"},
        scope["auth"],
        scope["db"],
    )

    assert response["success"] is False
    assert response["error_code"] == "GOAL_COMMAND_UNKNOWN_GOAL"


def test_the_command_route_refuses_an_over_long_input(scope: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as excinfo:
        run_conversation_goal_command(
            scope["conversation_id"],
            {"input": "x" * 8_001},
            scope["auth"],
            scope["db"],
        )

    assert excinfo.value.status_code == 422


# ── the read route ──────────────────────────────────────────────────────


def test_the_read_route_reports_the_goals(scope: dict[str, Any]) -> None:
    _command(scope, "ship the migration")

    rows = get_conversation_goals(
        scope["conversation_id"],
        scope["auth"],
        scope["db"],
    )

    assert len(rows) == 1
    assert rows[0]["name"] == "ship the migration"
    assert rows[0]["phase"] == GoalPhase.ACTIVE
    assert rows[0]["activation"] == GoalActivation.ARMED


def test_the_read_route_reports_an_empty_conversation(scope: dict[str, Any]) -> None:
    rows = get_conversation_goals(
        scope["conversation_id"],
        scope["auth"],
        scope["db"],
    )

    assert rows == []


def test_the_read_route_does_not_report_another_tenant_s_goal(
    scope: dict[str, Any], db: Session
) -> None:
    other_tenant = uuid.uuid4()
    other_user = uuid.uuid4()
    other_conversation = uuid.uuid4()
    db.add(Tenant(id=other_tenant, name="other"))
    db.add(
        User(
            id=other_user,
            tenant_id=other_tenant,
            email=f"{other_user.hex}@example.test",
            collection_code=f"C{other_user.hex[:10]}",
            password_hash="not-a-real-hash",
        )
    )
    db.add(
        Conversation(
            id=other_conversation,
            tenant_id=other_tenant,
            user_id=other_user,
            title="Other",
            kind="deepspace",
        )
    )
    db.commit()
    db.add(
        DeepSpaceGoal(
            tenant_id=other_tenant,
            user_id=other_user,
            conversation_id=other_conversation,
            name="other-goal",
            content="other",
        )
    )
    db.commit()

    rows = get_conversation_goals(
        scope["conversation_id"],
        scope["auth"],
        scope["db"],
    )

    assert rows == []
