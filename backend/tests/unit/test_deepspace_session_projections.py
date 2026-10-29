"""Tests for the DeepSpace session read models."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.session_events import (
    STEP_END,
    STEP_START,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
)
from app.deepspace.services.session_projections import (
    delegation_tree,
    tool_timeline,
    turn_boundary,
)

pytestmark = pytest.mark.unit_no_db


class _FakeResult:
    """Stand in for the SQLAlchemy result the projections consume."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Return one fixed row set for every projection query."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def execute(self, statement: Any) -> _FakeResult:
        del statement
        return _FakeResult(self._rows)


def test_turn_boundary_reports_the_open_turn() -> None:
    db = _FakeSession(
        [
            SimpleNamespace(event_type=TURN_START, turn=1, step=None),
            SimpleNamespace(event_type=STEP_START, turn=1, step=1),
            SimpleNamespace(event_type=STEP_END, turn=1, step=1),
            SimpleNamespace(event_type=TURN_END, turn=1, step=None),
            SimpleNamespace(event_type=TURN_START, turn=2, step=None),
        ]
    )

    boundary = turn_boundary(
        db,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert boundary.open_turn == 2
    assert boundary.last_turn == 2
    assert boundary.completed_turns == 1
    assert boundary.open_step is None


def test_tool_timeline_reports_calls_and_results_in_order() -> None:
    db = _FakeSession(
        [
            SimpleNamespace(
                seq=1,
                turn=1,
                step=1,
                tool_name="fs_read",
                event_type=TOOL_CALL,
                payload={"arguments": {"path": "a.txt"}},
            ),
            SimpleNamespace(
                seq=2,
                turn=1,
                step=1,
                tool_name="fs_read",
                event_type=TOOL_RESULT,
                payload={"success": True},
            ),
        ]
    )

    entries = tool_timeline(
        db,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert [entry.status for entry in entries] == ["call", "result"]
    assert entries[0].as_dict()["tool_name"] == "fs_read"
    assert entries[1].as_dict()["payload"] == {"success": True}


def test_delegation_tree_reports_parent_and_depth() -> None:
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    db = _FakeSession(
        [
            SimpleNamespace(
                id=child_id,
                parent_agent_id=parent_id,
                parent_run_id=None,
                label="research",
                mode="continuable",
                origin="subagent",
                status="idle",
                delegation_depth=1,
                turn_count=1,
            )
        ]
    )

    tree = delegation_tree(
        db,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert tree[0]["agent_id"] == str(child_id)
    assert tree[0]["parent_agent_id"] == str(parent_id)
    assert tree[0]["mode"] == "continuable"
    assert tree[0]["depth"] == 1
