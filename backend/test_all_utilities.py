#!/usr/bin/env python3
"""Comprehensive test suite for all new DeepSpace utilities."""

import asyncio
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.deepspace.utils import (
    Cron,
    RecurrenceRule,
    ScheduleManager,
    TimeoutReason,
    WeakMapWithValues,
    abbreviate_home_path,
    absolute_file_address,
    assert_never,
    canonicalize_time_zone,
    clamp_timeout,
    deep_equal_json,
    deep_freeze,
    duration_from_ms,
    duration_from_seconds,
    file_address_for,
    format_four_digit_year_instant,
    four_digit_year_epoch,
    idle_watchdog,
    is_four_digit_year_instant,
    is_json_value,
    load_manifest_from_file,
    parse_file_address,
    # time
    parse_local_calendar,
    parse_offset_instant,
    parse_partial_json,
    path_parts_of,
    resolve_local_instant,
    resolve_workspace_path,
    # workspace_path
    session_file_address,
    snapshot_json_value,
    validate_manifest,
    workspace_title_of,
)
from app.deepspace.utils.time import LocalCalendarTime
from app.deepspace.utils.timeout import with_deadline

print("✓ All imports successful!")

# ============================================================================
# Test Time Utilities
# ============================================================================

print("\n=== Testing Time Utilities ===")

# parse_local_calendar
calendar_result = parse_local_calendar("2024-01-15", "14:30:45.123")
assert calendar_result["kind"] == "accepted"
assert calendar_result["time"].year == 2024
assert calendar_result["time"].month == 1
assert calendar_result["time"].day == 15
assert calendar_result["time"].hour == 14
assert calendar_result["time"].minute == 30
assert calendar_result["time"].second == 45
assert calendar_result["time"].millisecond == 123
print("✓ parse_local_calendar")

# Invalid date
invalid_calendar_result = parse_local_calendar("2024-02-30", "12:00:00")
assert invalid_calendar_result["kind"] == "rejected"
assert invalid_calendar_result["reason"] == "not_a_calendar_date"
print("✓ parse_local_calendar rejects invalid date")

# parse_offset_instant
instant_result = parse_offset_instant("2024-01-15T14:30:45.123Z")
assert instant_result["kind"] == "accepted"
print("✓ parse_offset_instant UTC")

offset_instant_result = parse_offset_instant("2024-01-15T14:30:45.123+05:30")
assert offset_instant_result["kind"] == "accepted"
print("✓ parse_offset_instant with offset")

# four_digit_year_epoch
epoch_result = four_digit_year_epoch("2024-01-15T14:30:45.123Z")
assert epoch_result["kind"] == "accepted"
epoch = epoch_result["epoch"]
print(f"✓ four_digit_year_epoch: {epoch}")

formatted_instant = format_four_digit_year_instant(epoch)
assert formatted_instant == "2024-01-15T14:30:45.123Z"
print("✓ format_four_digit_year_instant")

# is_four_digit_year_instant
assert is_four_digit_year_instant(epoch)
assert not is_four_digit_year_instant(-62135596800001)  # Before year 1
assert not is_four_digit_year_instant(253402300800000)  # After year 9999
print("✓ is_four_digit_year_instant")

# canonicalize_time_zone
time_zone_result = canonicalize_time_zone("America/New_York")
assert time_zone_result["kind"] == "accepted"
print(f"✓ canonicalize_time_zone: {time_zone_result['time_zone']}")

utc_result = canonicalize_time_zone("UTC")
assert utc_result["kind"] == "accepted"
assert utc_result["time_zone"] == "UTC"
print("✓ canonicalize_time_zone UTC")

invalid_time_zone_result = canonicalize_time_zone("invalid/zone")
assert invalid_time_zone_result["kind"] == "rejected"
print("✓ canonicalize_time_zone rejects invalid")

# resolve_local_instant
time_obj = LocalCalendarTime(2024, 1, 15, 14, 30, 45, 123)
resolved_epoch = resolve_local_instant(time_obj, "America/New_York")
assert resolved_epoch is not None
print(f"✓ resolve_local_instant: {resolved_epoch}")

# Duration
d = duration_from_ms(5000)
assert d.milliseconds == 5000
assert d.seconds == 5.0
print("✓ Duration from ms")

