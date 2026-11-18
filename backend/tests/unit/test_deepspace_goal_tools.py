"""Tests for the DeepSpace goal tools.

The tools are the model-facing half of the goal surface.  The authority
rules are the interesting part: an automatic goal round may finish or block
the goal it is running, while creating, editing, pausing, and resuming a
goal need a person.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.deepspace.models.message import Message
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture
def goal_environment(tmp_path: Path) -> Any:
    """Build a database, a conversation, and a tool context for the goal tools."""
    settings = Settings(deepspace_goal_blocked_threshold=3)
    engine = create_engine(f"sqlite:///{tmp_path / 'goals.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Goal tools",
        )
    )
    session.commit()
    context = ToolContext(
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=settings,
        db=session,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )
    yield {
        "context": context,
        "session": session,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _call(context: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one native tool call and return its result."""
    result = asyncio.run(dispatch_native_tool(name, arguments, context))
    assert result is not None
    return result


def _add_user_message(environment: Any, source: dict[str, Any] | None) -> None:
    """Append one user message that carries an optional source."""
    session = environment["session"]
    session.add(
        Message(
            conversation_id=environment["conversation_id"],
            role="user",
            content="a request",
            metadata_json={"source": source} if source is not None else {},
            created_at=datetime.now(UTC),
        )
    )
    session.commit()


def _goal(environment: Any, name: str) -> DeepSpaceGoal:
    """Read one goal from the fixture scope."""
    return (
        environment["session"]
        .query(DeepSpaceGoal)
        .filter(
            DeepSpaceGoal.tenant_id == environment["tenant_id"],
            DeepSpaceGoal.user_id == environment["user_id"],
            DeepSpaceGoal.conversation_id == environment["conversation_id"],
            DeepSpaceGoal.name == name,
        )
        .one()
    )


# ── read and create ─────────────────────────────────────────────────────


def test_a_goal_is_recorded_and_read_back(goal_environment: Any) -> None:
    context = goal_environment["context"]

    written = _call(
        context,
        "goal_set",
        {"name": "ship-parity", "content": "Give DeepSpace the Harness tool surface."},
    )
    read = _call(context, "goal_read", {"name": "ship-parity"})

    assert written["status"] == "ok"
    assert written["goal"]["content"] == "Give DeepSpace the Harness tool surface."
    assert read["goal_count"] == 1
    assert read["goals"][0]["name"] == "ship-parity"


def test_a_new_goal_is_armed(goal_environment: Any) -> None:
    context = goal_environment["context"]

    written = _call(context, "goal_set", {"name": "ship-parity"})

    assert written["goal"]["activation"] == GoalActivation.ARMED
    assert written["goal"]["phase"] == GoalPhase.ACTIVE


def test_a_read_without_a_name_returns_every_goal(goal_environment: Any) -> None:
    context = goal_environment["context"]

    _call(context, "goal_set", {"name": "first"})
    _call(context, "goal_set", {"name": "second"})

    read = _call(context, "goal_read", {})

    assert read["goal_count"] == 2


def test_a_read_of_an_unknown_goal_returns_nothing(goal_environment: Any) -> None:
    context = goal_environment["context"]

    read = _call(context, "goal_read", {"name": "never-recorded"})

    assert read["goal_count"] == 0
    assert read["goals"] == []


def test_an_invalid_phase_is_refused(goal_environment: Any) -> None:
    context = goal_environment["context"]

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "finished"})

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_INVALID_PHASE"


# ── compare-and-set ─────────────────────────────────────────────────────


def test_a_stale_revision_is_refused(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})

    result = _call(
        context,
        "goal_set",
        {"name": "ship-parity", "content": "a newer objective", "revision": 99},
    )

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_REVISION_MISMATCH"
    assert result["current_revision"] == 1


def test_the_current_revision_is_accepted(goal_environment: Any) -> None:
    context = goal_environment["context"]
    created = _call(context, "goal_set", {"name": "ship-parity"})

    result = _call(
        context,
        "goal_set",
        {
            "name": "ship-parity",
            "content": "a newer objective",
            "revision": created["goal"]["revision"],
        },
    )

    assert result["status"] == "ok"
    assert result["goal"]["content"] == "a newer objective"


# ── the blocked threshold ───────────────────────────────────────────────


def test_a_goal_cannot_be_blocked_before_the_threshold(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "blocked"})

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_BLOCKED_BEFORE_THRESHOLD"
    assert result["threshold"] == 3
    assert result["rounds_started"] == 0


