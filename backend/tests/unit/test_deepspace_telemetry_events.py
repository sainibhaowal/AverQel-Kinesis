from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.deepspace.services.telemetry_events import (
    SESSION_LOG_MAX_REQUEST_BYTES,
    EventLogReporter,
    LogExporterOptions,
    OTelEventRecord,
    SessionLogRecord,
    SessionLogReporter,
)

pytestmark = pytest.mark.unit_no_db


@pytest.mark.parametrize("url", ["", "file:///tmp/events", "javascript:alert(1)"])
def test_log_exporter_requires_http_url(url: str) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        LogExporterOptions(url=url)


def test_event_reporter_bounds_queue_and_flushes_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[str] = []
    reporter = EventLogReporter(
        exporter=LogExporterOptions(url="http://127.0.0.1:4318/v1/logs"),
        resource_attributes={},
        scope={"name": "test"},
        processor_options={"max_queue_size": 1, "max_export_batch_size": 1},
        on_failure=lambda message, _error: failures.append(message),
    )
    batches: list[list[dict[str, Any]]] = []

    async def capture(records: list[dict[str, Any]]) -> None:
        batches.append(records)

    monkeypatch.setattr(reporter, "_export", capture)
    reporter.emit(OTelEventRecord("first", "one", 1))
    reporter.emit(OTelEventRecord("rejected", "two", 2))

    assert len(reporter._queue) == 1
    assert failures == ["Event log queue is full; record rejected"]

    asyncio.run(reporter.shutdown())
    assert [[record["eventName"] for record in batch] for batch in batches] == [["first"]]
    assert reporter._cancelled is True


def test_session_reporter_shutdown_flushes_queued_sync_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = SessionLogReporter(
        exporter=LogExporterOptions(url="http://127.0.0.1:4318/v1/logs"),
        resource_attributes={},
        scope={"name": "test"},
        processor_options={"scheduled_delay_millis": 60_000},
    )
    batches: list[list[dict[str, Any]]] = []

    async def capture(records: list[dict[str, Any]]) -> None:
        batches.append(records)

    monkeypatch.setattr(reporter._processor, "_send", capture)
    reporter.report_session_log(SessionLogRecord("session-1", {"time": 1, "data": "ok"}))

    asyncio.run(reporter.shutdown())
    assert len(batches) == 1
    assert batches[0][0]["attributes"]["sessionId"] == "session-1"


def test_session_reporter_rejects_record_larger_than_request_limit() -> None:
    failures: list[str] = []
    reporter = SessionLogReporter(
        exporter=LogExporterOptions(url="http://127.0.0.1:4318/v1/logs"),
        resource_attributes={},
        scope={"name": "test"},
        max_request_bytes=16,
        on_failure=lambda message, _error: failures.append(message),
    )

    reporter.report_session_log(
        SessionLogRecord("session-1", {"data": "x" * SESSION_LOG_MAX_REQUEST_BYTES})
    )

    assert reporter._processor._queue == []
    assert failures == ["Session log record rejected; content was not truncated"]


@pytest.mark.asyncio
async def test_bound_event_reporter_flushes_emits_from_sync_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = EventLogReporter(
        exporter=LogExporterOptions(url="http://127.0.0.1:4318/v1/logs"),
        resource_attributes={},
        scope={"name": "test"},
        processor_options={"scheduled_delay_millis": 1},
    )
    batches: list[list[dict[str, Any]]] = []

    async def capture(records: list[dict[str, Any]]) -> None:
        batches.append(records)

    monkeypatch.setattr(reporter, "_export", capture)
    reporter.bind_loop(asyncio.get_running_loop())
    await asyncio.to_thread(reporter.emit, OTelEventRecord("committed", "committed", 1))
    await asyncio.sleep(0.02)
    await reporter.shutdown()

    assert [record["eventName"] for batch in batches for record in batch] == ["committed"]


@pytest.mark.asyncio
async def test_bound_session_reporter_flushes_emits_from_sync_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = SessionLogReporter(
        exporter=LogExporterOptions(url="http://127.0.0.1:4318/v1/logs"),
        resource_attributes={},
        scope={"name": "test"},
        processor_options={"scheduled_delay_millis": 1},
    )
    batches: list[list[dict[str, Any]]] = []

    async def capture(records: list[dict[str, Any]]) -> None:
        batches.append(records)

    monkeypatch.setattr(reporter._processor, "_send", capture)
    reporter.bind_loop(asyncio.get_running_loop())
    await asyncio.to_thread(
        reporter.report_session_log,
        SessionLogRecord("session-1", {"time": 1, "type": "turn/end"}),
    )
    await asyncio.sleep(0.02)
    await reporter.shutdown()

    assert len(batches) == 1
    assert batches[0][0]["attributes"]["sessionId"] == "session-1"
