"""Tests for the in-process desktop schedule poller.

The poller fills the gap where the packaged Kinesis app has no Celery beat to
call ``deepspace.dispatch_schedules``.  The checks cover the desktop gate, the
one-minute poll loop, and the rule that one failed dispatch never kills the
poller.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.deepspace.workers import local_schedule_dispatch as local

pytestmark = pytest.mark.unit_no_db


def test_the_poller_does_nothing_outside_the_desktop_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(local, "is_averqel_desktop_runtime", lambda: False)
    monkeypatch.setattr(local, "dispatch_schedules", lambda: calls.append(1) or 0)
    dispatcher = local.LocalScheduleDispatcher(poll_interval_seconds=0.01)

    async def run() -> None:
        await dispatcher.start()
        assert dispatcher.running is False
        await asyncio.sleep(0.05)
        await dispatcher.stop()

    asyncio.run(run())

    assert calls == []


def test_the_poller_dispatches_due_schedules_until_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(local, "is_averqel_desktop_runtime", lambda: True)

    def fake_dispatch() -> int:
        calls.append(1)
        return 1

    monkeypatch.setattr(local, "dispatch_schedules", fake_dispatch)
    dispatcher = local.LocalScheduleDispatcher(poll_interval_seconds=0.01)

    async def run() -> None:
        await dispatcher.start()
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert dispatcher.running is True
        await dispatcher.stop()
        assert dispatcher.running is False

    asyncio.run(run())

    assert calls


def test_a_failed_dispatch_does_not_stop_the_poller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(local, "is_averqel_desktop_runtime", lambda: True)

    def fake_dispatch() -> int:
        calls.append(1)
        raise RuntimeError("the schedule store is unavailable")

    monkeypatch.setattr(local, "dispatch_schedules", fake_dispatch)
    dispatcher = local.LocalScheduleDispatcher(poll_interval_seconds=0.01)

    async def run() -> None:
        await dispatcher.start()
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 2
        await dispatcher.stop()

    asyncio.run(run())


def test_the_default_cadence_matches_the_celery_beat_entry() -> None:
    # The cron entry calls the dispatcher once a minute; the desktop poller
    # keeps the same cadence so both runtimes schedule at the same rate.
    assert local.POLL_INTERVAL_SECONDS == 60.0


def test_the_dispatcher_starts_at_most_one_poll_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(local, "is_averqel_desktop_runtime", lambda: True)
    monkeypatch.setattr(local, "dispatch_schedules", lambda: calls.append(1) or 0)
    dispatcher = local.LocalScheduleDispatcher(poll_interval_seconds=0.01)

    async def run() -> None:
        await dispatcher.start()
        first = dispatcher._task
        await dispatcher.start()
        assert dispatcher._task is first
        await dispatcher.stop()

    asyncio.run(run())
