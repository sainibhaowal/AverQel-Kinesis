from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.tools import delegation
from app.deepspace.workers import subagent_tasks
from app.platform.worker.celery_app import celery_app

pytestmark = pytest.mark.unit_no_db


def test_local_sqlite_child_worker_does_not_issue_postgres_role_commands() -> None:
    statements: list[str] = []

    class _SQLiteSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        @staticmethod
        def execute(statement: Any) -> None:
            statements.append(str(statement))

    assert subagent_tasks._apply_worker_role(_SQLiteSession()) is False  # type: ignore[arg-type]
    assert statements == []


def test_postgres_child_worker_applies_the_restricted_application_role() -> None:
    statements: list[str] = []

    class _PostgresSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        @staticmethod
        def execute(statement: Any) -> None:
            statements.append(str(statement))

    assert subagent_tasks._apply_worker_role(_PostgresSession()) is True  # type: ignore[arg-type]
    assert statements == ["SET ROLE aks_app"]


def test_eager_local_celery_dispatches_child_turns_in_parallel_background_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    all_started = threading.Event()
    all_finished = threading.Event()
    lock = threading.Lock()
    counts = {"started": 0, "finished": 0}

    def fake_run(**_kwargs: object) -> None:
        with lock:
            counts["started"] += 1
            if counts["started"] == 3:
                all_started.set()
        release.wait(timeout=2)
        with lock:
            counts["finished"] += 1
            if counts["finished"] == 3:
                all_finished.set()

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(subagent_tasks.run_subagent_turn_task, "run", fake_run)
    context = SimpleNamespace(
        auth=SimpleNamespace(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            roles=frozenset({"member"}),
            permissions=frozenset({"queries:run"}),
        ),
        conversation_id=uuid.uuid4(),
    )

    started_at = time.monotonic()
    accepted = [
        delegation._dispatch_child_turn(
            context,
            child=SimpleNamespace(id=uuid.uuid4()),
        )
        for _ in range(3)
    ]
    dispatch_duration = time.monotonic() - started_at

    try:
        assert accepted == [True, True, True]
        assert dispatch_duration < 0.5
        assert all_started.wait(timeout=1)
        assert not all_finished.is_set()
    finally:
        release.set()

    assert all_finished.wait(timeout=1)
