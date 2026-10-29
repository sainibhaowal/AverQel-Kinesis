"""Tests for the DeepSpace proactive daemon.

The checks cover the daemon starting, the daemon refusing to start when the
deployment switches it off, the daemon stopping on a signal, and the daemon
refusing to start beside another daemon.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

import pytest

from app.core.config import Settings
from app.deepspace.workers.daemon import (
    EXIT_ALREADY_RUNNING,
    ProactiveDaemon,
    acquire_lock,
    release_lock,
)

pytestmark = pytest.mark.unit_no_db


def _settings(*, enabled: bool = True, interval: int = 1) -> Settings:
    """Build the deployment settings one test needs.

    Args:
        enabled: Whether the deployment switched the daemon on.
        interval: The number of seconds between two passes.

    Returns:
        The settings the daemon reads.
    """
    return Settings(
        deepspace_proactive_daemon_enabled=enabled,
        deepspace_proactive_daemon_interval_seconds=interval,
    )


def test_the_daemon_runs_its_pass_and_stops_cleanly(tmp_path: Path) -> None:
    passes: list[int] = []
    stop = threading.Event()

    def _pass() -> float:
        passes.append(1)
        stop.set()
        return 60.0

    daemon = ProactiveDaemon(
        settings=_settings(),
        run_pass=_pass,
        stop_event=stop,
        lock_path=tmp_path / "daemon.lock",
    )
    assert daemon.run() == 0
    assert passes == [1]


def test_the_daemon_does_not_start_when_the_deployment_switches_it_off(tmp_path: Path) -> None:
    passes: list[int] = []

    def _pass() -> float:
        passes.append(1)
        return 60.0

    daemon = ProactiveDaemon(
        settings=_settings(enabled=False),
        run_pass=_pass,
        lock_path=tmp_path / "daemon.lock",
    )
    assert daemon.run() == 0
    assert passes == []


def test_the_daemon_stops_when_the_process_is_told_to(tmp_path: Path) -> None:
    running = threading.Event()

    def _pass() -> float:
        running.set()
        return 60.0

    daemon = ProactiveDaemon(
        settings=_settings(),
        run_pass=_pass,
        lock_path=tmp_path / "daemon.lock",
    )

    def _signal() -> None:
        assert running.wait(timeout=10)
        os.kill(os.getpid(), signal.SIGTERM)

    signaller = threading.Thread(target=_signal, daemon=True)
    signaller.start()
    assert daemon.run() == 0
    signaller.join(timeout=10)


def test_a_second_daemon_refuses_to_start_beside_the_first(tmp_path: Path) -> None:
    lock = tmp_path / "daemon.lock"
    passes: list[int] = []

    def _pass() -> float:
        passes.append(1)
        return 60.0

    held = acquire_lock(lock)
    try:
        daemon = ProactiveDaemon(
            settings=_settings(),
            run_pass=_pass,
            lock_path=lock,
        )
        assert daemon.run() == EXIT_ALREADY_RUNNING
    finally:
        release_lock(held)
    assert passes == []


def test_the_daemon_releases_the_lock_when_it_stops(tmp_path: Path) -> None:
    lock = tmp_path / "daemon.lock"
    stop = threading.Event()

    def _pass() -> float:
        stop.set()
        return 60.0

    daemon = ProactiveDaemon(
        settings=_settings(),
        run_pass=_pass,
        stop_event=stop,
        lock_path=lock,
    )
    assert daemon.run() == 0
    handle = acquire_lock(lock)
    release_lock(handle)


def test_a_failing_pass_does_not_stop_the_daemon(tmp_path: Path) -> None:
    passes: list[int] = []
    stop = threading.Event()

    def _pass() -> float:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("the schedule could not be read")
        stop.set()
        return 60.0

    daemon = ProactiveDaemon(
        settings=_settings(),
        run_pass=_pass,
        stop_event=stop,
        lock_path=tmp_path / "daemon.lock",
    )
    assert daemon.run() == 0
    assert len(passes) == 2