def test_a_goal_can_be_blocked_once_the_threshold_is_reached(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    goal = _goal(goal_environment, "ship-parity")
    goal.rounds_started = 3
    goal_environment["session"].commit()

    result = _call(
        context,
        "goal_set",
        {
            "name": "ship-parity",
            "phase": "blocked",
            "blocked_reason": {"message": "the release key never arrived"},
        },
    )

    assert result["status"] == "ok"
    assert result["goal"]["phase"] == GoalPhase.BLOCKED
    assert result["goal"]["blocked_reason"]["message"] == "the release key never arrived"


def test_a_blocked_goal_without_a_reason_carries_the_model_code(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    goal = _goal(goal_environment, "ship-parity")
    goal.rounds_started = 3
    goal_environment["session"].commit()

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "blocked"})

    assert result["goal"]["blocked_reason"]["code"] == "model-reported"


def test_the_threshold_follows_the_deployment_setting(goal_environment: Any) -> None:
    context = goal_environment["context"]
    context = ToolContext(
        auth=context.auth,
        conversation_id=context.conversation_id,
        settings=Settings(deepspace_goal_blocked_threshold=1),
        db=context.db,
        task_store=context.task_store,
        tool_result_store=context.tool_result_store,
        provider_registry=context.provider_registry,
    )
    _call(context, "goal_set", {"name": "ship-parity"})
    goal = _goal(goal_environment, "ship-parity")
    goal.rounds_started = 1
    goal_environment["session"].commit()

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "blocked"})

    assert result["status"] == "ok"


# ── activation follows the phase ────────────────────────────────────────


def test_a_paused_goal_is_disarmed(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "paused"})

    assert result["goal"]["activation"] == GoalActivation.DISARMED


def test_a_complete_goal_is_disarmed(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "complete"})

    assert result["goal"]["activation"] == GoalActivation.DISARMED
    assert result["goal"]["status"] == "reached"


def test_an_active_goal_is_armed_again(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    _call(context, "goal_set", {"name": "ship-parity", "phase": "paused"})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "active"})

    assert result["goal"]["activation"] == GoalActivation.ARMED


# ── goal-round authority ────────────────────────────────────────────────


def test_a_goal_round_may_not_create_a_goal(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _add_user_message(goal_environment, {"kind": "goal", "round": 1})

    result = _call(context, "goal_set", {"name": "ship-parity"})

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_ROUND_CREATE_FORBIDDEN"


def test_a_goal_round_may_not_edit_a_goal(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    _add_user_message(goal_environment, {"kind": "goal", "round": 1})

    result = _call(
        context,
        "goal_set",
        {"name": "ship-parity", "content": "a newer objective"},
    )

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_ROUND_PHASE_FORBIDDEN"
    assert result["phase"] == "active"


def test_a_goal_round_may_not_pause_a_goal(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    _add_user_message(goal_environment, {"kind": "goal", "round": 1})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "paused"})

    assert result["status"] == "error"
    assert result["error_code"] == "GOAL_ROUND_PHASE_FORBIDDEN"


def test_a_goal_round_may_complete_its_goal(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    _add_user_message(goal_environment, {"kind": "goal", "round": 1})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "complete"})

    assert result["status"] == "ok"
    assert result["goal"]["phase"] == GoalPhase.COMPLETE


def test_a_goal_round_may_block_its_goal_after_the_threshold(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _call(context, "goal_set", {"name": "ship-parity"})
    goal = _goal(goal_environment, "ship-parity")
    goal.rounds_started = 3
    goal_environment["session"].commit()
    _add_user_message(goal_environment, {"kind": "goal", "round": 3})

    result = _call(context, "goal_set", {"name": "ship-parity", "phase": "blocked"})

    assert result["status"] == "ok"
    assert result["goal"]["phase"] == GoalPhase.BLOCKED


def test_a_human_message_is_not_a_goal_round(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _add_user_message(goal_environment, {"kind": "user"})

    result = _call(context, "goal_set", {"name": "ship-parity"})

    assert result["status"] == "ok"


def test_a_message_without_a_source_is_not_a_goal_round(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _add_user_message(goal_environment, None)

    result = _call(context, "goal_set", {"name": "ship-parity"})

    assert result["status"] == "ok"


def test_the_latest_user_message_decides_the_authority(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _add_user_message(goal_environment, {"kind": "goal", "round": 1})
    _add_user_message(goal_environment, {"kind": "user"})

    result = _call(context, "goal_set", {"name": "ship-parity"})

    assert result["status"] == "ok"


def test_a_goal_round_with_round_zero_is_not_a_goal_round(goal_environment: Any) -> None:
    context = goal_environment["context"]
    _add_user_message(goal_environment, {"kind": "goal", "round": 0})

    result = _call(context, "goal_set", {"name": "ship-parity"})

    assert result["status"] == "ok"
