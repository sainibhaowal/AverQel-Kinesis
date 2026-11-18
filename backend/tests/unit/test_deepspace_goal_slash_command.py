"""Tests for the ``/goal`` slash command handler.

The handler is the registry's entry into the durable goal command service.
It runs in the command plane: the command's input, its mutations, and its
direct output never enter a model request.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models.tenant import Tenant
from app.auth.models.user import User
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal, GoalPhase
from app.deepspace.services.slash_commands import (
    SlashCommandArg,
    SlashCommandContext,
    get_slash_command_registry,
)
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
    """Create one tenant, user, and conversation for the command to act on."""
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
            title="Goal slash",
            kind="deepspace",
        )
    )
    db.commit()
    return {
        "db": db,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _context(scope: dict[str, Any], raw_input: str) -> SlashCommandContext:
    """Build a command context that carries the fixture session."""
    return SlashCommandContext(
        conversation_id=scope["conversation_id"],
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        raw_input=raw_input,
        command="goal",
        args=[SlashCommandArg(name="arg0", value="goal")],
        kwargs={},
        db=scope["db"],
    )


def _run(scope: dict[str, Any], raw_input: str) -> Any:
    """Run the registry's ``/goal`` command against the fixture scope."""
    registry = get_slash_command_registry()
    return asyncio.run(registry.execute(raw_input, _context(scope, raw_input)))


def test_the_command_records_a_goal(scope: dict[str, Any]) -> None:
    result = _run(scope, "/goal ship the migration")

    assert result.success is True
    assert result.data["action"] == "goal_create"

    goal = scope["db"].query(DeepSpaceGoal).one()
    assert goal.content == "ship the migration"
    assert goal.phase == GoalPhase.ACTIVE


def test_the_command_shows_the_current_goal(scope: dict[str, Any]) -> None:
    _run(scope, "/goal ship the migration")

    result = _run(scope, "/goal")

    assert result.success is True
    assert result.data["action"] == "goal_status"
    assert result.data["goal"]["name"] == "ship the migration"


def test_a_refused_command_reports_its_code(scope: dict[str, Any]) -> None:
    result = _run(scope, "/goal pause")

    assert result.success is False
    assert result.data["error_code"] == "GOAL_COMMAND_NO_CURRENT_GOAL"


def test_the_command_requires_a_conversation(scope: dict[str, Any]) -> None:
    context = SlashCommandContext(
        conversation_id=None,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        raw_input="/goal",
        command="goal",
        args=[],
        kwargs={},
        db=scope["db"],
    )
    registry = get_slash_command_registry()

    result = asyncio.run(registry.execute("/goal", context))

    assert result.success is False
    assert "active conversation" in result.message
