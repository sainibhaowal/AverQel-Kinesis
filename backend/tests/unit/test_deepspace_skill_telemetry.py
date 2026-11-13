"""Tests for skill telemetry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.deepspace.services import session_telemetry
from app.deepspace.services.skill_telemetry import (
    SkillTelemetryEvent,
    record_skill_event,
    reset_skill_counters,
    skill_counters,
)

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(autouse=True)
def _clean_counters():
    reset_skill_counters()
    yield
    reset_skill_counters()


def test_counts_and_serializes_events() -> None:
    record_skill_event(
        SkillTelemetryEvent(
            action="load",
            name="demo",
            provider="filesystem",
            source="project",
            duration_ms=12.3456,
        )
    )
    record_skill_event(SkillTelemetryEvent(action="load", name="missing", outcome="not_found"))
    counters = skill_counters()

    assert counters["load:ok"] == 1
    assert counters["load:not_found"] == 1
    assert counters["provider:filesystem"] == 1
    event = SkillTelemetryEvent(action="resource", name="demo", duration_ms=1.0)
    assert event.to_dict()["duration_ms"] == 1.0
    assert event.to_dict()["action"] == "resource"


def test_disabled_telemetry_never_reaches_session_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_settings):
        raise AssertionError("session telemetry must not be created")

    monkeypatch.setattr(session_telemetry, "get_session_telemetry", fail)
    settings = SimpleNamespace(deepspace_skill_telemetry_enabled=False)

    record_skill_event(
        SkillTelemetryEvent(action="load", name="demo"),
        conversation_id="conversation",
        settings=settings,
    )
    assert skill_counters()["load:ok"] == 1


def test_enabled_telemetry_exports_a_content_free_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class FakeTelemetry:
        def record_session_event(self, *, conversation_id: str, event: dict) -> None:
            captured.append({"conversation_id": conversation_id, "event": event})

    monkeypatch.setattr(
        session_telemetry, "get_session_telemetry", lambda _settings: FakeTelemetry()
    )
    settings = SimpleNamespace(deepspace_skill_telemetry_enabled=True)

    record_skill_event(
        SkillTelemetryEvent(action="load", name="demo", provider="filesystem"),
        conversation_id="conversation-1",
        settings=settings,
    )

    assert len(captured) == 1
    assert captured[0]["conversation_id"] == "conversation-1"
    event = captured[0]["event"]
    assert event["type"] == "skill_load"
    assert event["tool"] == "demo"
    assert "instructions" not in event


def test_export_failure_never_breaks_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_settings):
        raise RuntimeError("exporter offline")

    monkeypatch.setattr(session_telemetry, "get_session_telemetry", fail)
    settings = SimpleNamespace(deepspace_skill_telemetry_enabled=True)

    record_skill_event(
        SkillTelemetryEvent(action="load", name="demo"),
        conversation_id="conversation-1",
        settings=settings,
    )
    assert skill_counters()["load:ok"] == 1
