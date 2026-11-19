"""Product Telemetry service for DeepSpace.

Provides explicit product usage events via OTLP/HTTP export.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Configuration for product telemetry."""

    endpoint: str = "https://dsh-otel-collector.deepseeksvc.com/v1/logs"
    service_name: str = "deepspace"
    service_version: str = "1.0.0"
    channel: str = "dsh_otel_report"
    compression: str = "gzip"  # gzip or none
    max_export_batch_size: int = 512
    max_queue_size: int = 2048
    scheduled_delay_millis: int = 30000
    timeout_millis: int = 15000
    export_timeout_millis: int = 20000
    shutdown_timeout_millis: int = 21000


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """A telemetry event to be exported."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    timestamp_millis: int = field(default_factory=lambda: int(time.time() * 1000))
    severity: str = "INFO"


class ProductTelemetryService:
    """
    Product telemetry service for explicit event submission via OTLP/HTTP.
    """

    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        self._queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=config.max_queue_size)
        self._exporter_task: asyncio.Task[None] | None = None
        self._running = False
        self._session: Any = None  # aiohttp session

    async def start(self) -> None:
        """Start the telemetry service."""
        self._running = True
        self._exporter_task = asyncio.create_task(self._exporter_loop())
        logger.info("Product telemetry service started")

    async def stop(self) -> None:
        """Stop the telemetry service with graceful shutdown."""
        self._running = False
        if self._exporter_task:
            self._exporter_task.cancel()
            try:
                await asyncio.wait_for(
                    self._exporter_task, timeout=self.config.shutdown_timeout_millis / 1000
                )
            except TimeoutError:
                logger.warning("Telemetry exporter did not shut down gracefully")

    def emit(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        timestamp_millis: int | None = None,
        severity: str = "INFO",
    ) -> None:
        """Emit a telemetry event."""
        event = TelemetryEvent(
            name=name,
            attributes=attributes or {},
            timestamp_millis=timestamp_millis or int(time.time() * 1000),
            severity=severity,
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Telemetry queue full, dropping event")

    async def _exporter_loop(self) -> None:
        """Main exporter loop that batches and exports events."""
        batch: list[TelemetryEvent] = []
        last_export = time.monotonic()

        while self._running:
            try:
                # Wait for events or timeout
                timeout = self.config.scheduled_delay_millis / 1000
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(event)
                except TimeoutError:
                    pass

                # Check if we should export
                now = time.monotonic()
                should_export = len(batch) >= self.config.max_export_batch_size or (
                    batch and (now - last_export) >= self.config.scheduled_delay_millis / 1000
                )

                if should_export and batch:
                    await self._export_batch(batch)
                    batch.clear()
                    last_export = now

            except asyncio.CancelledError:
                # Export remaining on shutdown
                if batch:
                    await self._export_batch(batch)
                break
            except Exception as e:
                logger.error(f"Telemetry exporter error: {e}")

    async def _export_batch(self, batch: list[TelemetryEvent]) -> None:
        """Export a batch of events via OTLP/HTTP."""
        if not batch:
            return

        # Build OTLP payload - build log_records first
        log_records = []
        for event in batch:
            attributes = [
                {"key": k, "value": {"stringValue": str(v)}} for k, v in event.attributes.items()
            ]
            log_records.append(
                {
                    "timeUnixNano": str(event.timestamp_millis * 1_000_000),
                    "severityText": event.severity,
                    "body": {"stringValue": event.name},
                    "attributes": attributes,
                }
            )

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": self.config.service_name},
                            },
                            {
                                "key": "service.version",
                                "value": {"stringValue": self.config.service_version},
                            },
                        ]
                    },
                    "scopeLogs": [{"logRecords": log_records}],
                }
            ]
        }

        headers = {
            "Content-Type": "application/json",
            "x-channel": self.config.channel,
        }

        if self.config.compression == "gzip":
            import gzip
            import json

            body = gzip.compress(json.dumps(payload).encode())
            headers["Content-Encoding"] = "gzip"
        else:
            import json

            body = json.dumps(payload).encode()

        try:
            import aiohttp

            if self._session is None:
                self._session = aiohttp.ClientSession()

            async with self._session.post(
                self.config.endpoint,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_millis / 1000),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.warning(f"Telemetry export failed: {resp.status} {text}")
        except Exception as e:
            logger.warning(f"Telemetry export failed: {e}")


def create_product_telemetry_service(config: Any | None = None) -> ProductTelemetryService:
    """Create the product telemetry service."""
    if config is None:
        config = TelemetryConfig(
            service_version=os.environ.get("DEEPSPACE_VERSION", "1.0.0"),
        )
    return ProductTelemetryService(config)


__all__ = [
    "ProductTelemetryService",
    "TelemetryConfig",
    "TelemetryEvent",
    "create_product_telemetry_service",
]
