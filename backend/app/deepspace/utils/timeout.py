"""
DeepSpace Timeout Utilities - Python-native implementation of dsh-timeout.

Provides unified timeout arithmetic, signal fusion, idle watchdog, and classification.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

# ============================================================================
# Constants
# ============================================================================

MAX_TIMER_DELAY_MS = 2_147_483_647  # 2^31 - 1 (max setTimeout delay in Node.js)


# ============================================================================
# Types
# ============================================================================


class TimeoutReason(Exception):  # noqa: N818 - compatibility name is part of the public API
    """
    Internal abort reason carrying a capability-owned code and elapsed deadline.
    Providers translate it through `timeout_of` before returning to callers.
    """

    def __init__(self, code: str, timeout_ms: int) -> None:
        self.code = code
        self.timeout_ms = timeout_ms
        super().__init__(f"{code} after {timeout_ms}ms")

    def __reduce__(self) -> tuple:
        return (self.__class__, (self.code, self.timeout_ms))


@dataclass(frozen=True, slots=True)
class Deadline:
    """A deadline signal plus the cleanup that clears its timer (dispose-once)."""

    signal: asyncio.AbstractEvent
    dispose: callable

    def __enter__(self) -> Deadline:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.dispose()


class IdleWatchdog:
    """
    Rearmable timeout around one outstanding async-iterator demand.

    The timer exists only while `next()` is outstanding, so consumer think time
    does not count as provider idle time.
    """

    def __init__(
        self,
        upstream: asyncio.Event | None,
        timeout_ms: int,
        code: str,
    ) -> None:
        if not isinstance(timeout_ms, int) or timeout_ms <= 0 or timeout_ms > MAX_TIMER_DELAY_MS:
            raise ValueError(
                f"timeout_ms must be a positive finite integer <= {MAX_TIMER_DELAY_MS}"
            )

        self._timeout_ms = timeout_ms
        self._code = code
        self._upstream = upstream
        self._disposed = False
        self._outstanding = False
        self._timer: asyncio.TimerHandle | None = None
        self._reason = TimeoutReason(code, timeout_ms)

        # Create fused signal
        if upstream is None:
            self._signal = asyncio.Event()
        else:
            self._signal = asyncio.Event()
            # We'll manually set this when either upstream or timeout fires
            upstream.add_done_callback(lambda _: self._signal.set())

    @property
    def signal(self) -> asyncio.Event:
        """Stable signal aborted by upstream cancellation or this watchdog's timeout."""
        return self._signal

    async def next(self, iterator: Any) -> Any:
        """
        Await one iterator demand while the idle timer is armed.

        The deadline settles the demand even when the awaited work ignores
        its abort signal.
        """
        if self._disposed:
            raise RuntimeError("idleWatchdog is disposed")
        if self._outstanding:
            raise RuntimeError("idleWatchdog next is already outstanding")

        self._outstanding = True
        self._arm()

        # Create a future that completes when the iterator yields or timeout fires
        deadline_future = asyncio.get_event_loop().create_future()

        def on_timeout() -> None:
            if not deadline_future.done():
                deadline_future.set_exception(self._reason)

        def on_upstream() -> None:
            if not deadline_future.done():
                deadline_future.set_exception(asyncio.CancelledError())

        # Arm the timeout
        self._timer = asyncio.get_event_loop().call_later(self._timeout_ms / 1000.0, on_timeout)

        # Listen for upstream cancellation
        if self._upstream:
            self._upstream.add_done_callback(lambda _: on_upstream())

        try:
            # Race the iterator against the deadline
            iterator_task = asyncio.create_task(self._consume_iterator(iterator))
            done, pending = await asyncio.wait(
                [iterator_task, deadline_future],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # Return the result from the iterator
            return done.pop().result()
        finally:
            self._disarm()
            self._outstanding = False

    async def _consume_iterator(self, iterator: Any) -> Any:
        """Consume one value from an async iterator."""
        if hasattr(iterator, "__anext__"):
            return await iterator.__anext__()
        elif hasattr(iterator, "__next__"):
            return next(iterator)
        else:
            raise TypeError("Expected iterator with __anext__ or __next__")

    def _arm(self) -> None:
        """Arm the idle timer."""
        self._disarm()
        self._timer = asyncio.get_event_loop().call_later(
            self._timeout_ms / 1000.0,
            lambda: self._signal.set() if not self._signal.is_set() else None,
        )

    def _disarm(self) -> None:
        """Disarm the idle timer."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def pulse(self) -> None:
        """
        Rearm an outstanding demand after transport activity that yields no
        iterator value; otherwise a no-op.
        """
        if self._disposed or not self._outstanding:
            return
        self._arm()

    def dispose(self) -> None:
        """Clear the timer; safe to call once at the owning stream's exit."""
        if self._disposed:
            return
        self._disposed = True
        self._disarm()
        self._signal.set()


# ============================================================================
# Core Timeout Functions
# ============================================================================


def assert_timer_delay(timeout_ms: int, name: str = "timeoutMs") -> None:
    """Validate a timeout value."""
    if not isinstance(timeout_ms, int) or timeout_ms <= 0 or timeout_ms > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{name} must be a positive finite integer <= {MAX_TIMER_DELAY_MS}")


def clamp_timeout(
    requested: int | None,
    default: int,
    max_timeout: int,
    name: str = "timeoutMs",
) -> int:
    """
    Validate a caller's optional timeout hint, use the backend default,
    then cap it to the maximum.

    Args:
        requested: The caller's optional hint; validated when present.
        default: The backend default applied when `requested` is absent.
        max_timeout: The backend upper bound the result is capped to.
        name: Field name used in the thrown message.

    Returns:
        The effective timeout in milliseconds: `min(requested ?? default, max)`.
    """
    if requested is not None:
        if not isinstance(requested, int) or requested <= 0:
            raise ValueError(f"{name} must be a positive finite integer")
    return min(requested if requested is not None else default, max_timeout)


def deadline(
    upstream: asyncio.Event | None,
    timeout_ms: int,
    code: str,
) -> Deadline:
    """
    Fuse upstream cancellation with an identifiable timeout.

    Args:
        upstream: The caller's cancellation event, if any, fused into the result.
        timeout_ms: Deadline in milliseconds; <= 0 means "no timeout".
        code: Capability-owned code stamped onto the timeout's TimeoutReason.

    Returns:
        A Deadline with signal and cleanup disposer.
    """
    if timeout_ms <= 0:
        # No timeout (background work): forward only the upstream signal
        return Deadline(
            signal=upstream if upstream is not None else asyncio.Event(),
            dispose=lambda: None,
        )

    assert_timer_delay(timeout_ms, "deadline timeoutMs")

    timer_controller = asyncio.Event()
    timer_handle = asyncio.get_event_loop().call_later(
        timeout_ms / 1000.0, lambda: timer_controller.set()
    )

    # Fuse signals using wait
    if upstream is not None:
        fused_signal = asyncio.Event()

        def on_upstream() -> None:
            fused_signal.set()

        def on_timeout() -> None:
            timer_controller.set()
            fused_signal.set()

        upstream.add_done_callback(lambda _: on_upstream())

        return Deadline(
            signal=fused_signal,
            dispose=lambda: (
                timer_handle.cancel(),
                (
                    upstream.remove_done_callback(lambda _: on_upstream())
                    if hasattr(upstream, "remove_done_callback")
                    else None
                ),
            ),
        )
    else:
        # No upstream, just timeout
        def on_timeout() -> None:
            timer_controller.set()

        timer_handle = asyncio.get_event_loop().call_later(timeout_ms / 1000.0, on_timeout)

        return Deadline(signal=timer_controller, dispose=lambda: timer_handle.cancel())


def idle_watchdog(
    upstream: asyncio.Event | None,
    timeout_ms: int,
    code: str,
) -> IdleWatchdog:
    """
    Create a rearmable idle watchdog for an async iterator.

    Args:
        upstream: Caller cancellation fused into the stable signal.
        timeout_ms: Positive finite idle interval in milliseconds.
        code: Capability-owned code carried by the timeout reason.

    Returns:
        An IdleWatchdog with stable signal, guarded next operation, and timer disposer.
    """
    return IdleWatchdog(upstream, timeout_ms, code)


def timeout_of(
    x: asyncio.Event | BaseException | Any,
    code: str | None = None,
) -> TimeoutReason | None:
    """
    Recover a timeout reason from a reason-bearing object.

    Args:
        x: An asyncio.Event or any object with a `reason` attribute.
        code: When provided, only a TimeoutReason with this exact code matches.

    Returns:
        The matching TimeoutReason, or None.
    """
    # Handle asyncio.Event
    if isinstance(x, asyncio.Event):
        # asyncio.Event doesn't carry a reason directly in Python
        # This is a limitation - we'd need to track the reason separately
        return None

    # Handle exceptions with reason attribute
    if hasattr(x, "reason"):
        reason = x.reason
        if isinstance(reason, TimeoutReason):
            if code is None or reason.code == code:
                return reason

    # Handle BaseException with __cause__ or __context__
    if isinstance(x, BaseException):
        for attr in ("__cause__", "__context__"):
            reason = getattr(x, attr, None)
            if isinstance(reason, TimeoutReason):
                if code is None or reason.code == code:
                    return reason

    return None


# ============================================================================
# Async Context Manager Helpers
# ============================================================================


@contextlib.asynccontextmanager
async def with_deadline(
    upstream: asyncio.Event | None,
    timeout_ms: int,
    code: str,
) -> AsyncIterator[asyncio.Event]:
    """Async context manager for deadline."""
    deadline_obj = deadline(upstream, timeout_ms, code)
    try:
        yield deadline_obj.signal
    finally:
        deadline_obj.dispose()


@contextlib.asynccontextmanager
async def with_idle_watchdog(
    upstream: asyncio.Event | None,
    timeout_ms: int,
    code: str,
) -> AsyncIterator[IdleWatchdog]:
    """Async context manager for idle watchdog."""
    watchdog = idle_watchdog(upstream, timeout_ms, code)
    try:
        yield watchdog
    finally:
        watchdog.dispose()


# ============================================================================
# Synchronous Helpers
# ============================================================================


class SyncDeadline:
    """Synchronous deadline for non-async code."""

    def __init__(self, timeout_ms: int, code: str) -> None:
        assert_timer_delay(timeout_ms, "SyncDeadline timeoutMs")
        self._timeout_ms = timeout_ms
        self._code = code
        self._deadline = time.monotonic() + timeout_ms / 1000.0
        self._timer: threading.Timer | None = None
        self._reason = TimeoutReason(code, timeout_ms)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._deadline

    @property
    def reason(self) -> TimeoutReason:
        return self._reason

    def check(self) -> None:
        """Raise TimeoutReason if expired."""
        if self.expired:
            raise self._reason

    def reset(self) -> None:
        """Reset the deadline."""
        self._deadline = time.monotonic() + self._timeout_ms / 1000.0

    def __enter__(self) -> SyncDeadline:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._timer = None


def with_timeout(
    timeout_ms: int,
    code: str,
    func: Callable,
    *args,
    **kwargs,
) -> Any:
    """
    Execute a function with a timeout.

    Args:
        timeout_ms: Timeout in milliseconds.
        code: Capability-owned timeout code.
        func: Function to execute.
        *args, **kwargs: Arguments to pass to func.

    Returns:
        The result of func.

    Raises:
        TimeoutReason: If the timeout expires.
    """
    deadline = SyncDeadline(timeout_ms, code)
    try:
        return func(*args, **kwargs)
    finally:
        deadline.check()


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    # Constants
    "MAX_TIMER_DELAY_MS",
    # Types
    "TimeoutReason",
    "Deadline",
    "IdleWatchdog",
    # Core functions
    "clamp_timeout",
    "deadline",
    "idle_watchdog",
    "timeout_of",
    # Context managers
    "with_deadline",
    "with_idle_watchdog",
    # Sync helpers
    "SyncDeadline",
    "with_timeout",
    # Constants
    "MAX_TIMER_DELAY_MS",
]
