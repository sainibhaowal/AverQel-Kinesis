"""The DeepSpace proactive daemon.

A deployment that does not run the Celery beat scheduler still needs the
product's timed work: the DeepSpace schedule dispatch that starts a
conversation's recurring follow-ups, the maintenance jobs, the notification
outbox, the connector synchronisation, and the MCP catalogue refresh.  The
daemon runs that work in one process, on the schedule the application
already declares, so the jobs are the same jobs and the timing is Celery's
own.

The daemon is off by default.  ``deepspace_proactive_daemon_enabled`` turns
it on, and ``deepspace_proactive_daemon_interval_seconds`` says how long the
daemon may sleep between two passes over the schedule.

One daemon runs at a time.  The process holds an exclusive lock on a file
under the AverQel home, so a second daemon refuses to start instead of
sending the same work twice.  The operating system releases the lock when
the process ends, so a daemon that was killed does not keep it.

The daemon stops on ``SIGTERM`` and on ``SIGINT``.  The signal sets a flag,
the loop finishes the pass it is in, and the process leaves with the code 0.
"""

from __future__ import annotations

import errno
import logging
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: The file the daemon locks, so that only one daemon sends the work.
LOCK_FILE_NAME = "deepspace-proactive-daemon.lock"

#: The exit code the daemon leaves with when another daemon holds the lock.
EXIT_ALREADY_RUNNING = 1


class DaemonAlreadyRunningError(RuntimeError):
    """Another daemon already holds the single-instance lock."""


class DaemonLockUnavailableError(RuntimeError):
    """The host offers no file lock, so the daemon cannot run alone."""


def daemon_lock_path() -> Path:
    """Return the file the daemon locks.

    Returns:
        The path of the lock file under the AverQel home.
    """
    from app.core.averqel_home import averqel_cache_path

    return averqel_cache_path() / LOCK_FILE_NAME


def acquire_lock(path: Path) -> IO[bytes]:
    """Take the exclusive daemon lock.

    The lock is held until the handle is closed or the process ends.  It is
    not left behind by a process that was killed, because the operating
    system releases it with the file descriptor.

    Args:
        path: The lock file.

    Returns:
        The open lock handle.

    Raises:
        DaemonAlreadyRunningError: When another daemon holds the lock.
        DaemonLockUnavailableError: When the host offers no file lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - only Windows lacks fcntl
        handle.close()
        raise DaemonLockUnavailableError(
            "This host offers no file lock, so the DeepSpace proactive daemon cannot run."
        ) from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise DaemonAlreadyRunningError(
                "Another DeepSpace proactive daemon already holds the lock."
            ) from exc
        raise
    return handle


def release_lock(handle: IO[bytes]) -> None:
    """Release the exclusive daemon lock.

    Args:
        handle: The open lock handle.
    """
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError) as exc:  # pragma: no cover - the handle closes anyway
        logger.debug("The DeepSpace proactive daemon lock was already released: %s", exc)
    finally:
        handle.close()


class BeatScheduleRunner:
    """The product's own timed schedule, run in this process.

    The runner uses Celery's own beat scheduler, so the timing and the
    dispatch are the ones the beat process uses.  The schedule is the one
    the application declares, which includes the DeepSpace schedule
    dispatch, the maintenance jobs, the notification outbox, the connector
    synchronisation, and the MCP catalogue refresh.
    """

    def __init__(self, app: Any) -> None:
        """Create the runner.

        Args:
            app: The Celery application that declares the schedule.
        """
        from celery.beat import Scheduler

        self._scheduler = Scheduler(app=app)

    def run_pass(self) -> float:
        """Send the work that is due.

        Returns:
            The number of seconds until the next pass.
        """
        return float(self._scheduler.tick())


def _default_pass(settings: Settings) -> Callable[[], float]:
    """Return the pass the daemon runs when the caller names none.

    Args:
        settings: The deployment settings in force.

    Returns:
        A callable that runs one pass over the product's timed schedule.
    """
    from app.platform.worker.celery_app import celery_app

    runner = BeatScheduleRunner(celery_app)
    return runner.run_pass


class ProactiveDaemon:
    """The DeepSpace proactive daemon.

    The daemon runs the product's timed work on a timer.  It is off when
    the deployment does not turn it on, it refuses to start beside another
    daemon, and it stops when it is asked to.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        run_pass: Callable[[], float] | None = None,
        stop_event: threading.Event | None = None,
        lock_path: Path | None = None,
    ) -> None:
        """Create the daemon.

        Args:
            settings: The deployment settings in force.
            run_pass: The pass the daemon runs on every tick, when the
                caller names none.  It returns the number of seconds until
                the next pass.
            stop_event: The event that asks the daemon to stop, when the
                caller names none.
            lock_path: The file the daemon locks, when the caller names
                none.
        """
        self.settings = settings
        self._run_pass = run_pass
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._lock_path = lock_path if lock_path is not None else daemon_lock_path()

    def request_stop(self) -> None:
        """Ask the daemon to stop after the pass it is in."""
        self._stop.set()

    def _pass(self) -> Callable[[], float]:
        """Return the pass the daemon runs on every tick."""
        if self._run_pass is None:
            self._run_pass = _default_pass(self.settings)
        return self._run_pass

    def run(self) -> int:
        """Run the daemon until it is asked to stop.

        Returns:
            The exit code.  It is 0 when the daemon stopped on a signal and
            1 when another daemon already holds the lock.
        """
        if not self.settings.deepspace_proactive_daemon_enabled:
            logger.info("The DeepSpace proactive daemon is switched off.")
            return 0
        try:
            handle = acquire_lock(self._lock_path)
        except DaemonAlreadyRunningError:
            logger.error("Another DeepSpace proactive daemon is already running.")
            return EXIT_ALREADY_RUNNING
        logger.info("The DeepSpace proactive daemon has started.")
        try:
            self._install_signal_handlers()
            self._loop()
        finally:
            release_lock(handle)
        logger.info("The DeepSpace proactive daemon has stopped.")
        return 0

    def _install_signal_handlers(self) -> None:
        """Ask the daemon to stop when the process is told to."""

        def _handle(signum: int, frame: object) -> None:
            logger.info("The DeepSpace proactive daemon was asked to stop.")
            self.request_stop()

        for number in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(number, _handle)
            except ValueError:
                logger.warning(
                    "The DeepSpace proactive daemon could not install a signal handler",
                    exc_info=True,
                )

    def _loop(self) -> None:
        """Run a pass over the schedule until the daemon is asked to stop."""
        interval = float(self.settings.deepspace_proactive_daemon_interval_seconds)
        run_pass = self._pass()
        while not self._stop.is_set():
            delay = interval
            try:
                delay = float(run_pass())
            except Exception:  # noqa: BLE001
                logger.warning("A DeepSpace proactive daemon pass failed", exc_info=True)
            self._stop.wait(max(0.0, min(delay, interval)))


def main() -> int:
    """Run the DeepSpace proactive daemon.

    Returns:
        The exit code of the daemon.
    """
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    return ProactiveDaemon(settings=settings).run()


if __name__ == "__main__":  # pragma: no cover - the module runs as a program
    raise SystemExit(main())
