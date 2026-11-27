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
from app.deepspace.services import terminals
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
    create_function = getattr(dbapi_connection, "create_function", None)
    if create_function is None:
        return
    create_function("greatest", -1, lambda *values: max(values))
    create_function("least", -1, lambda *values: min(values))


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


def test_running_background_shells_uses_owned_terminal_jobs(monkeypatch: Any) -> None:
    tenant_id, user_id, conversation_id, child_agent_id = (uuid.uuid4() for _ in range(4))

    class _Manager:
        def jobs(self, **kwargs: Any) -> list[Any]:
            assert kwargs == {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
            }
            return [
                SimpleNamespace(
                    status="running",
                    session_id="root-session",
                    job_id="job-1",
                    agent_id=None,
                ),
                SimpleNamespace(
                    status="running",
                    session_id="child-session",
                    job_id="job-2",
                    agent_id=child_agent_id,
                ),
                SimpleNamespace(
                    status="completed",
                    session_id="finished-session",
                    job_id="job-3",
                    agent_id=None,
                ),
            ]

    monkeypatch.setattr(terminals, "terminal_manager", lambda: _Manager())

    assert GoalRoundDriver._running_background_shells(
        conversation_id=conversation_id,
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=frozenset({"member"}),
            token_id="token",
        ),
    ) == ["terminal session root-session (job job-1)"]


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


# ── reviewer verdicts and stall guards ──────────────────────────────────


class _FakeReviewerProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def generate(self, request: Any) -> Any:
        self.calls += 1
        content = self._responses.pop(0) if self._responses else ""
        return SimpleNamespace(content=content)


def test_parse_goal_verdict_handles_plain_and_fenced_json() -> None:
    from app.deepspace.services.goal_driver import parse_goal_verdict

    v1 = parse_goal_verdict('{"verdict":"done","reason":"All tests passed."}')
    assert v1 is not None
    assert v1.verdict == "done"
    assert v1.reason == "All tests passed."

    v2 = parse_goal_verdict(
        '```json\n{"verdict":"continue","reason":"Tests missing","next_step":"Run pytest"}\n```'
    )
    assert v2 is not None
    assert v2.verdict == "continue"
    assert v2.next_step == "Run pytest"

    assert parse_goal_verdict("not json at all") is None
    assert parse_goal_verdict('{"verdict":"unknown","reason":"x"}') is None


def test_reviewer_verdict_done_completes_goal(driver_environment: Any) -> None:
    goal = _goal(driver_environment)
    provider = _FakeReviewerProvider(['{"verdict":"done","reason":"pytest exited 0"}'])

    continued = asyncio.run(
        driver_environment["driver"].maybe_continue_goal(
            driver_environment["conversation_id"],
            goal,
            driver_environment["agent_id"],
            driver_environment["auth"],
            driver_environment["session"],
            history=[{"role": "assistant", "content": "Done"}],
            provider=provider,
            used_tools=True,
        )
    )

    assert continued is False
    assert goal.phase == GoalPhase.COMPLETE
    assert goal.activation == GoalActivation.DISARMED


def test_reviewer_verdict_paused_pauses_goal(driver_environment: Any) -> None:
    goal = _goal(driver_environment)
    provider = _FakeReviewerProvider(['{"verdict":"paused","reason":"User asked to stop"}'])

    continued = asyncio.run(
        driver_environment["driver"].maybe_continue_goal(
            driver_environment["conversation_id"],
            goal,
            driver_environment["agent_id"],
            driver_environment["auth"],
            driver_environment["session"],
            history=[{"role": "user", "content": "Pause this for now"}],
            provider=provider,
            used_tools=True,
        )
    )

    assert continued is False
    assert goal.phase == GoalPhase.PAUSED
    assert goal.activation == GoalActivation.DISARMED


def test_reviewer_verdict_continue_includes_next_step_and_retries_bad_json(
    driver_environment: Any,
) -> None:
    from app.deepspace.repositories.chat import DeepSpaceChatRepository

    goal = _goal(driver_environment)
    provider = _FakeReviewerProvider(
        [
            "invalid json",
            '{"verdict":"continue","reason":"Missing unit test","next_step":"Add unit test"}',
        ]
    )

    continued = asyncio.run(
        driver_environment["driver"].maybe_continue_goal(
            driver_environment["conversation_id"],
            goal,
            driver_environment["agent_id"],
            driver_environment["auth"],
            driver_environment["session"],
            history=[{"role": "assistant", "content": "Edited file"}],
            provider=provider,
            used_tools=True,
        )
    )

    assert continued is True
    assert provider.calls == 2
    assert goal.rounds_started == 1
    messages = DeepSpaceChatRepository(driver_environment["session"]).get_messages(
        tenant_id=driver_environment["tenant_id"],
        conversation_id=driver_environment["conversation_id"],
        user_id=driver_environment["user_id"],
    )
    assert any("Suggested next step: Add unit test" in (m.content or "") for m in messages)


