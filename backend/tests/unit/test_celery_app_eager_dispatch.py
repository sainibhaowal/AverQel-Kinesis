from __future__ import annotations

from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from app.platform.worker.celery_app import _eager_aware_apply_async

pytestmark = pytest.mark.unit_no_db


def test_mcp_stdio_catalog_refresh_runs_off_the_request_thread_in_eager_mode() -> None:
    completed = Event()
    caller_thread = get_ident()
    worker_threads: list[int] = []
    fake_task = SimpleNamespace(
        name="mcp.refresh_server_catalog",
        _get_app=lambda: SimpleNamespace(conf=SimpleNamespace(task_always_eager=True)),
        apply=lambda **_kwargs: (worker_threads.append(get_ident()), completed.set()),
        AsyncResult=lambda task_id: task_id,
    )

    result = _eager_aware_apply_async(
        fake_task,
        args=("server-id", "tenant-id"),
        task_id="scheduled-task-id",
    )

    assert result == "scheduled-task-id"
    assert completed.wait(timeout=2)
    assert worker_threads and worker_threads[0] != caller_thread
