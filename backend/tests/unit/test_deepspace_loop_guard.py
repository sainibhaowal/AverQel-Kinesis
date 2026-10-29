"""Tests for the DeepSpace per-conversation loop guard.

The checks cover the four limits a turn runs under, the canonical form of a
call's arguments, the turn reset, the registry, and the refusal result the
tool pipeline reports.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.loop_guard import (
    LOOP_GUARD_CODE,
    LoopGuard,
    LoopGuardLimits,
    LoopGuardRegistry,
    LoopGuardTrip,
    guard_result,
    loop_guard_registry,
    turn_budget_exhausted,
)

pytestmark = pytest.mark.unit_no_db


def _guard(limits: LoopGuardLimits | None = None, now: Any = None) -> LoopGuard:
    """Build a guard for one conversation.

    Args:
        limits: The limits the guard applies.
        now: The clock the turn budget is measured against.

    Returns:
        The guard.
    """
    if now is None:
        return LoopGuard(conversation_id=uuid.uuid4(), limits=limits)
    return LoopGuard(conversation_id=uuid.uuid4(), limits=limits, now=now)


def test_a_call_under_the_limits_is_allowed_and_recorded() -> None:
    guard = _guard(LoopGuardLimits(max_tool_calls=64, max_identical_calls=4))
    guard.start_turn()

    assert guard.check("bash", {"command": "ls"}) is None
    guard.record("bash", {"command": "ls"})

    assert guard.trip() is None
    assert guard.stats()["tool_calls"] == 1
    assert guard.stats()["distinct_calls"] == 1


def test_the_identical_call_limit_trips_after_the_repeats() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=64, max_identical_calls=3, max_repeated_arguments=10)
    )
    guard.start_turn()

    for _ in range(3):
        assert guard.check("bash", {"command": "ls"}) is None
        guard.record("bash", {"command": "ls"})

    trip = guard.check("bash", {"command": "ls"})

    assert trip is not None
    assert trip.code == "LOOP_GUARD_TRIPPED"
    assert trip.code == LOOP_GUARD_CODE
    assert trip.tool_name == "bash"
    assert trip.limit == "identical_calls"
    assert guard.trip() == trip


def test_the_repeated_arguments_limit_trips_across_tool_names() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=64, max_identical_calls=10, max_repeated_arguments=3)
    )
    guard.start_turn()

    for index in range(3):
        tool_name = f"tool_{index}"
        assert guard.check(tool_name, {"path": "a.md"}) is None
        guard.record(tool_name, {"path": "a.md"})

    trip = guard.check("tool_3", {"path": "a.md"})

    assert trip is not None
    assert trip.limit == "repeated_arguments"
    assert trip.tool_name == "tool_3"


def test_a_repetition_refusal_does_not_poison_an_unrelated_tool() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=64, max_identical_calls=2, max_repeated_arguments=10)
    )
    guard.start_turn()

    for _ in range(2):
        assert guard.check("list_teammates", {}) is None
        guard.record("list_teammates", {})

    refusal = guard.check("list_teammates", {})

    assert refusal is not None
    assert refusal.limit == "identical_calls"
    assert guard.stats()["tool_calls"] == 3
    assert guard.check("wait_agent", {"timeout_seconds": 45}) is None


def test_bounded_coordination_reads_can_repeat_without_disabling_global_limits() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=3, max_identical_calls=1, max_repeated_arguments=1)
    )
    guard.start_turn()

    for _ in range(3):
        assert guard.check("list_teammates", {}, enforce_repetition_limits=False) is None
        guard.record("list_teammates", {}, enforce_repetition_limits=False)

    refusal = guard.check("list_teammates", {}, enforce_repetition_limits=False)

    assert refusal is not None
    assert refusal.limit == "tool_calls"


def test_repeatable_reads_do_not_count_against_write_repetition_limits() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=64, max_identical_calls=1, max_repeated_arguments=1)
    )
    guard.start_turn()

    for _ in range(4):
        assert guard.check("list_teammates", {}, enforce_repetition_limits=False) is None
        guard.record("list_teammates", {}, enforce_repetition_limits=False)

    assert guard.check("team_task_update", {}) is None


def test_the_tool_call_limit_trips_at_the_limit() -> None:
    guard = _guard(
        LoopGuardLimits(
            max_tool_calls=5,
            max_identical_calls=10,
            max_repeated_arguments=10,
        )
    )
    guard.start_turn()

    for index in range(5):
        assert guard.check("bash", {"command": f"run {index}"}) is None
        guard.record("bash", {"command": f"run {index}"})

    trip = guard.check("bash", {"command": "run 5"})

    assert trip is not None
    assert trip.limit == "tool_calls"
    assert trip.tool_name == "bash"


def test_the_turn_budget_trips_when_the_clock_passes_it() -> None:
    clock = {"value": 0.0}
    guard = _guard(LoopGuardLimits(max_turn_seconds=10.0), now=lambda: clock["value"])
    guard.start_turn()

    assert guard.check("bash", {"command": "ls"}) is None
    assert turn_budget_exhausted(guard) is None

    clock["value"] = 11.0

    trip = guard.check("bash", {"command": "ls"})

    assert trip is not None
    assert trip.limit == "turn_seconds"
    assert turn_budget_exhausted(guard) is not None
    assert turn_budget_exhausted(guard) == trip


def test_the_same_arguments_in_another_order_are_the_same_call() -> None:
    guard = _guard(
        LoopGuardLimits(max_tool_calls=64, max_identical_calls=1, max_repeated_arguments=10)
    )
    guard.start_turn()

    assert guard.check("fs_read", {"a": 1, "b": 2}) is None
    guard.record("fs_read", {"a": 1, "b": 2})

    trip = guard.check("fs_read", {"b": 2, "a": 1})

    assert trip is not None
    assert trip.limit == "identical_calls"


def test_start_turn_resets_the_counters_and_keeps_the_registry_entry() -> None:
    registry = LoopGuardRegistry()
    conversation_id = uuid.uuid4()
    guard = registry.get(conversation_id)
    guard.start_turn()
    guard.record("bash", {"command": "ls"})

    assert guard.stats()["tool_calls"] == 1

    guard.start_turn()

    assert guard.stats()["tool_calls"] == 0
    assert registry.get(conversation_id) is guard


def test_the_registry_keeps_one_guard_per_conversation() -> None:
    registry = LoopGuardRegistry()
    conversation_id = uuid.uuid4()

    guard = registry.get(conversation_id)

    assert registry.get(conversation_id) is guard
    assert registry.get(uuid.uuid4()) is not guard

    registry.drop(conversation_id)

    assert registry.get(conversation_id) is not guard


def test_the_process_registry_is_shared() -> None:
    registry = loop_guard_registry()
    conversation_id = uuid.uuid4()

    guard = registry.get(conversation_id)

    try:
        assert loop_guard_registry() is registry
        assert registry.get(conversation_id) is guard
    finally:
        registry.drop(conversation_id)


def test_the_refusal_result_carries_the_typed_code() -> None:
    trip = LoopGuardTrip(
        reason="The same tool call repeated 5 times.",
        tool_name="bash",
        limit="identical_calls",
    )

    result = guard_result(trip)

    assert result["status"] == "denied"
    assert result["error_code"] == "LOOP_GUARD_TRIPPED"
    assert result["message"] == trip.reason
    assert result["limit"] == "identical_calls"
    assert result["tool_name"] == "bash"
    assert json.loads(json.dumps(result)) == result


def test_turn_wide_refusal_is_marked_terminal_for_the_model_loop() -> None:
    global_trip = LoopGuardTrip(
        reason="The turn reached its tool-call limit.",
        tool_name="list_teammates",
        limit="tool_calls",
    )
    repeated_call_trip = LoopGuardTrip(
        reason="The same write call repeated.",
        tool_name="team_task_update",
        limit="identical_calls",
    )

    global_result = guard_result(global_trip)
    repeated_call_result = guard_result(repeated_call_trip)

    assert global_result["stop_turn"] is True
    assert global_result["recoverable"] is False
    assert repeated_call_result["stop_turn"] is False
    assert repeated_call_result["recoverable"] is True


def test_the_trip_serializes_to_a_mapping() -> None:
    trip = LoopGuardTrip(reason="Turn over budget.", tool_name=None, limit="turn_seconds")

    assert trip.to_dict() == {
        "code": "LOOP_GUARD_TRIPPED",
        "reason": "Turn over budget.",
        "tool_name": None,
        "limit": "turn_seconds",
    }


def test_the_limits_fall_back_to_the_defaults_without_settings() -> None:
    limits = LoopGuardLimits.from_settings(SimpleNamespace())

    assert limits == LoopGuardLimits()
    assert limits.max_tool_calls == 64
    assert limits.max_identical_calls == 4
    assert limits.max_repeated_arguments == 6
    assert limits.max_turn_seconds == 900.0


def test_the_limits_are_read_from_the_settings() -> None:
    settings = SimpleNamespace(
        deepspace_loop_guard_max_tool_calls=10,
        deepspace_loop_guard_max_identical_calls=2,
        deepspace_loop_guard_max_repeated_arguments=3,
        deepspace_loop_guard_max_turn_seconds=30.0,
    )

    limits = LoopGuardLimits.from_settings(settings)

    assert limits.max_tool_calls == 10
    assert limits.max_identical_calls == 2
    assert limits.max_repeated_arguments == 3
    assert limits.max_turn_seconds == 30.0