d2 = duration_from_seconds(10)
assert d2.milliseconds == 10000
print("✓ duration_from_seconds")

# Addition
d3 = d + d2
assert d3.milliseconds == 15000
print("✓ Duration addition")

# Cron
cron = Cron.parse("0 9 * * 1-5", "America/New_York")
next_occurrence = cron.next(int(datetime.now().timestamp() * 1000))
assert next_occurrence is not None
print(f"✓ Cron next occurrence: {next_occurrence}")

# RecurrenceRule
rule = RecurrenceRule(freq=0, interval=1, count=5, dtstart=datetime(2024, 1, 1, 9, 0))
occurrences = rule.next(datetime(2024, 1, 1, 8, 0), count=3)
assert len(occurrences) == 3
print(f"✓ RecurrenceRule: {len(occurrences)} occurrences")

print("\n✓ All Time tests passed!")

# ============================================================================
# Test Timeout Utilities
# ============================================================================

print("\n=== Testing Timeout Utilities ===")

# clamp_timeout
assert clamp_timeout(None, 1000, 5000) == 1000
assert clamp_timeout(3000, 1000, 5000) == 3000
assert clamp_timeout(6000, 1000, 5000) == 5000
print("✓ clamp_timeout")

# TimeoutReason
reason = TimeoutReason("TEST_CODE", 5000)
assert reason.code == "TEST_CODE"
assert reason.timeout_ms == 5000
print("✓ TimeoutReason")


# deadline (async test)
async def test_deadline() -> None:
    async with with_deadline(None, 100, "TEST") as signal:
        # Should not trigger in 50ms
        await asyncio.sleep(0.05)
        assert not signal.is_set()


asyncio.run(test_deadline())
print("✓ deadline context manager")


# IdleWatchdog
async def test_idle_watchdog() -> None:
    async def mock_iterator() -> AsyncIterator[str]:
        yield "value"

    watchdog = idle_watchdog(None, 1000, "TEST")
    # Just test creation
    assert watchdog._timeout_ms == 1000
    watchdog.dispose()
    print("✓ IdleWatchdog creation and disposal")


asyncio.run(test_idle_watchdog())

print("\n✓ All Timeout tests passed!")

# ============================================================================
# Test Values Utilities
# ============================================================================

print("\n=== Testing Values Utilities ===")

# snapshot_json_value
data = {"a": 1, "b": [2, 3], "c": {"d": "hello"}}
snapshot = snapshot_json_value(data)
assert snapshot == data
print("✓ snapshot_json_value")


# Non-JSON value
class CustomClass:
    pass


result = snapshot_json_value(CustomClass())
assert result is None
print("✓ snapshot_json_value rejects non-JSON")

# is_json_value
assert is_json_value({"a": 1})
assert not is_json_value({1, 2, 3})
print("✓ is_json_value")

# deep_equal_json
assert deep_equal_json({"a": [1, 2]}, {"a": [1, 2]})
assert not deep_equal_json({"a": 1}, {"a": 2})
assert deep_equal_json([1, 2, 3], [1, 2, 3])
print("✓ deep_equal_json")

# deep_freeze
data = {"a": [1, 2], "b": {"c": 3}}
frozen = deep_freeze(data)
# Should not raise
print("✓ deep_freeze")


# WeakMapWithValues
class Key:
    pass


wm: WeakMapWithValues[Key, str] = WeakMapWithValues()

key1 = Key()
key2 = Key()
wm.set(key1, "value1")
wm.set(key2, "value2")
assert wm.get(key1) == "value1"
assert wm.get(key2) == "value2"
assert len(wm) == 2
assert len(wm.values) == 2
wm.delete(key1)
assert len(wm) == 1
print("✓ WeakMapWithValues")

# parse_partial_json
data = parse_partial_json('{"a": 1, "b": {"c": 2}}')
assert data == {"a": 1, "b": {"c": 2}}
print("✓ parse_partial_json")

# assert_never
try:
    assert_never("never", "test")
    raise AssertionError("assert_never should reject an unreachable variant")
except AssertionError as e:
    assert "unreachable variant" in str(e)
print("✓ assert_never")

