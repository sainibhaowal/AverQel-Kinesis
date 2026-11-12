from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import session_telemetry as telemetry_module

pytestmark = pytest.mark.unit_no_db


class _Reporter:
    def __init__(self) -> None:
        self.records: list[Any] = []
        self.shutdown_calls = 0

    def emit(self, record: Any) -> None:
        self.records.append(record)

    def report_session_log(self, record: Any) -> None:
        self.records.append(record)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _Factory:
    def __init__(self) -> None:
        self.event = _Reporter()
        self.session = _Reporter()

    def create_event_reporter(self, _options: dict[str, Any]) -> _Reporter:
        return self.event

    def create_session_log_reporter(self, _options: dict[str, Any]) -> _Reporter:
        return self.session


def _settings() -> SimpleNamespace:
    return SimpleNamespace()


def test_session_telemetry_initializes_opt_in_log_reporters_and_filters_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_module, "OTEL_AVAILABLE", False)
    factory = _Factory()
    config = telemetry_module.SessionTelemetryConfig(
        enabled=True,
        logs_endpoint="http://127.0.0.1:4318/v1/logs",
        logs_headers={"Authorization": "Bearer test"},
        service_name="unit-test",
        attributes={"service.version": "1"},
    )
    telemetry = telemetry_module.SessionTelemetry(config, _settings())
    telemetry._channel_factory = factory

    assert telemetry.initialize() is False  # Log reporters work without the trace SDK.
    loop = asyncio.new_event_loop()
    try:
        telemetry.bind_loop(loop)
    finally:
        loop.close()
    telemetry.record_session_event(
        conversation_id="conversation-1",
        event={
            "type": "tool/call",
            "time": 123,
            "sequence": 8,
            "turn": 2,
            "step": 3,
            "tool": "fs_read",
            "payload": {"prompt": "private", "result": "private"},
        },
    )

    ordinary, session_log = factory.event.records[0], factory.session.records[0]
    assert ordinary.event_name == "tool/call"
    assert ordinary.body == "tool/call"
    assert ordinary.timestamp == 123
    assert "payload" not in ordinary.attributes
    assert session_log.session_id == "conversation-1"
    assert session_log.event == {
        "type": "tool/call",
        "time": 123,
        "sequence": 8,
        "turn": 2,
        "step": 3,
        "tool": "fs_read",
    }

    asyncio.run(telemetry.shutdown_channels())
    assert factory.event.shutdown_calls == 1
    assert factory.session.shutdown_calls == 1
    assert telemetry._event_reporter is None
    assert telemetry._session_log_reporter is None


def test_session_telemetry_disabled_does_not_create_reporters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_module, "OTEL_AVAILABLE", False)
    telemetry = telemetry_module.SessionTelemetry(
        telemetry_module.SessionTelemetryConfig(
            enabled=False,
            logs_endpoint="http://127.0.0.1:4318/v1/logs",
        ),
        _settings(),
    )

    assert telemetry.initialize() is False
    assert telemetry._event_reporter is None
    assert telemetry._session_log_reporter is None
    telemetry.record_session_event(conversation_id="c1", event={"type": "turn/end"})
    asyncio.run(telemetry.shutdown_channels())


def test_span_handle_closes_context_once() -> None:
    class Context:
        exits = 0

        def __enter__(self) -> str:
            return "span"

        def __exit__(self, *_args: object) -> None:
            self.exits += 1

    context = Context()
    handle = telemetry_module.SpanHandle(context)

    assert handle.span == "span"
    assert handle.closed is False
    handle.close()
    handle.close()
    assert context.exits == 1
    assert handle.closed is True


def test_session_telemetry_span_helpers_and_result_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Span:
        def __init__(self) -> None:
            self.attributes: list[tuple[str, Any]] = []
            self.statuses: list[Any] = []
            self.events: list[tuple[str, dict[str, Any]]] = []

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes.append((key, value))

        def set_status(self, status: Any) -> None:
            self.statuses.append(status)

        def add_event(self, name: str, attributes: dict[str, Any]) -> None:
            self.events.append((name, attributes))

    class Tracer:
        def __init__(self) -> None:
            self.spans: list[tuple[str, dict[str, Any], Span]] = []

        @contextlib.contextmanager
        def start_as_current_span(self, name: str, *, kind: Any, attributes: dict[str, Any]):
            span = Span()
            self.spans.append((name, {**attributes, "kind": kind}, span))
            yield span

    tracer = Tracer()
    monkeypatch.setattr(
        telemetry_module, "SpanKind", SimpleNamespace(INTERNAL="internal", CLIENT="client")
    )
    monkeypatch.setattr(
        telemetry_module, "Status", lambda code, description=None: (code, description)
    )
    monkeypatch.setattr(telemetry_module, "StatusCode", SimpleNamespace(OK="ok", ERROR="error"))
    telemetry = telemetry_module.SessionTelemetry(
        telemetry_module.SessionTelemetryConfig(enabled=True),
        _settings(),
    )
    telemetry._initialized = True
    telemetry._tracer = tracer
    conversation_id = uuid.uuid4()

    with telemetry.session_span(conversation_id, turn=4):
        pass
    with telemetry.turn_span(conversation_id, turn=4, step=2):
        pass
    with telemetry.tool_span(conversation_id, turn=4, step=2, tool_name="fs_read") as tool_span:
        telemetry.record_tool_result(tool_span, success=False, duration_ms=7, error="denied")
        telemetry.add_event(tool_span, "tool/rejected", {"reason": "policy"})
    with telemetry.provider_span(
        conversation_id, provider="openai", model="gpt-test", turn=4
    ) as provider_span:
        telemetry.record_provider_result(
            provider_span,
            success=True,
            input_tokens=3,
            output_tokens=5,
            latency_ms=11,
        )
    telemetry.record_tool_result(None, success=True, duration_ms=0)
    telemetry.record_provider_result(None, success=True)
    telemetry.add_event(None, "ignored")

    assert [row[0] for row in tracer.spans] == [
        "deepspace.session",
        "deepspace.turn",
        "deepspace.tool.fs_read",
        "deepspace.provider.openai",
    ]
    assert ("deepspace.tool.success", False) in tracer.spans[2][2].attributes
    assert tracer.spans[2][2].statuses[-1] == ("error", "denied")
    assert tracer.spans[2][2].events == [("tool/rejected", {"reason": "policy"})]
    assert ("deepspace.provider.output_tokens", 5) in tracer.spans[3][2].attributes
    assert tracer.spans[3][2].statuses[-1] == ("ok", None)
