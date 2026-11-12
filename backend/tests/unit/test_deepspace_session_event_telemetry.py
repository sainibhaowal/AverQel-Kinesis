from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services import session_events

pytestmark = pytest.mark.unit_no_db


class _TelemetryRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record_session_event(self, *, conversation_id: str, event: dict[str, Any]) -> None:
        self.records.append((conversation_id, event))


def test_committed_events_forward_only_content_free_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _TelemetryRecorder()
    monkeypatch.setattr(
        "app.deepspace.services.session_telemetry.get_session_telemetry",
        lambda: recorder,
    )
    conversation_id = uuid.uuid4()
    db_session = SimpleNamespace(info={})
    row = DeepSpaceSessionEvent(
        conversation_id=conversation_id,
        event_type="tool/call",
        seq=7,
        turn=2,
        step=4,
        tool_name="fs_read",
        payload={"prompt": "private prompt", "result": "private result"},
    )

    session_events._queue_telemetry_event(db_session, row)
    session_events._forward_committed_session_events(db_session)

    assert len(recorder.records) == 1
    recorded_conversation, summary = recorder.records[0]
    assert recorded_conversation == str(conversation_id)
    assert summary["type"] == "tool/call"
    assert summary["sequence"] == 7
    assert summary["turn"] == 2
    assert summary["step"] == 4
    assert summary["tool"] == "fs_read"
    assert "payload" not in summary
    assert "prompt" not in summary
    assert "result" not in summary
    assert session_events._TELEMETRY_QUEUE_KEY not in db_session.info


def test_rolled_back_events_are_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _TelemetryRecorder()
    monkeypatch.setattr(
        "app.deepspace.services.session_telemetry.get_session_telemetry",
        lambda: recorder,
    )
    db_session = SimpleNamespace(info={session_events._TELEMETRY_QUEUE_KEY: [{"type": "turn/end"}]})

    session_events._discard_rolled_back_session_events(db_session)
    session_events._forward_committed_session_events(db_session)

    assert recorder.records == []
    assert db_session.info == {}


def test_commit_hook_keeps_database_commit_independent_of_telemetry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTelemetry:
        def record_session_event(self, **_kwargs: Any) -> None:
            raise RuntimeError("exporter unavailable")

    monkeypatch.setattr(
        "app.deepspace.services.session_telemetry.get_session_telemetry",
        lambda: BrokenTelemetry(),
    )
    db_session = SimpleNamespace(
        info={session_events._TELEMETRY_QUEUE_KEY: [{"conversation_id": "c1", "type": "turn/end"}]}
    )

    session_events._forward_committed_session_events(db_session)

    assert db_session.info == {}
