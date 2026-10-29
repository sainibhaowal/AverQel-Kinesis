"""Tests for time context service."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app.deepspace.services.time_context import (
    BrowserTimeZoneContext,
    TimeContextService,
    format_duration,
    format_injection,
    format_timestamp,
    parse_browser_time_zone,
    render_browser_time_zone,
)


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(1000) == "1s"
    assert format_duration(60000) == "1m 0s"
    assert format_duration(3600000) == "1h 0s"
    assert format_duration(86400000) == "1d 0s"
    assert format_duration(90000) == "1m 30s"


def test_format_timestamp():
    ts = 1700000000  # 2023-11-14 22:13:20 UTC
    formatted = format_timestamp(ts, "UTC")
    assert "2023-11-14" in formatted
    assert "22:13:20" in formatted
    assert "UTC" in formatted


def test_render_browser_time_zone():
    # No context
    assert render_browser_time_zone(None) == "Browser time zone: unavailable"

    # With context
    ctx = BrowserTimeZoneContext(time_zone="America/Los_Angeles", offset_minutes=-480, is_dst=True)
    rendered = render_browser_time_zone(ctx)
    assert "America/Los_Angeles" in rendered
    assert "UTC-08:00" in rendered


def test_format_injection():
    now = time.time()
    browser_ctx = BrowserTimeZoneContext(time_zone="UTC", offset_minutes=0, is_dst=False)

    # First step
    injection = format_injection(
        now, turn=1, step=1, previous_event_time=None, time_zone="UTC", browser_context=browser_ctx
    )
    assert "turn 1, step 1" in injection
    assert "model-visible message" in injection
    assert "unavailable" in injection

    # Later step with previous event
    prev = now - 10
    injection = format_injection(
        now, turn=2, step=3, previous_event_time=prev, time_zone="UTC", browser_context=browser_ctx
    )
    assert "turn 2, step 3" in injection
    assert "step context" in injection
    assert "10s" in injection


def test_parse_browser_time_zone():
    # Valid data
    data = {"timeZone": "Europe/London", "offsetMinutes": 60, "isDST": True}
    ctx = parse_browser_time_zone(data)
    assert ctx is not None
    assert ctx.time_zone == "Europe/London"
    assert ctx.offset_minutes == 60
    assert ctx.is_dst is True

    # Missing data
    assert parse_browser_time_zone(None) is None
    assert parse_browser_time_zone({}) is None  # Empty dict returns None

    # Invalid data
    assert parse_browser_time_zone({"offsetMinutes": "invalid"}) is None


def test_time_context_service_basic():
    service = TimeContextService()
    conv_id = uuid4()

    # Initial projection
    proj = service.get_projection(conv_id)
    assert proj.last_message_time is None
    assert proj.last_injection_time is None
    assert proj.last_turn_injection_time is None

    # Update projection
    now = time.time()
    service.update_projection(conv_id, last_message_time=now)
    proj = service.get_projection(conv_id)
    assert proj.last_message_time == now


def test_time_context_should_inject():
    service = TimeContextService()
    conv_id = uuid4()

    # refresh_interval_ms = 0 means always inject
    assert service.should_inject(conv_id, 1, 1, 0) is True
    assert service.should_inject(conv_id, 1, 2, 0) is True

    # With interval, first injection always happens
    assert service.should_inject(conv_id, 1, 1, 60000) is True


def test_time_context_prepare_injection():
    service = TimeContextService()
    conv_id = uuid4()

    # Disabled
    class FakeSettings:
        deepspace_time_context_enabled = False
        deepspace_time_context_time_zone = "UTC"
        deepspace_time_context_refresh_interval_ms = 0

    result = service.prepare_injection(conv_id, 1, 1, None, FakeSettings(), None)
    assert result is None

    # Enabled
    class FakeSettings2:
        deepspace_time_context_enabled = True
        deepspace_time_context_time_zone = "UTC"
        deepspace_time_context_refresh_interval_ms = 0

    result = service.prepare_injection(conv_id, 1, 1, None, FakeSettings2(), None)
    assert result is not None
    assert "turn 1, step 1" in result
    assert "UTC" in result


def test_time_context_record_message_time():
    service = TimeContextService()
    conv_id = uuid4()

    now = time.time()
    service.record_message_time(conv_id, now)
    proj = service.get_projection(conv_id)
    assert proj.last_message_time == now


def test_time_context_injection_interval():
    service = TimeContextService()
    conv_id = uuid4()

    class FakeSettings:
        deepspace_time_context_enabled = True
        deepspace_time_context_time_zone = "UTC"
        deepspace_time_context_refresh_interval_ms = 1000  # 1 second

    # First injection
    result1 = service.prepare_injection(conv_id, 1, 1, None, FakeSettings(), None)
    assert result1 is not None

    # Immediate second injection should be skipped due to interval
    result2 = service.prepare_injection(conv_id, 1, 2, None, FakeSettings(), None)
    assert result2 is None

    # Wait for interval
    time.sleep(1.1)
    result3 = service.prepare_injection(conv_id, 1, 3, None, FakeSettings(), None)
    assert result3 is not None


def test_time_context_per_conversation_isolation():
    service = TimeContextService()
    conv1 = uuid4()
    conv2 = uuid4()

    class FakeSettings:
        deepspace_time_context_enabled = True
        deepspace_time_context_time_zone = "UTC"
        deepspace_time_context_refresh_interval_ms = 0

    service.prepare_injection(conv1, 1, 1, None, FakeSettings(), None)

    # conv2 should not be affected
    proj1 = service.get_projection(conv1)
    proj2 = service.get_projection(conv2)
    assert proj1.last_injection_time is not None
    assert proj2.last_injection_time is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
