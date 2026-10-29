"""Tests for the durable DeepSpace trajectory projection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
from app.deepspace.services.session_surfaces import trajectory_projection

pytestmark = pytest.mark.unit_no_db


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self.rows


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def execute(self, statement: Any) -> _Result:
        del statement
        return _Result(self.rows)


def test_trajectory_includes_tools_when_turn_close_precedes_pairing() -> None:
    """Tool calls remain visible after the turn's end event is projected."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            seq=1,
            event_type=TURN_START,
            turn=1,
            step=None,
            tool_name=None,
            payload={},
            created_at=start,
        ),
        SimpleNamespace(
            seq=2,
            event_type=STEP_START,
            turn=1,
            step=1,
            tool_name=None,
            payload={},
            created_at=start + timedelta(milliseconds=1),
        ),
        SimpleNamespace(
            seq=3,
            event_type=TOOL_CALL,
            turn=1,
            step=1,
            tool_name="fs_read",
            payload={"arguments": {"path": "README.md"}},
            created_at=start + timedelta(milliseconds=2),
        ),
        SimpleNamespace(
            seq=4,
            event_type=TOOL_RESULT,
            turn=1,
            step=1,
            tool_name="fs_read",
            payload={"status": "completed", "duration_ms": 7},
            created_at=start + timedelta(milliseconds=9),
        ),
        SimpleNamespace(
            seq=5,
            event_type=STEP_END,
            turn=1,
            step=1,
            tool_name=None,
            payload={},
            created_at=start + timedelta(milliseconds=10),
        ),
        SimpleNamespace(
            seq=6,
            event_type=TURN_END,
            turn=1,
            step=None,
            tool_name=None,
            payload={},
            created_at=start + timedelta(milliseconds=11),
        ),
    ]

    result = trajectory_projection(
        _Session(rows),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    step = result["turns"][0]["steps"][0]
    assert step["tool_count"] == 1
    assert step["tools"][0]["tool_name"] == "fs_read"
    assert result["totals"]["tool_count"] == 1
    # Verify 3-lane spans and records are populated per turn
    turn = result["turns"][0]
    assert "spans" in turn
    assert len(turn["spans"]) >= 1
    assert any(s["lane"] == "tools" for s in turn["spans"])
    assert "records" in turn
    assert len(turn["records"]) > 0


def test_workflow_run_projection() -> None:
    """Workflow runs are projected from workflow_run tool calls and results."""
    from app.deepspace.services.session_surfaces import workflow_run_projection

    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            seq=1,
            event_type=TOOL_CALL,
            turn=1,
            step=1,
            tool_name="workflow_run",
            payload={"arguments": {"workflow": "deploy_staging"}},
            created_at=start,
        ),
        SimpleNamespace(
            seq=2,
            event_type=TOOL_RESULT,
            turn=1,
            step=1,
            tool_name="workflow_run",
            payload={"status": "completed", "duration_ms": 1500},
            created_at=start + timedelta(milliseconds=1500),
        ),
    ]

    result = workflow_run_projection(
        _Session(rows),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert len(result) == 1
    assert result[0]["workflow"] == "deploy_staging"
    assert result[0]["status"] == "completed"
    assert result[0]["duration_ms"] == 1500


def test_changed_file_projection() -> None:
    """Changed files are extracted and counted from write tool calls."""
    from app.deepspace.services.session_surfaces import changed_file_projection

    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            seq=1,
            event_type=TOOL_CALL,
            turn=1,
            step=1,
            tool_name="fs_write",
            payload={"arguments": {"path": "src/index.ts"}},
            created_at=start,
        ),
        SimpleNamespace(
            seq=2,
            event_type=TOOL_CALL,
            turn=1,
            step=2,
            tool_name="fs_write",
            payload={"arguments": {"path": "src/index.ts"}},
            created_at=start + timedelta(milliseconds=100),
        ),
        SimpleNamespace(
            seq=3,
            event_type=TOOL_CALL,
            turn=1,
            step=3,
            tool_name="fs_write",
            payload={"arguments": {"path": "README.md"}},
            created_at=start + timedelta(milliseconds=200),
        ),
    ]

    result = changed_file_projection(
        _Session(rows),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )

    assert len(result) == 2
    paths = {f["path"]: f["write_count"] for f in result}
    assert paths["src/index.ts"] == 2
    assert paths["README.md"] == 1
