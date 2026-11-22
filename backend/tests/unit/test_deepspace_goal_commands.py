"""Tests for the human-facing ``/goal`` command.

The command lets a person control a conversation's goals without spending a
model turn.  The grammar is deliberate: a control word is recognized only
when it fills the whole input, and any other suffix is an objective.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models.tenant import Tenant
from app.auth.models.user import User
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal, GoalActivation, GoalPhase
from app.deepspace.models.message import Message
from app.deepspace.services.goal_commands import (
    HUMAN_BLOCKED_CODE,
    GoalCommandError,
    execute_goal_command,
    parse_goal_command,
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
            Message.__table__,
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
def scope(db: Session) -> dict[str, uuid.UUID]:
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
            title="Goal scope",
            kind="deepspace",
        )
    )
    db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _run(db: Session, scope: dict[str, uuid.UUID], raw_input: str) -> Any:
    """Carry out one goal command against the fixture scope."""
    return execute_goal_command(
        db,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        conversation_id=scope["conversation_id"],
        raw_input=raw_input,
    )


def _run_on(
    db: Session,
    scope: dict[str, uuid.UUID],
    raw_input: str,
    goal_name: str,
) -> Any:
    """Carry out one goal command that names the goal it acts on."""
    return execute_goal_command(
        db,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        conversation_id=scope["conversation_id"],
        raw_input=raw_input,
        goal_name=goal_name,
    )


def _goal(db: Session, scope: dict[str, uuid.UUID], name: str) -> DeepSpaceGoal:
    """Read one goal from the fixture scope."""
    return (
        db.query(DeepSpaceGoal)
        .filter(
            DeepSpaceGoal.tenant_id == scope["tenant_id"],
            DeepSpaceGoal.user_id == scope["user_id"],
            DeepSpaceGoal.conversation_id == scope["conversation_id"],
            DeepSpaceGoal.name == name,
        )
        .one()
    )


# ── grammar ─────────────────────────────────────────────────────────────


def test_an_empty_input_names_no_control_word() -> None:
    assert parse_goal_command("") == ("", "")


def test_a_control_word_is_recognized_on_its_own() -> None:
    assert parse_goal_command("pause") == ("pause", "")
    assert parse_goal_command("resume") == ("resume", "")
    assert parse_goal_command("clear") == ("clear", "")
    assert parse_goal_command("list") == ("list", "")
    assert parse_goal_command("complete") == ("complete", "")


def test_a_control_word_with_a_suffix_is_an_objective() -> None:
    assert parse_goal_command("pause after verification") == (
        "create",
        "pause after verification",
    )


def test_an_objective_is_a_create() -> None:
    assert parse_goal_command("ship the migration") == ("create", "ship the migration")


def test_create_names_its_objective_explicitly() -> None:
    assert parse_goal_command("create ship the migration") == ("create", "ship the migration")


def test_edit_takes_its_objective_inline() -> None:
    assert parse_goal_command("edit ship the migration") == ("edit", "ship the migration")


def test_a_bare_edit_names_no_objective() -> None:
    assert parse_goal_command("edit") == ("edit", "")


def test_block_takes_its_reason_inline() -> None:
    assert parse_goal_command("block waiting for the release key") == (
        "block",
        "waiting for the release key",
    )


def test_a_control_word_is_case_insensitive() -> None:
    assert parse_goal_command("PAUSE") == ("pause", "")


# ── status ──────────────────────────────────────────────────────────────


def test_a_conversation_without_goals_reports_how_to_record_one(db: Session, scope: Any) -> None:
    outcome = _run(db, scope, "")

    assert outcome.success is True
    assert "no goal" in outcome.message
    assert outcome.data["action"] == "goal_status"
    assert outcome.data["goal"] is None
    assert "/goal <objective>" in outcome.data["next_commands"]


def test_a_bare_command_shows_the_current_goal(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "")

    assert outcome.success is True
    assert "ship the migration" in outcome.message
    assert outcome.data["goal"]["phase"] == GoalPhase.ACTIVE
    assert outcome.data["goal"]["activation"] == GoalActivation.ARMED
    assert "/goal pause" in outcome.data["next_commands"]


def test_list_reports_every_goal(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "list")

    assert outcome.success is True
    assert outcome.data["action"] == "goal_list"
    assert len(outcome.data["goals"]) == 1


def test_list_reports_an_empty_conversation(db: Session, scope: Any) -> None:
    outcome = _run(db, scope, "list")

    assert outcome.success is True
    assert "no goals" in outcome.message
    assert outcome.data["goals"] == []


# ── create ──────────────────────────────────────────────────────────────


def test_an_objective_records_a_goal(db: Session, scope: Any) -> None:
    outcome = _run(db, scope, "ship the migration")

    assert outcome.success is True
    assert outcome.data["action"] == "goal_create"

    goal = _goal(db, scope, "ship the migration")
    assert goal.content == "ship the migration"
    assert goal.phase == GoalPhase.ACTIVE
    assert goal.activation == GoalActivation.ARMED
    assert goal.revision == 1


def test_an_empty_objective_is_refused(db: Session, scope: Any) -> None:
    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "create")

    assert excinfo.value.code == "GOAL_COMMAND_EMPTY_OBJECTIVE"


def test_an_objective_over_the_cap_is_refused(db: Session, scope: Any) -> None:
    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "x" * 100_001)

    assert excinfo.value.code == "GOAL_COMMAND_OBJECTIVE_TOO_LONG"


def test_a_second_goal_is_refused_while_the_first_is_unfinished(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "write the release notes")

    assert excinfo.value.code == "GOAL_COMMAND_UNFINISHED_GOAL"


def test_a_finished_goal_may_be_replaced(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "complete")

    outcome = _run(db, scope, "write the release notes")

    assert outcome.success is True
    assert _goal(db, scope, "write the release notes").content == "write the release notes"


def test_replacing_a_finished_goal_starts_a_fresh_identity(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    first = _goal(db, scope, "ship the migration")
    _run(db, scope, "complete")
    first_id = first.id

    _run(db, scope, "ship the migration")

    goal = _goal(db, scope, "ship the migration")
    assert goal.id != first_id
    assert goal.rounds_started == 0
    assert goal.revision == 1


# ── edit ────────────────────────────────────────────────────────────────


def test_an_edit_replaces_the_objective(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "edit ship the migration and the release notes")

    assert outcome.success is True
    goal = _goal(db, scope, "ship the migration")
    assert goal.content == "ship the migration and the release notes"
    assert goal.phase == GoalPhase.ACTIVE
    assert goal.revision == 2


def test_an_edit_without_an_objective_is_refused(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "edit")

    assert excinfo.value.code == "GOAL_COMMAND_EMPTY_OBJECTIVE"


def test_an_edit_without_a_current_goal_is_refused(db: Session, scope: Any) -> None:
    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "edit something")

    assert excinfo.value.code == "GOAL_COMMAND_NO_CURRENT_GOAL"


def test_a_completed_goal_is_replaced_by_an_edit(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "complete")

    outcome = _run(db, scope, "edit write the release notes")

    assert outcome.success is True
    assert outcome.data["action"] == "goal_create"
    assert _goal(db, scope, "write the release notes").content == "write the release notes"


# ── pause and resume ────────────────────────────────────────────────────


def test_a_pause_stops_the_goal_and_disarms_it(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "pause")

    assert outcome.success is True
    goal = _goal(db, scope, "ship the migration")
    assert goal.phase == GoalPhase.PAUSED
    assert goal.activation == GoalActivation.DISARMED
    assert goal.revision == 2


def test_a_pause_of_a_paused_goal_is_refused(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "pause")

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "pause")

    assert excinfo.value.code == "GOAL_COMMAND_NOT_ACTIVE"


def test_a_resume_restarts_a_paused_goal(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "pause")

    outcome = _run(db, scope, "resume")

    assert outcome.success is True
    goal = _goal(db, scope, "ship the migration")
    assert goal.phase == GoalPhase.ACTIVE
    assert goal.activation == GoalActivation.ARMED


def test_a_resume_clears_a_blocker(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "block waiting for the release key")

    _run(db, scope, "resume")

    goal = _goal(db, scope, "ship the migration")
    assert goal.blocked_reason is None
    assert goal.phase == GoalPhase.ACTIVE


def test_a_resume_is_refused_when_the_round_cap_is_spent(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    goal = _goal(db, scope, "ship the migration")
    goal.rounds_started = goal.max_goal_rounds
    db.commit()

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "resume")

    assert excinfo.value.code == "GOAL_COMMAND_ROUND_CAP_SPENT"


def test_a_resume_of_a_completed_goal_is_refused(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "complete")

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "resume")

    assert excinfo.value.code == "GOAL_COMMAND_GOAL_FINISHED"


# ── complete, block, clear ──────────────────────────────────────────────


def test_a_completion_finishes_the_goal(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "complete")

    assert outcome.success is True
    goal = _goal(db, scope, "ship the migration")
    assert goal.phase == GoalPhase.COMPLETE
    assert goal.status == "reached"
    assert goal.activation == GoalActivation.DISARMED


def test_completing_twice_is_refused(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "complete")

    with pytest.raises(GoalCommandError) as excinfo:
        _run(db, scope, "complete")

    assert excinfo.value.code == "GOAL_COMMAND_GOAL_FINISHED"


def test_a_person_may_block_a_goal_immediately(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "block waiting for the release key")

    assert outcome.success is True
    goal = _goal(db, scope, "ship the migration")
    assert goal.phase == GoalPhase.BLOCKED
    assert goal.activation == GoalActivation.DISARMED
    assert goal.blocked_reason is not None
    assert goal.blocked_reason["code"] == HUMAN_BLOCKED_CODE
    assert goal.blocked_reason["message"] == "waiting for the release key"


def test_a_block_without_a_reason_still_records_the_blocker(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    _run(db, scope, "block")

    goal = _goal(db, scope, "ship the migration")
    assert goal.blocked_reason is not None
    assert goal.blocked_reason["code"] == HUMAN_BLOCKED_CODE


def test_a_clear_removes_the_goal(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run(db, scope, "clear")

    assert outcome.success is True
    assert (
        db.query(DeepSpaceGoal)
        .filter(DeepSpaceGoal.conversation_id == scope["conversation_id"])
        .count()
        == 0
    )


def test_a_clear_without_a_current_goal_reports_nothing_to_clear(db: Session, scope: Any) -> None:
    outcome = _run(db, scope, "clear")

    assert outcome.success is True
    assert "no goal to clear" in outcome.message
    assert outcome.data["goal_id"] is None


# ── a named goal ────────────────────────────────────────────────────────


def test_a_named_goal_is_the_one_the_command_acts_on(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run_on(db, scope, "pause", "ship the migration")

    assert outcome.success is True
    assert outcome.data["goal"]["name"] == "ship the migration"
    assert _goal(db, scope, "ship the migration").phase == GoalPhase.PAUSED


def test_a_named_goal_may_be_resumed(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")
    _run(db, scope, "pause")

    outcome = _run_on(db, scope, "resume", "ship the migration")

    assert outcome.success is True
    assert _goal(db, scope, "ship the migration").phase == GoalPhase.ACTIVE
    assert _goal(db, scope, "ship the migration").activation == GoalActivation.ARMED


def test_an_unknown_named_goal_is_refused(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    with pytest.raises(GoalCommandError) as excinfo:
        _run_on(db, scope, "pause", "never-recorded")

    assert excinfo.value.code == "GOAL_COMMAND_UNKNOWN_GOAL"


def test_a_named_goal_may_be_cleared(db: Session, scope: Any) -> None:
    _run(db, scope, "ship the migration")

    outcome = _run_on(db, scope, "clear", "ship the migration")

    assert outcome.success is True
    assert (
        db.query(DeepSpaceGoal)
        .filter(DeepSpaceGoal.conversation_id == scope["conversation_id"])
        .count()
        == 0
    )


# ── tenant isolation ────────────────────────────────────────────────────


def test_a_goal_of_another_tenant_is_not_visible(db: Session, scope: Any) -> None:
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

    outcome = _run(db, scope, "list")

    assert outcome.success is True
    assert outcome.data["goals"] == []
