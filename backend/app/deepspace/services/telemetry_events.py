"""OpenTelemetry event-log and session-log reporters for DeepSpace.

Python-native port of the AverQel KineSis telemetry/otel package:
- EventLogReporter: fire-and-forget emit(record) + count-based batching
- SessionLogReporter: byte-bounded (4MB), count+byte queue, single in-flight
- Independent channels: each reporter owns its transport/SDK state
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SESSION_LOG_MAX_REQUEST_BYTES = 4_000_000


@dataclass
class OTelEventRecord:
    """Ordinary event record."""

    event_name: str
    body: str
    timestamp: int  # Unix ms
    severity_number: int = 9  # INFO
    attributes: dict[str, Any] | None = None


@dataclass
class SessionLogRecord:
    """Session log record."""

    session_id: str
    event: dict[str, Any]
    attributes: dict[str, Any] | None = None
    severity_number: int = 9


@dataclass
class LogExporterOptions:
    url: str
    headers: dict[str, str] | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlsplit(str(self.url or ""))
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Telemetry exporter URL must be an HTTP(S) URL with an authority.")


class EventLogReporter:
    """Fire-and-forget ordinary-event reporter with count-based batching."""

    def __init__(
        self,
        exporter: LogExporterOptions,
        resource_attributes: dict[str, Any],
        scope: dict[str, str],
        processor_options: dict[str, Any] | None = None,
        on_failure: Callable[[str, Exception | None], None] | None = None,
    ) -> None:
        self._exporter = exporter
        self._resource = resource_attributes
        self._scope = scope
        options = processor_options or {}
        self._processor = options
        self._on_failure = on_failure or (lambda msg, err: logger.warning(msg))
        self._cancelled = False
        self._closing = False
        self._queue: list[dict[str, Any]] = []
        self._batch_size = options.get("max_export_batch_size", 512)
        self._max_queue = options.get("max_queue_size", 2048)
        delay_ms = options.get("scheduled_delay_millis", 1000)
        for key, value in (
            ("max_queue_size", self._max_queue),
            ("max_export_batch_size", self._batch_size),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"event log processor.{key} must be a positive integer")
        if self._batch_size > self._max_queue:
            raise ValueError("event log maxExportBatchSize must not exceed maxQueueSize")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
            raise ValueError(
                "event log processor.scheduled_delay_millis must be a non-negative integer"
            )
        self._scheduled_delay = delay_ms / 1000.0
        self._timer: asyncio.TimerHandle | None = None
        self._flushing = False
        self._flush_done = asyncio.Event()
        self._flush_done.set()
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the application loop so synchronous ORM hooks can schedule exports."""
        if loop.is_closed():
            raise RuntimeError("Cannot bind a closed telemetry event loop.")
        self._loop = loop

    def emit(self, record: OTelEventRecord) -> None:
        entry = {
            "eventName": record.event_name,
            "body": record.body,
            "timestamp": record.timestamp,
            "observedTimestamp": int(time.time() * 1000),
            "severityNumber": record.severity_number,
            "attributes": record.attributes or {},
        }
        with self._lock:
            if self._cancelled or self._closing:
                return
            if len(self._queue) >= self._max_queue:
                rejected = True
            else:
                rejected = False
                self._queue.append(entry)
                should_flush = len(self._queue) >= self._batch_size
        if rejected:
            self._on_failure("Event log queue is full; record rejected", None)
            return
        self._schedule_flush(immediate=should_flush)

    def _schedule_flush(self, *, immediate: bool) -> None:
        """Schedule delivery only when an event loop is available.

        Reporters are also called by synchronous startup and logging paths.
        In that case records remain bounded in the queue until a later emit
        from an async consumer or an explicit ``shutdown`` flushes them.
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        loop = self._loop or running_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        self._loop = loop

        def schedule_on_loop() -> None:
            if self._cancelled or loop.is_closed():
                return
            if self._timer is not None:
                if not immediate:
                    return
                self._timer.cancel()
                self._timer = None
            if immediate:
                asyncio.create_task(self._flush())
            else:
                self._timer = loop.call_later(
                    self._scheduled_delay,
                    lambda: asyncio.create_task(self._flush()),
                )

        try:
            if running_loop is loop:
                schedule_on_loop()
            else:
                loop.call_soon_threadsafe(schedule_on_loop)
        except RuntimeError:
            # The owning loop may have stopped between the check and dispatch.
            # The queued records remain available for the next flush.
            return

    async def _flush(self) -> None:
        if self._flushing:
            await self._flush_done.wait()
            if self._queue:
                await self._flush()
            return
        self._flushing = True
        self._flush_done.clear()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    batch = self._queue[: self._batch_size]
                    del self._queue[: len(batch)]
                try:
                    await self._export(batch)
                except Exception as exc:
                    self._on_failure("Product telemetry export failed", exc)
        finally:
            self._flushing = False
            self._flush_done.set()

    async def _export(self, records: list[dict[str, Any]]) -> None:
        if self._cancelled:
            return
        payload = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json"}
        if self._exporter.headers:
            headers.update(self._exporter.headers)
        req = Request(self._exporter.url, data=payload, headers=headers, method="POST")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: urlopen(
                    req, timeout=self._exporter.timeout_seconds
                ),  # nosec B310 - exporter URL is validated as HTTP(S) above.
            )
        except URLError as exc:
            raise RuntimeError(f"OTLP export failed: {exc}") from exc

    async def shutdown(self, signal: Any = None) -> None:
        with self._lock:
            self._closing = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        await self._flush()
        self._cancelled = True
        self._shutdown_event.set()


class SessionLogProcessor:
    """Byte/count batching with one transport request in flight."""

    def __init__(
        self,
        exporter: LogExporterOptions,
        limit: int,
        config: dict[str, Any],
        on_failure: Callable[[str, Exception | None], None],
    ) -> None:
        self._exporter = exporter
        self._limit = limit
        self._config = {
            "max_queue_size": config.get("max_queue_size", 2048),
            "max_export_batch_size": config.get("max_export_batch_size", 512),
            "scheduled_delay_millis": config.get("scheduled_delay_millis", 1000),
            "export_timeout_millis": config.get("export_timeout_millis", 30000),
        }
        self._on_failure = on_failure
        self._queue: list[tuple[dict[str, Any], int]] = []
        self._bytes = 0
        self._timer: asyncio.TimerHandle | None = None
        self._active: asyncio.Task[None] | None = None
        self._stopped = False
        self._shutdown_promise: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._closing = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the application loop so synchronous ORM hooks can schedule exports."""
        if loop.is_closed():
            raise RuntimeError("Cannot bind a closed telemetry event loop.")
        self._loop = loop

    def on_emit(self, record: dict[str, Any]) -> None:
        try:
            serialized = json.dumps(record).encode("utf-8")
            bytes_len = len(serialized)
        except (TypeError, ValueError) as exc:
            self._on_failure("Session log serialization failed; record rejected", exc)
            return
        if bytes_len > self._limit:
            self._on_failure(
                "Session log record rejected; content was not truncated",
                ValueError(
                    f"Session log record exceeds maxRequestBytes: {bytes_len} > {self._limit}"
                ),
            )
            return
        with self._lock:
            if self._stopped or self._closing:
                return
            if len(self._queue) >= self._config["max_queue_size"]:
                rejected = True
            else:
                rejected = False
                self._queue.append((record, bytes_len))
                self._bytes += bytes_len
                should_flush = (
                    len(self._queue) >= self._config["max_export_batch_size"]
                    or self._bytes >= self._limit
                )
        if rejected:
            self._on_failure("Session log queue is full; record rejected", None)
            return
        self._schedule_flush(immediate=should_flush)

    def _schedule_flush(self, *, immediate: bool) -> None:
        """Schedule a flush safely from sync or async producer contexts."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        loop = self._loop or running_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        self._loop = loop

        def schedule_on_loop() -> None:
            if self._stopped or loop.is_closed():
                return
            if self._active is not None:
                return
            if self._timer is not None:
                if not immediate:
                    return
                self._timer.cancel()
                self._timer = None
            if immediate:
                asyncio.create_task(self.force_flush())
            else:
                delay = self._config["scheduled_delay_millis"] / 1000.0
                self._timer = loop.call_later(
                    delay,
                    lambda: asyncio.create_task(self.force_flush()),
                )

        try:
            if running_loop is loop:
                schedule_on_loop()
            else:
                loop.call_soon_threadsafe(schedule_on_loop)
        except RuntimeError:
            return

    async def force_flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._active is not None:
            await self._active
            return
        if not self._queue:
            return
        self._active = asyncio.ensure_future(self._drain())
        await self._active

    async def _drain(self) -> None:
        try:
            while not self._stopped and self._queue:
                with self._lock:
                    batch_bytes = 0
                    count = 0
                    for entry in self._queue:
                        if count >= self._config["max_export_batch_size"]:
                            break
                        if batch_bytes + entry[1] > self._limit:
                            break
                        batch_bytes += entry[1]
                        count += 1
                    records = [e[0] for e in self._queue[:count]]
                    del self._queue[:count]
                    self._bytes -= batch_bytes
                if not records:
                    break
                await self._send(records)
        finally:
            self._active = None

    async def _send(self, records: list[dict[str, Any]]) -> None:
        payload = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json"}
        if self._exporter.headers:
            headers.update(self._exporter.headers)
        req_url = self._exporter.url
        timeout = self._exporter.timeout_seconds
        try:
            loop = asyncio.get_event_loop()
            req = __import__("urllib.request", fromlist=["Request"]).Request(
                req_url, data=payload, headers=headers, method="POST"
            )
            await loop.run_in_executor(
                None,
                lambda: __import__("urllib.request", fromlist=["urlopen"]).urlopen(
                    req, timeout=timeout
                ),
            )
        except Exception as exc:
            self._on_failure(
                "Session log export failed",
                exc if isinstance(exc, Exception) else Exception(str(exc)),
            )

    def stop_pending(self) -> None:
        with self._lock:
            self._closing = True
            self._stopped = True
            self._queue.clear()
            self._bytes = 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def shutdown(self) -> None:
        if self._shutdown_promise is not None:
            await self._shutdown_promise
            return
        with self._lock:
            self._closing = True
        self._shutdown_promise = asyncio.ensure_future(self.force_flush())
        try:
            await self._shutdown_promise
        finally:
            with self._lock:
                self._stopped = True


class SessionLogReporter:
    """Byte-bounded session-log reporter."""

    def __init__(
        self,
        exporter: LogExporterOptions,
        resource_attributes: dict[str, Any],
        scope: dict[str, str],
        processor_options: dict[str, Any] | None = None,
        max_request_bytes: int | None = None,
        on_failure: Callable[[str, Exception | None], None] | None = None,
    ) -> None:
        self._on_failure = on_failure or (lambda msg, err: logger.warning(msg))
        limit = (
            max_request_bytes if max_request_bytes is not None else SESSION_LOG_MAX_REQUEST_BYTES
        )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > SESSION_LOG_MAX_REQUEST_BYTES
        ):
            raise ValueError(
                f"session log maxRequestBytes must be an integer between 1 and {SESSION_LOG_MAX_REQUEST_BYTES}"
            )
        opts = processor_options or {}
        for key in (
            "max_queue_size",
            "max_export_batch_size",
            "scheduled_delay_millis",
            "export_timeout_millis",
        ):
            val = opts.get(key)
            if val is not None and (
                isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > 2_147_483_647
            ):
                raise ValueError(f"session log processor.{key} must be a positive integer")
        queue = opts.get("max_queue_size", 2048)
        batch = opts.get("max_export_batch_size", 512)
        if batch > queue:
            raise ValueError("session log maxExportBatchSize must not exceed maxQueueSize")

        config = {
            "max_queue_size": queue,
            "max_export_batch_size": batch,
            "scheduled_delay_millis": opts.get("scheduled_delay_millis", 1000),
            "export_timeout_millis": opts.get("export_timeout_millis", 30000),
        }
        self._processor = SessionLogProcessor(exporter, limit, config, self._on_failure)
        self._resource = resource_attributes
        self._scope = scope

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the application loop used by the bounded session-log queue."""
        self._processor.bind_loop(loop)

    def report_session_log(self, record: SessionLogRecord) -> None:
        severity = record.severity_number
        entry = {
            "eventName": "session-log",
            "body": "session-log",
            "timestamp": record.event.get("time", int(time.time() * 1000)),
            "observedTimestamp": record.event.get("time", int(time.time() * 1000)),
            "severityNumber": severity,
            "attributes": {
                **(record.attributes or {}),
                "sessionId": record.session_id,
                "content": json.dumps(record.event),
            },
        }
        self._processor.on_emit(entry)

    def stop_pending(self) -> None:
        self._processor.stop_pending()

    async def shutdown(self) -> None:
        await self._processor.shutdown()


