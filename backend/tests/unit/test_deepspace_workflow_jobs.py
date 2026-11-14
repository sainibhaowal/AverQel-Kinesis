"""Tests for the durable workflow store's realtime publication.

The workflow panel reads live progress from the realtime stream, so a
status change and an appended event both have to reach that stream.  The
database stays authoritative: a publication failure never fails a run.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.deepspace.services import workflow_jobs as workflow_jobs_module
from app.deepspace.services.workflow_jobs import (
    WORKFLOW_END,
    WORKFLOW_LOG,
    WORKFLOW_PHASE,
    WorkflowJobStore,
)

pytestmark = pytest.mark.unit_no_db


class _Result:
    """The query result shape the store reads a sequence number from."""

    def scalar(self):
        return 0


class _FakeDb:
    """The session surface the store writes through."""

    def __init__(self) -> None:
        self.records: list[object] = []

    def execute(self, *_args, **_kwargs):
        return _Result()

    def add(self, record) -> None:
        self.records.append(record)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None


def _job():
    """Build the durable job the store writes about."""
    from app.deepspace.models.workflow_job import DeepSpaceWorkflowJob

    return DeepSpaceWorkflowJob(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        mode="script",
        workflow_name="demo",
        script="return 1;",
        input_text=None,
        meta={},
        args={},
        roles=[],
        permissions=[],
        status="queued",
        result={},
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every realtime publication the store makes."""
    published: list[dict] = []

    def fake_publish(_client, **kwargs) -> None:
        published.append(kwargs)

    monkeypatch.setattr(workflow_jobs_module, "publish_event_sync", fake_publish)
    monkeypatch.setattr(workflow_jobs_module, "get_redis_client", lambda: None)
    return published


def test_a_status_change_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status change reaches the realtime stream, because the event stream
    alone cannot tell an observer that the run moved from running to done."""
    published = _capture(monkeypatch)
    store = WorkflowJobStore(_FakeDb())
    job = _job()

    store.set_state(job, status="completed", stop_reason="completed", result={"ok": True})

    assert len(published) == 1
    payload = published[0]
    assert payload["resource"] == "workflow-runs"
    assert payload["event_type"] == WORKFLOW_LOG
    assert payload["data"]["status"] == "completed"
    assert payload["data"]["stop_reason"] == "completed"
    assert payload["data"]["run_id"] == str(job.id)


def test_a_published_event_carries_its_sequence_and_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An appended event reaches the stream with the sequence the panel orders by."""
    published = _capture(monkeypatch)
    store = WorkflowJobStore(_FakeDb())
    job = _job()

    store.append(job, WORKFLOW_PHASE, {"title": "reading"})

    assert len(published) == 1
    payload = published[0]
    assert payload["resource"] == "workflow-runs"
    assert payload["event_type"] == WORKFLOW_PHASE
    assert payload["data"]["sequence"] == 1
    assert payload["data"]["payload"] == {"title": "reading"}


def test_the_published_payload_reports_the_agent_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel shows how many children a run started."""
    published = _capture(monkeypatch)
    store = WorkflowJobStore(_FakeDb())
    job = _job()
    job.agents_spawned = 3

    store.set_state(job, status="running")

    assert published[0]["data"]["agents_spawned"] == 3


def test_a_publication_failure_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realtime is advisory, so a broken stream never fails a workflow."""

    def failing_publish(_client, **_kwargs) -> None:
        raise RuntimeError("the realtime stream is down")

    monkeypatch.setattr(workflow_jobs_module, "publish_event_sync", failing_publish)
    monkeypatch.setattr(workflow_jobs_module, "get_redis_client", lambda: None)
    store = WorkflowJobStore(_FakeDb())
    job = _job()

    store.set_state(job, status="completed", stop_reason="completed")

    assert job.status == "completed"


def test_the_projection_reports_the_run_fields_the_panel_shows() -> None:
    """The projection carries the fields the workflow panel reads."""
    job = _job()
    job.status = "completed"
    job.stop_reason = "completed"
    job.agents_spawned = 2
    job.output = "done"
    job.result = {"ok": True}

    projection = WorkflowJobStore.projection(job)

    assert projection["run_id"] == str(job.id)
    assert projection["mode"] == "script"
    assert projection["status"] == "completed"
    assert projection["agents_spawned"] == 2
    assert projection["output"] == "done"
    assert projection["result"] == {"ok": True}
    assert projection["cancel_requested"] is False


def test_the_end_event_name_is_stable() -> None:
    """The run's last event keeps the name the panel listens for."""
    assert WORKFLOW_END == "workflow/end"
    assert SimpleNamespace(workflow_end=WORKFLOW_END).workflow_end == "workflow/end"
