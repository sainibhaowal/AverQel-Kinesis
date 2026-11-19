"""Tests for the DeepSpace goal round driver.

The driver queues one automatic continuation round for an armed, active
goal.  A goal that spent its round allowance is blocked with a stable
policy code rather than reported as complete, and a disarmed goal never
continues on its own.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.auth.dependencies import AuthContext
from app.auth.models.tenant import Tenant
from app.auth.models.user import User
from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.deepspace.services.durable_subagent_manager import get_durable_subagent_manager
from app.deepspace.services.goal_driver import (
    ROUND_LIMIT_BLOCKED_CODE,
    GoalRoundDriver,
    RoundIdentity,
    is_goal_round_source,
    same_queued,
    same_round,
)
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@event.listens_for(Engine, "connect")
def _register_sqlite_helpers(dbapi_connection: Any, _record: Any) -> None:
    """Give SQLite the PostgreSQL scalar functions the storage tables use.

    The storage lifecycle upsert compares two timestamps with ``greatest``,
    which SQLite does not ship.  The local comparison is exact for the
    timestamps the upsert stores.
    """
    dbapi_connection.create_function("greatest", -1, lambda *values: max(values))
    dbapi_connection.create_function("least", -1, lambda *values: min(values))


@pytest.fixture
def driver_environment(tmp_path: Path) -> Any:
    """Build a database, a conversation, and a goal for the driver."""
    engine = create_engine(f"sqlite:///{tmp_path / 'driver.db'}")
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
            title="Goal driver",
        )
    )
    session.commit()
    auth = AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=frozenset({"member"}),
        token_id="token",
    )
    driver = GoalRoundDriver(SimpleNamespace(), Settings())

    # Mock the durable subagent manager's wait_for_children to return True immediately
    durable_manager = get_durable_subagent_manager()
    original_wait = durable_manager.wait_for_children

    async def mock_wait_for_children(*args: Any, **kwargs: Any) -> bool:
        return True

    durable_manager.wait_for_children = mock_wait_for_children  # type: ignore[assignment]

    yield {
        "driver": driver,
        "session": session,
        "auth": auth,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "agent_id": uuid.uuid4(),
    }

    # Restore original
    durable_manager.wait_for_children = original_wait


def _goal(environment: Any, **values: Any) -> DeepSpaceGoal:
    """Record one goal in the fixture scope."""
    fields: dict[str, Any] = {
        "tenant_id": environment["tenant_id"],
        "user_id": environment["user_id"],
        "conversation_id": environment["conversation_id"],
        "name": "ship-parity",
        "content": "Give DeepSpace the Harness tool surface.",
        "phase": GoalPhase.ACTIVE,
        "activation": GoalActivation.ARMED,
    }
    fields.update(values)
    goal = DeepSpaceGoal(**fields)
    environment["session"].add(goal)
    environment["session"].commit()
    return goal


def _continue(environment: Any, goal: DeepSpaceGoal) -> bool:
    """Run one continuation attempt against the fixture scope."""
    return asyncio.run(
        environment["driver"].maybe_continue_goal(
            environment["conversation_id"],
            goal,
            environment["agent_id"],
            environment["auth"],
            environment["session"],
        )
    )


# ── activation and phase gates ──────────────────────────────────────────


def test_a_disarmed_goal_does_not_continue(driver_environment: Any) -> None:
    goal = _goal(driver_environment, activation=GoalActivation.DISARMED)

    assert _continue(driver_environment, goal) is False
    assert goal.rounds_started == 0


def test_a_paused_goal_does_not_continue(driver_environment: Any) -> None:
    goal = _goal(driver_environment, phase=GoalPhase.PAUSED)

    assert _continue(driver_environment, goal) is False
    assert goal.rounds_started == 0


def test_a_blocked_goal_does_not_continue(driver_environment: Any) -> None:
    goal = _goal(driver_environment, phase=GoalPhase.BLOCKED)

    assert _continue(driver_environment, goal) is False
    assert goal.rounds_started == 0


def test_a_completed_goal_does_not_continue(driver_environment: Any) -> None:
    goal = _goal(driver_environment, phase=GoalPhase.COMPLETE)

    assert _continue(driver_environment, goal) is False


# ── round exhaustion ────────────────────────────────────────────────────


def test_a_goal_that_spent_its_allowance_is_blocked(driver_environment: Any) -> None:
    goal = _goal(driver_environment, rounds_started=3, max_goal_rounds=3)

    assert _continue(driver_environment, goal) is False
    assert goal.phase == GoalPhase.BLOCKED
    assert goal.status == "abandoned"
    assert goal.activation == GoalActivation.DISARMED
    assert goal.blocked_reason is not None
    assert goal.blocked_reason["code"] == ROUND_LIMIT_BLOCKED_CODE


def test_a_goal_that_spent_its_allowance_is_not_reported_as_complete(
    driver_environment: Any,
) -> None:
    goal = _goal(driver_environment, rounds_started=3, max_goal_rounds=3)

    _continue(driver_environment, goal)

    assert goal.phase != GoalPhase.COMPLETE
    assert goal.status != "reached"


def test_a_goal_below_its_allowance_is_not_blocked(driver_environment: Any) -> None:
    goal = _goal(driver_environment, rounds_started=2, max_goal_rounds=3)

    assert _continue(driver_environment, goal) is True
    assert goal.phase == GoalPhase.ACTIVE
    assert goal.blocked_reason is None


# ── a queued round ──────────────────────────────────────────────────────


def test_a_queued_round_advances_the_round_counter(driver_environment: Any) -> None:
    goal = _goal(driver_environment, max_goal_rounds=3)

    assert _continue(driver_environment, goal) is True
    assert goal.rounds_started == 1
    assert goal.revision == 2


def test_a_queued_round_records_the_goal_source(driver_environment: Any) -> None:
    from app.deepspace.models.message import Message

    goal = _goal(driver_environment, max_goal_rounds=3)

    _continue(driver_environment, goal)

    message = (
        driver_environment["session"]
        .query(Message)
        .filter(Message.conversation_id == driver_environment["conversation_id"])
        .one()
    )
    source = message.metadata_json["source"]
    assert source["kind"] == "goal"
    assert source["round"] == 1
    assert source["goalId"] == str(goal.id)


def test_a_second_round_is_not_queued_while_one_is_pending(driver_environment: Any) -> None:
    goal = _goal(driver_environment, max_goal_rounds=3)

    assert _continue(driver_environment, goal) is True
    assert _continue(driver_environment, goal) is False


# ── the marker survives the turn's own user message ─────────────────────


def test_the_marker_the_driver_records_is_found_by_its_request_id(
    driver_environment: Any,
) -> None:
    from app.deepspace.models.message import Message
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    goal = _goal(driver_environment, max_goal_rounds=3)
    _continue(driver_environment, goal)

    session = driver_environment["session"]
    message = (
        session.query(Message)
        .filter(Message.conversation_id == driver_environment["conversation_id"])
        .one()
    )
    request_id = str(message.metadata_json["client_request_id"])

    source = DeepSpaceChatRepository(session).get_user_source_by_request_id(
        tenant_id=driver_environment["tenant_id"],
        conversation_id=driver_environment["conversation_id"],
        user_id=driver_environment["user_id"],
        request_id=request_id,
    )

    assert source["kind"] == "goal"
    assert source["round"] == 1


def test_a_marker_belongs_to_its_own_request_only(driver_environment: Any) -> None:
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    goal = _goal(driver_environment, max_goal_rounds=3)
    _continue(driver_environment, goal)

    source = DeepSpaceChatRepository(driver_environment["session"]).get_user_source_by_request_id(
        tenant_id=driver_environment["tenant_id"],
        conversation_id=driver_environment["conversation_id"],
        user_id=driver_environment["user_id"],
        request_id="a-different-request",
    )

    assert source == {}


def test_the_chat_service_carries_the_marker_onto_its_own_user_message(
    driver_environment: Any,
) -> None:
    from app.deepspace.models.message import Message
    from app.deepspace.services.chat_service import DeepSpaceChatService

    goal = _goal(driver_environment, max_goal_rounds=3)
    _continue(driver_environment, goal)

    session = driver_environment["session"]
    message = (
        session.query(Message)
        .filter(Message.conversation_id == driver_environment["conversation_id"])
        .one()
    )
    request_id = str(message.metadata_json["client_request_id"])
    service = DeepSpaceChatService(db=session, settings=Settings())

    source = service._turn_source(
        auth=driver_environment["auth"],
        conversation_id=driver_environment["conversation_id"],
        client_request_id=request_id,
    )

    assert source["kind"] == "goal"


def test_a_persons_turn_carries_no_marker(driver_environment: Any) -> None:
    from app.deepspace.services.chat_service import DeepSpaceChatService

    service = DeepSpaceChatService(db=driver_environment["session"], settings=Settings())

    source = service._turn_source(
        auth=driver_environment["auth"],
        conversation_id=driver_environment["conversation_id"],
        client_request_id=None,
    )

    assert source == {}


# ── source identity ─────────────────────────────────────────────────────


def test_a_goal_source_is_recognized() -> None:
    assert is_goal_round_source({"kind": "goal", "round": 1}) is True


def test_a_user_source_is_not_a_goal_source() -> None:
    assert is_goal_round_source({"kind": "user"}) is False


def test_a_goal_source_with_round_zero_is_not_a_goal_source() -> None:
    assert is_goal_round_source({"kind": "goal", "round": 0}) is False


def test_a_source_matches_its_reserved_identity() -> None:
    identity = RoundIdentity(goal_id=uuid.uuid4(), revision=2, round=3)

    assert same_round(
        {"goalId": str(identity.goal_id), "revision": 2, "round": 3},
        identity,
    )


def test_a_source_does_not_match_another_identity() -> None:
    identity = RoundIdentity(goal_id=uuid.uuid4(), revision=2, round=3)

    assert not same_round(
        {"goalId": str(identity.goal_id), "revision": 2, "round": 4},
        identity,
    )


def test_a_queued_record_matches_its_reservation() -> None:
    from app.deepspace.services.goal_driver import RoundAttempt

    goal_id = uuid.uuid4()
    attempt = RoundAttempt(
        goal_id=goal_id,
        revision=1,
        round=1,
        message_id=uuid.uuid4(),
        content=[],
    )

    assert same_queued(
        [],
        {"kind": "goal", "goalId": str(goal_id), "revision": 1, "round": 1},
        attempt,
    )


def test_a_queued_record_without_a_goal_source_does_not_match() -> None:
    from app.deepspace.services.goal_driver import RoundAttempt

    goal_id = uuid.uuid4()
    attempt = RoundAttempt(
        goal_id=goal_id,
        revision=1,
        round=1,
        message_id=uuid.uuid4(),
        content=[],
    )

    assert not same_queued(
        [],
        {"kind": "user", "goalId": str(goal_id), "revision": 1, "round": 1},
        attempt,
    )
