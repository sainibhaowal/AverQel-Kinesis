"""In-process schedule dispatch for the standalone desktop runtime.

The packaged Kinesis app runs without Celery beat, so the cron entry that
would normally call ``deepspace.dispatch_schedules`` never fires.  This module
starts a small poller inside the API process when the desktop runtime is
detected, and runs the same dispatcher on the same one-minute cadence.

The dispatcher itself keeps the durable claim rules (``FOR UPDATE SKIP
LOCKED``, a run row, and an advanced ``next_run_at``), so starting this poller
does not change the production Celery behavior.  It only fills the gap where
no beat process exists.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.averqel_home import is_averqel_desktop_runtime
from app.deepspace.workers.schedules import dispatch_schedules

logger = logging.getLogger(__name__)

#: How often the desktop poller looks for due schedules.
POLL_INTERVAL_SECONDS = 60.0


class LocalScheduleDispatcher:
    """Run due DeepSpace schedules inside the desktop API process."""

    def __init__(self, *, poll_interval_seconds: float = POLL_INTERVAL_SECONDS) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._warned = False

    @property
    def running(self) -> bool:
        """Return whether the poller task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the poller when this process is the desktop runtime."""
        if not is_averqel_desktop_runtime():
            return
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="deepspace-local-schedules")

    async def stop(self) -> None:
        """Stop the poller and wait briefly for an in-flight dispatch."""
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._task = None

    async def _run(self) -> None:
        """Poll for due schedules until stopped."""
        while not self._stop.is_set():
            try:
                dispatched = await asyncio.to_thread(dispatch_schedules)
                if dispatched:
                    logger.info("Dispatched %d due DeepSpace schedule(s)", dispatched)
            except Exception:  # noqa: BLE001 - the poller must survive one bad cycle
                if not self._warned:
                    logger.warning("Local DeepSpace schedule dispatch failed", exc_info=True)
                    self._warned = True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue


__all__ = ["LocalScheduleDispatcher", "POLL_INTERVAL_SECONDS"]