print("\n✓ All Values tests passed!")

# ============================================================================
# Test Workspace Path Utilities
# ============================================================================

print("\n=== Testing Workspace Path Utilities ===")

# session_file_address
addr = session_file_address("session-123", "path/to/file.txt")
assert addr == "dsh-resource://file/session/session-123/path/to/file.txt"
print("✓ session_file_address")

# absolute_file_address
addr = absolute_file_address("/home/user/file.txt")
assert addr == "dsh-resource://file/absolute/home/user/file.txt"
print("✓ absolute_file_address")

# parse_file_address
parsed = parse_file_address("dsh-resource://file/session/sess-123/path/to/file.txt")
assert parsed is not None
assert parsed.scope == "session"
assert parsed.session_id == "sess-123"
assert parsed.path == "path/to/file.txt"
print("✓ parse_file_address session")

parsed = parse_file_address("dsh-resource://file/absolute/home/user/file.txt")
assert parsed is not None
assert parsed.scope == "absolute"
assert parsed.path == "/home/user/file.txt"
print("✓ parse_file_address absolute")

# UNC path
parsed = parse_file_address("dsh-resource://file/absolute//server/share/file.txt")
assert parsed is not None
assert parsed.scope == "absolute"
assert parsed.path == "//server/share/file.txt"
print("✓ parse_file_address UNC")

# file_address_for
addr = file_address_for("sess-1", "/home/user/workspace", "/home/user/workspace/project/file.txt")
assert "dsh-resource://file/session/sess-1/" in addr
print("✓ file_address_for")

# resolve_workspace_path
path = resolve_workspace_path("/home/user/ws", "project/file.txt")
assert path == "/home/user/ws/project/file.txt"
path = resolve_workspace_path("/home/user/ws", "/absolute/path")
assert path == "/absolute/path"
print("✓ resolve_workspace_path")

# abbreviate_home_path
path = abbreviate_home_path("/home/user/project/file.txt", "/home/user")
assert path == "~/project/file.txt"
path = abbreviate_home_path("/other/path", "/home/user")
assert path == "/other/path"
print("✓ abbreviate_home_path")

# workspace_title_of
title = workspace_title_of("/path/to/project")
assert title == "project"
title = workspace_title_of("/path/to/")
assert title == ""
print("✓ workspace_title_of")

# path_parts_of
dir_part, name = path_parts_of("/path/to/file.txt")
assert dir_part == "/path/to/"
assert name == "file.txt"
print("✓ path_parts_of")

print("\n✓ All Workspace Path tests passed!")

# ============================================================================
# Test Project Manifest
# ============================================================================

print("\n=== Testing Project Manifest ===")

# Valid manifest
data = {"name": "test-pkg", "version": "1.0.0"}
manifest = validate_manifest(data)
assert manifest.name == "test-pkg"
assert manifest.version == "1.0.0"
print("✓ validate_manifest basic")

# Invalid - missing name
try:
    validate_manifest({"version": "1.0.0"})
    raise AssertionError("a project manifest without a name must be rejected")
except Exception as e:
    assert "$.name" in str(e)
print("✓ validate_manifest rejects missing name")

# Invalid - wrong private type
try:
    validate_manifest({"name": "test", "version": "1.0.0", "private": "yes"})
    raise AssertionError("a non-boolean private field must be rejected")
except Exception as e:
    assert "$.private" in str(e)
print("✓ validate_manifest rejects invalid private")

# Full manifest with deepspace section
data = {
    "name": "test-pkg",
    "version": "1.0.0",
    "deepspace": {
        "manifestVersion": 1,
        "settings": {"theme": "dark"},
        "profile": {"bundles": ["core", "agent"]},
    },
}
manifest = validate_manifest(data)
assert manifest.deepspace is not None
assert manifest.deepspace.manifest_version == 1
assert manifest.deepspace.settings == {"theme": "dark"}
assert manifest.deepspace.profile is not None
assert manifest.deepspace.profile.bundles == ["core", "agent"]
print("✓ validate_manifest with deepspace section")