def test_reviewer_blocked_respects_threshold_and_stall_limit_blocks(
    driver_environment: Any,
) -> None:
    from app.deepspace.services.goal_driver import (
        REVIEWER_BLOCKED_CODE,
        STALL_LIMIT_BLOCKED_CODE,
    )

    driver = GoalRoundDriver(
        SimpleNamespace(),
        Settings(deepspace_goal_blocked_threshold=2, deepspace_goal_max_stalls=2),
    )
    goal = _goal(driver_environment, max_goal_rounds=5)
    provider = _FakeReviewerProvider(
        [
            '{"verdict":"blocked","reason":"Need API key"}',
            '{"verdict":"blocked","reason":"Still need API key"}',
        ]
    )

    # First blocked verdict is below threshold (2), so it still continues once
    first = asyncio.run(
        driver.maybe_continue_goal(
            driver_environment["conversation_id"],
            goal,
            driver_environment["agent_id"],
            driver_environment["auth"],
            driver_environment["session"],
            history=[{"role": "assistant", "content": "Waiting"}],
            provider=provider,
            used_tools=True,
        )
    )
    assert first is True
    driver.complete_attempt(driver_environment["agent_id"])

    # Second consecutive blocked verdict hits threshold (2) and blocks
    second = asyncio.run(
        driver.maybe_continue_goal(
            driver_environment["conversation_id"],
            goal,
            driver_environment["agent_id"],
            driver_environment["auth"],
            driver_environment["session"],
            history=[{"role": "assistant", "content": "Still waiting"}],
            provider=provider,
            used_tools=True,
        )
    )
    assert second is False
    assert goal.phase == GoalPhase.BLOCKED
    assert goal.blocked_reason["code"] == REVIEWER_BLOCKED_CODE

    # Now verify stall limit blocks after 2 consecutive no-tool rounds
    stall_goal = _goal(driver_environment, name="stall-test", max_goal_rounds=5)
    stall_agent = uuid.uuid4()
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                stall_goal,
                stall_agent,
                driver_environment["auth"],
                driver_environment["session"],
                used_tools=False,
            )
        )
        is True
    )
    driver.complete_attempt(stall_agent)
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                stall_goal,
                stall_agent,
                driver_environment["auth"],
                driver_environment["session"],
                used_tools=False,
            )
        )
        is False
    )
    assert stall_goal.phase == GoalPhase.BLOCKED
    assert stall_goal.blocked_reason["code"] == STALL_LIMIT_BLOCKED_CODE


def test_defers_while_active_subagent_is_busy_and_times_out_after_wait_limit(
    driver_environment: Any,
) -> None:
    from app.deepspace.models.subagent import DeepSpaceSubagentRun

    clock = {"t": 100.0}
    driver = GoalRoundDriver(
        SimpleNamespace(),
        Settings(),
        now=lambda: clock["t"],
        wait_limit_seconds=60.0,
    )
    goal = _goal(driver_environment, name="subagent-wait")
    child = DeepSpaceSubagentRun(
        id=uuid.uuid4(),
        tenant_id=driver_environment["tenant_id"],
        user_id=driver_environment["user_id"],
        conversation_id=driver_environment["conversation_id"],
        label="Background child",
        busy=True,
    )
    driver_environment["session"].add(child)
    driver_environment["session"].commit()

    # Busy child: defers without consuming rounds or calling reviewer
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                goal,
                driver_environment["agent_id"],
                driver_environment["auth"],
                driver_environment["session"],
            )
        )
        is False
    )
    assert goal.rounds_started == 0
    assert goal.phase == GoalPhase.ACTIVE

    # Advance past wait_limit_seconds: pauses cleanly
    clock["t"] = 200.0
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                goal,
                driver_environment["agent_id"],
                driver_environment["auth"],
                driver_environment["session"],
            )
        )
        is False
    )
    assert goal.phase == GoalPhase.PAUSED
    assert goal.activation == GoalActivation.DISARMED


def test_reviewer_wait_verdict_only_allowed_with_running_shells_and_respects_limit(
    driver_environment: Any,
) -> None:
    clock = {"t": 10.0}
    driver = GoalRoundDriver(
        SimpleNamespace(),
        Settings(),
        now=lambda: clock["t"],
        wait_limit_seconds=30.0,
    )
    goal = _goal(driver_environment, name="shell-wait")

    # Without running shells, "wait" is rejected and retried
    no_shell_provider = _FakeReviewerProvider(
        [
            '{"verdict":"wait","reason":"Waiting on nothing"}',
            '{"verdict":"continue","reason":"Run tests","next_step":"Run pytest"}',
        ]
    )
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                goal,
                driver_environment["agent_id"],
                driver_environment["auth"],
                driver_environment["session"],
                history=[{"role": "assistant", "content": "Checked"}],
                provider=no_shell_provider,
                background_shells=[],
            )
        )
        is True
    )
    assert no_shell_provider.calls == 2
    driver.complete_attempt(driver_environment["agent_id"])

    # With a running shell, "wait" is accepted and defers without consuming a round
    shell_provider = _FakeReviewerProvider(
        [
            '{"verdict":"wait","reason":"pytest is still running"}',
            '{"verdict":"wait","reason":"pytest is still running"}',
        ]
    )
    rounds_before = goal.rounds_started
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                goal,
                driver_environment["agent_id"],
                driver_environment["auth"],
                driver_environment["session"],
                history=[{"role": "assistant", "content": "Started pytest"}],
                provider=shell_provider,
                background_shells=["terminal session s1 (job j1)"],
            )
        )
        is False
    )
    assert goal.rounds_started == rounds_before
    assert goal.phase == GoalPhase.ACTIVE

    # Exceeding wait_limit_seconds pauses the goal
    clock["t"] = 50.0
    assert (
        asyncio.run(
            driver.maybe_continue_goal(
                driver_environment["conversation_id"],
                goal,
                driver_environment["agent_id"],
                driver_environment["auth"],
                driver_environment["session"],
                history=[{"role": "assistant", "content": "Still running"}],
                provider=shell_provider,
                background_shells=["terminal session s1 (job j1)"],
            )
        )
        is False
    )
    assert goal.phase == GoalPhase.PAUSED
