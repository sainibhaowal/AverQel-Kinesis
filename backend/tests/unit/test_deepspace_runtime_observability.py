from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import runtime_observability
from app.deepspace.services.session_events import TOOL_CALL

pytestmark = pytest.mark.unit_no_db


class _Result:
    def __init__(self, rows: list[Any] | None = None, count: int = 0) -> None:
        self._rows = rows or []
        self._count = count

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> int:
        return self._count


class _ScopedSession:
    bind = None

    def __init__(self, root_run: Any, child: Any, root_step: Any) -> None:
        self.statements: list[str] = []
        self.root_run = root_run
        self.child = child
        self.root_step = root_step

    def execute(self, statement: Any, _params: Any = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "set_config" in sql:
            return _Result()
        if "deepspace_agent_steps" in sql:
            return _Result([self.root_step])
        if "deepspace_session_events" in sql:
            return _Result(
                [
                    SimpleNamespace(
                        subagent_id=self.child.id, event_type=TOOL_CALL, tool_name="fs_glob"
                    )
                ]
            )
        if "deepspace_subagent_runs" in sql:
            return _Result([self.child])
        if "deepspace_agent_runs" in sql and "count" not in sql:
            return _Result([self.root_run])
        return _Result(count=0)


def test_live_activity_is_account_scoped_and_reports_active_root_and_child_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4()
    child_id = uuid.uuid4()
    now = datetime.now(UTC)
    root_run = SimpleNamespace(id=run_id, status="running", updated_at=now)
    child = SimpleNamespace(
        id=child_id,
        label="Local Scout",
        status="running",
        busy=True,
        delegation_depth=1,
        updated_at=now,
    )
    root_step = SimpleNamespace(
        run_id=run_id,
        step_type="tool_start",
        status="running",
        tool_name="bash",
        tool_call_id="root-call-1",
        created_at=now,
    )
    db = _ScopedSession(root_run, child, root_step)
    auth = SimpleNamespace(tenant_id=tenant_id, user_id=user_id)
    monkeypatch.setattr(
        runtime_observability,
        "_worker_status",
        lambda: {"mode": "embedded-background-pool", "status": "available", "worker_count": 4},
    )
    monkeypatch.setattr(
        runtime_observability,
        "_execution_gate_status",
        lambda: {"status": "available"},
    )

    payload = runtime_observability.runtime_activity_status(
        db=db,  # type: ignore[arg-type]
        auth=auth,  # type: ignore[arg-type]
        settings=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert payload["jobs"] == {"queued": 0, "running": 2, "failed_last_24h": 0}
    assert payload["execution_gate"] == {"status": "available"}
    assert {item["tool_name"] for item in payload["active_tools"]} == {"bash", "fs_glob"}
    assert {item["label"] for item in payload["agents"]} == {"Main agent", "Local Scout"}
    scoped_queries = [sql for sql in db.statements if "deepspace_" in sql]
    assert scoped_queries
    assert all("tenant_id" in sql and "user_id" in sql for sql in scoped_queries)