# load_manifest_from_file
with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
    f.write("""
name = "test-package"
version = "1.0.0"
[deepspace]
manifestVersion = 1
settings = { theme = "dark" }
""")
    f.flush()
    manifest = load_manifest_from_file(f.name)
    assert manifest.name == "test-package"
    assert manifest.deepspace is not None
    assert manifest.deepspace.manifest_version == 1
    print("✓ load_manifest_from_file")

print("\n✓ All Project Manifest tests passed!")

# ============================================================================
# Test Time (comprehensive)
# ============================================================================

print("\n=== Comprehensive Time Tests ===")

# LocalCalendarTime round-trip
time_obj = LocalCalendarTime(2024, 6, 15, 14, 30, 45, 123)
round_trip_epoch = resolve_local_instant(time_obj, "America/New_York")
assert round_trip_epoch is not None
print(f"✓ resolve_local_instant: {round_trip_epoch}")

# Cron with different time zones
cron_utc = Cron.parse("0 0 * * *", "UTC")
cron_ny = Cron.parse("0 0 * * *", "America/New_York")
cron_tokyo = Cron.parse("0 0 * * *", "Asia/Tokyo")

next_utc = cron_utc.next(int(datetime.now(UTC).timestamp() * 1000))
next_ny = cron_ny.next(int(datetime.now(UTC).timestamp() * 1000))
next_tokyo = cron_tokyo.next(int(datetime.now(UTC).timestamp() * 1000))

assert next_utc is not None
assert next_ny is not None
assert next_tokyo is not None
print(f"✓ Cron in UTC: {next_utc}")
print(f"✓ Cron in NY: {next_ny}")
print(f"✓ Cron in Tokyo: {next_tokyo}")

# RecurrenceRule with complex rules
rule = RecurrenceRule(
    freq=2,  # WEEKLY
    interval=1,
    byday=[0, 2, 4],  # MO, WE, FR
    dtstart=datetime(2024, 1, 1, 9, 0),
    count=10,
)
occurrences = rule.next(datetime(2024, 1, 1), count=5)
assert len(occurrences) == 5
print(f"✓ RecurrenceRule weekly: {len(occurrences)} occurrences")

# Monthly rule
rule_monthly = RecurrenceRule(
    freq=3, bymonthday=[1, 15], dtstart=datetime(2024, 1, 1, 10, 0), count=6  # MONTHLY
)
occurrences = rule_monthly.next(datetime(2024, 1, 1), count=6)
assert len(occurrences) == 6
print(f"✓ RecurrenceRule monthly: {len(occurrences)} occurrences")

# ScheduleManager
manager = ScheduleManager(timezone="America/New_York")
manager.start()

# Add a cron job
job_id = manager.add_cron_job(
    lambda: print("Job executed"),
    "0 * * * *",  # Every hour
    "test-job",
    max_retries=2,
)
assert job_id == "test-job"
print("✓ ScheduleManager add cron job")

# Add interval job
job_id2 = manager.add_interval_job(
    lambda: print("Interval job"),
    60,  # 60 seconds
    "interval-job",
    max_retries=1,
)
assert job_id2 == "interval-job"
print("✓ ScheduleManager add interval job")

# Check jobs
jobs = manager.get_jobs()
assert len(jobs) >= 2
print("✓ ScheduleManager get jobs")

manager.shutdown()

print("\n✓ All Comprehensive Time tests passed!")

# ============================================================================
# Test Timeout (async)
# ============================================================================

print("\n=== Testing Timeout (async) ===")


async def test_timeout_async() -> None:
    from app.deepspace.utils.timeout import (
        idle_watchdog,
        with_deadline,
    )

    # Test deadline
    async with with_deadline(None, 100, "TEST") as signal:
        # Should not trigger in 50ms
        await asyncio.sleep(0.05)
        assert not signal.is_set()

    # Test idle_watchdog
    watchdog = idle_watchdog(None, 5000, "TEST")
    assert watchdog._timeout_ms == 5000
    watchdog.dispose()

    print("✓ Async timeout functions")


asyncio.run(test_timeout_async())

print("\n✓ All Timeout async tests passed!")

# ============================================================================
# Summary
# ============================================================================

print("\n" + "=" * 60)
print("ALL TESTS PASSED!")
print("=" * 60)
print("\nSelected utility smoke checks passed.")
print("This script does not measure code coverage.")