class TelemetryChannelFactory:
    """Factory for independent telemetry channels (OTel-equivalent)."""

    def __init__(self) -> None:
        self._channels: list[Any] = []

    def create_event_reporter(self, options: dict[str, Any]) -> EventLogReporter:
        exporter = LogExporterOptions(
            url=options["exporter"]["url"],
            headers=options["exporter"].get("headers"),
            timeout_seconds=options["exporter"].get("timeout_seconds", 30.0),
        )
        reporter = EventLogReporter(
            exporter=exporter,
            resource_attributes=options.get("resource_attributes", {}),
            scope=options.get("scope", {"name": "deepspace"}),
            processor_options=options.get("processor"),
            on_failure=options.get("on_failure"),
        )
        self._channels.append(reporter)
        return reporter

    def create_session_log_reporter(self, options: dict[str, Any]) -> SessionLogReporter:
        exporter = LogExporterOptions(
            url=options["exporter"]["url"],
            headers=options["exporter"].get("headers"),
            timeout_seconds=options["exporter"].get("timeout_seconds", 30.0),
        )
        reporter = SessionLogReporter(
            exporter=exporter,
            resource_attributes=options.get("resource_attributes", {}),
            scope=options.get("scope", {"name": "deepspace"}),
            processor_options=options.get("processor"),
            max_request_bytes=options.get("max_request_bytes"),
            on_failure=options.get("on_failure"),
        )
        self._channels.append(reporter)
        return reporter
