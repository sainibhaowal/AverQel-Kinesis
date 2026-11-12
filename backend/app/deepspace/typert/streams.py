"""Bidirectional capability streams with an uplink byte window.

A stream carries two independent directions:

* the owner publishes output values, which the caller reads by iterating
  the stream;
* the caller sends uplink values through :class:`RemoteStreamHandle`.

The uplink has a byte window.  ``send`` waits while the unread uplink bytes
plus the new item would exceed the window, so a fast caller cannot grow an
unbounded queue.  One item larger than the window is still accepted when
the window is empty; the caller owns that item's size.

Both directions use codecs, so a malformed value fails at the boundary
instead of reaching the implementation.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

from app.deepspace.typert.codecs import Codec, JsonCodec
from app.deepspace.typert.errors import RemoteError

Out = TypeVar("Out")
In = TypeVar("In")

_END = object()


class RemoteStream(Generic[Out, In]):
    """One open capability stream.

    Attributes:
        window_bytes: The uplink byte window.
    """

    def __init__(
        self,
        *,
        out_codec: Codec[Out] | None = None,
        in_codec: Codec[In] | None = None,
        window_bytes: int = 64 * 1024,
    ) -> None:
        if window_bytes < 1:
            raise ValueError("A stream window must be positive.")
        self.window_bytes = window_bytes
        self._out_codec = out_codec or JsonCodec()
        self._in_codec = in_codec or JsonCodec()
        self._downlink: asyncio.Queue[Any] = asyncio.Queue()
        self._uplink: asyncio.Queue[Any] = asyncio.Queue()
        self._condition = asyncio.Condition()
        self._pending_uplink_bytes = 0
        self._closed = False
        self._downlink_finished = False
        self._handle: RemoteStreamHandle[In] = RemoteStreamHandle(self)
        self._close_callbacks: list[Any] = []

    @property
    def closed(self) -> bool:
        """Whether the stream is closed."""
        return self._closed

    @property
    def pending_uplink_bytes(self) -> int:
        """The unread uplink bytes currently inside the window."""
        return self._pending_uplink_bytes

    def handle(self) -> RemoteStreamHandle[In]:
        """Return the caller's uplink handle."""
        return self._handle

    def add_close_callback(self, callback: Any) -> None:
        """Register one resource-release callback for stream disposal."""
        if self._closed or self._downlink_finished:
            raise RemoteError("stream/closed", "The stream is closed.")
        if not callable(callback):
            raise TypeError("A stream close callback must be callable.")
        self._close_callbacks.append(callback)

    def publish(self, value: Out) -> None:
        """Publish one downlink value.

        Raises:
            RemoteError: With ``stream/closed`` after close.
        """
        if self._closed:
            raise RemoteError("stream/closed", "The stream is closed.")
        try:
            encoded = self._out_codec.encode(value)
        except RemoteError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize codec failures
            raise RemoteError("gateway/internal", str(exc) or "The downlink codec failed.") from exc
        self._downlink.put_nowait(encoded)

    def publish_many(self, values: list[Out]) -> None:
        """Publish several downlink values in order."""
        for value in values:
            self.publish(value)

    def finish(self) -> None:
        """End the downlink after every queued value is read."""
        if self._closed or self._downlink_finished:
            return
        self._downlink_finished = True
        self._downlink.put_nowait(_END)

    async def receive(self) -> Out:
        """Read the next downlink value.

        Raises:
            RemoteError: With ``stream/closed`` after the downlink ends.
        """
        value = await self._downlink.get()
        if value is _END:
            self._downlink_finished = True
            raise RemoteError("stream/closed", "The downlink has ended.")
        return self._out_codec.decode(value)

    async def __aiter__(self) -> AsyncIterator[Out]:
        while True:
            try:
                yield await self.receive()
            except RemoteError as exc:
                if exc.code == "stream/closed":
                    return
                raise

    async def receive_uplink(self) -> In | None:
        """Read one uplink value, or ``None`` after the caller ends it."""
        value = await self._uplink.get()
        if value is _END:
            return None
        assert isinstance(value, tuple)
        encoded, size = value
        async with self._condition:
            self._pending_uplink_bytes = max(0, self._pending_uplink_bytes - size)
            self._condition.notify_all()
        return self._in_codec.decode(encoded)

    async def close(self) -> None:
        """Close both directions and release every waiter."""
        if self._closed:
            return
        self._closed = True
        self._downlink.put_nowait(_END)
        self._uplink.put_nowait(_END)
        async with self._condition:
            self._pending_uplink_bytes = 0
            self._condition.notify_all()
        callbacks, self._close_callbacks = self._close_callbacks, []
        failure: BaseException | None = None
        for callback in reversed(callbacks):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # release every owned resource
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    async def _send_uplink(self, value: In) -> None:
        if self._closed:
            raise RemoteError("stream/closed", "The stream is closed.")
        try:
            encoded = self._in_codec.encode(value)
        except RemoteError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize codec failures
            raise RemoteError("gateway/internal", str(exc) or "The uplink codec failed.") from exc
        size = _encoded_size(encoded)
        async with self._condition:
            while (
                self._pending_uplink_bytes > 0
                and self._pending_uplink_bytes + size > self.window_bytes
            ):
                await self._condition.wait()
                if self._closed:
                    raise RemoteError("stream/closed", "The stream is closed.")
            self._pending_uplink_bytes += size
        self._uplink.put_nowait((encoded, size))


class RemoteStreamHandle(Generic[In]):
    """The caller's uplink handle for one stream."""

    def __init__(self, stream: RemoteStream[Any, Any]) -> None:
        self._stream = stream
        self._ended = False

    @property
    def ended(self) -> bool:
        """Whether this handle already ended its uplink."""
        return self._ended

    async def send(self, value: In) -> None:
        """Send one uplink value, waiting for window capacity.

        Raises:
            RemoteError: With ``stream/closed`` after the stream or the
                handle is closed.
        """
        if self._ended:
            raise RemoteError("stream/closed", "The uplink handle has ended.")
        await self._stream._send_uplink(value)

    async def end(self) -> None:
        """End the uplink after every sent value is read."""
        if self._ended:
            return
        self._ended = True
        self._stream._uplink.put_nowait(_END)

    async def dispose(self) -> None:
        """End the uplink and close the whole stream."""
        await self.end()
        await self._stream.close()


def _encoded_size(value: Any) -> int:
    """Return the utf-8 byte size of an encoded JSON value."""
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


__all__ = ["RemoteStream", "RemoteStreamHandle"]
