"""Session telemetry and OpenTelemetry integration for DeepSpace.

Provides OTel-backed telemetry for sessions, turns, tools, and provider calls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.config import Settings, get_settings
from app.deepspace.services.telemetry_events import (
    OTelEventRecord,
    SessionLogRecord,
    TelemetryChannelFactory,
)


def _load_otel_exports() -> tuple[bool, Any]:
    """Load optional OpenTelemetry symbols without importing them at module import time."""
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        from opentelemetry.trace import Span, SpanKind, Status, StatusCode
    except ImportError:
        return False, None
    return True, {
        "trace": trace,
        "OTLPSpanExporter": OTLPSpanExporter,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "ConsoleSpanExporter": ConsoleSpanExporter,
        "TraceIdRatioBased": TraceIdRatioBased,
        "Span": Span,
        "SpanKind": SpanKind,
        "Status": Status,
        "StatusCode": StatusCode,
    }


OTEL_AVAILABLE, _OTEL_EXPORTS = _load_otel_exports()
trace: Any = _OTEL_EXPORTS.get("trace") if _OTEL_EXPORTS else None
OTLPSpanExporter: Any = _OTEL_EXPORTS.get("OTLPSpanExporter") if _OTEL_EXPORTS else None
Resource: Any = _OTEL_EXPORTS.get("Resource") if _OTEL_EXPORTS else None
TracerProvider: Any = _OTEL_EXPORTS.get("TracerProvider") if _OTEL_EXPORTS else None
BatchSpanProcessor: Any = _OTEL_EXPORTS.get("BatchSpanProcessor") if _OTEL_EXPORTS else None
ConsoleSpanExporter: Any = _OTEL_EXPORTS.get("ConsoleSpanExporter") if _OTEL_EXPORTS else None
TraceIdRatioBased: Any = _OTEL_EXPORTS.get("TraceIdRatioBased") if _OTEL_EXPORTS else None
Span: Any = _OTEL_EXPORTS.get("Span") if _OTEL_EXPORTS else None
SpanKind: Any = _OTEL_EXPORTS.get("SpanKind") if _OTEL_EXPORTS else None
Status: Any = _OTEL_EXPORTS.get("Status") if _OTEL_EXPORTS else None
StatusCode: Any = _OTEL_EXPORTS.get("StatusCode") if _OTEL_EXPORTS else None

logger = logging.getLogger(__name__)


class SpanHandle:
    """One opened span context that the turn owns until it closes it.

    Telemetry span factories are context managers. A turn spans several
    generator phases and can return from many error paths, so the turn keeps
    the opened context here and closes every handle once in a single place.
    Closing twice is a no-op, which lets a phase close its own span early and
    still leave it registered for the turn-level cleanup.
    """

    __slots__ = ("_context", "span")

    def __init__(self, context: Any) -> None:
        self._context = context
        self.span = context.__enter__()

    @property
    def closed(self) -> bool:
        """Whether the underlying span context has already exited."""
        return self._context is None

    def close(self) -> None:
        """Exit the span context once; later calls do nothing."""
        context = self._context
        if context is not None:
            self._context = None
            context.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionTelemetryConfig:
    """Session telemetry configuration."""

    enabled: bool = False
    otel_endpoint: str | None = None
    otel_headers: dict[str, str] = field(default_factory=dict)
    logs_endpoint: str | None = None
    logs_headers: dict[str, str] = field(default_factory=dict)
    service_name: str = "deepspace"
    environment: str = "production"
    sample_rate: float = 1.0
    export_console: bool = False
    attributes: dict[str, str] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Telemetry Provider
# ──────────────────────────────────────────────────────────────────────


class SessionTelemetry:
    """Session telemetry provider using OpenTelemetry."""

    def __init__(self, config: SessionTelemetryConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self._tracer_provider: Any | None = None
        self._tracer: Any | None = None
        self._initialized = False
        self._channel_factory = TelemetryChannelFactory()
        self._event_reporter: Any | None = None
        self._session_log_reporter: Any | None = None

    def initialize(self) -> bool:
        """Initialize the telemetry provider."""
        if not self.config.enabled:
            return False
        if self.config.logs_endpoint and self._event_reporter is None:
            try:
                common = {
                    "exporter": {
                        "url": self.config.logs_endpoint,
                        "headers": dict(self.config.logs_headers),
                    },
                    "resource_attributes": {
                        "service.name": self.config.service_name,
                        "deployment.environment": self.config.environment,
                        **self.config.attributes,
                    },
                    "scope": {"name": "deepspace.session-events"},
                }
                self._event_reporter = self.create_event_reporter(common)
                self._session_log_reporter = self.create_session_log_reporter(common)
            except Exception:  # noqa: BLE001 - telemetry setup must not stop the API
                logger.warning("Session event-log reporters did not initialize", exc_info=True)
                self._event_reporter = None
                self._session_log_reporter = None
        if not OTEL_AVAILABLE:
            logger.warning(
                "OpenTelemetry tracing is unavailable; configured session event logs remain active"
            )
            return False
        if self._initialized:
            return True

        try:
            resource = Resource.create(
                {
                    "service.name": self.config.service_name,
                    "deployment.environment": self.config.environment,
                    **self.config.attributes,
                }
            )

            # Sampling belongs to the provider, not the call sites.  This
            # keeps session, turn, provider, and tool spans on one decision
            # and makes the configured rate effective for every runtime path.
            provider = TracerProvider(
                resource=resource,
                sampler=TraceIdRatioBased(self.config.sample_rate),
            )

            if self.config.otel_endpoint:
                exporter = OTLPSpanExporter(
                    endpoint=self.config.otel_endpoint,
                    headers=self.config.otel_headers,
                )
                provider.add_span_processor(BatchSpanProcessor(exporter))
            elif self.config.export_console:
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

            trace.set_tracer_provider(provider)
            self._tracer_provider = provider
            self._tracer = trace.get_tracer(__name__)
            self._initialized = True
            logger.info("Session telemetry initialized")
            return True
        except Exception:
            logger.error("Failed to initialize session telemetry", exc_info=True)
            return False

    def create_event_reporter(self, options: dict[str, Any]) -> Any:
        """Create an independent ordinary-event channel (per-consumer)."""
        return self._channel_factory.create_event_reporter(options)

    def create_session_log_reporter(self, options: dict[str, Any]) -> Any:
        """Create an independent byte-bounded Session-log channel."""
        return self._channel_factory.create_session_log_reporter(options)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Give synchronous persistence hooks a loop for bounded async exports."""
        for reporter in (self._event_reporter, self._session_log_reporter):
            if reporter is not None:
                reporter.bind_loop(loop)

    def record_session_event(self, *, conversation_id: str, event: dict[str, Any]) -> None:
        """Send a content-free committed session-event summary to configured log channels."""
        timestamp = int(event.get("time") or 0)
        event_type = str(event.get("type") or "unknown")
        attributes = {
            "deepspace.event.type": event_type,
            "deepspace.event.sequence": event.get("sequence"),
            "deepspace.turn": event.get("turn"),
            "deepspace.step": event.get("step"),
        }
        safe_attributes = {key: value for key, value in attributes.items() if value is not None}
        if self._event_reporter is not None:
            self._event_reporter.emit(
                OTelEventRecord(
                    event_name=event_type,
                    body=event_type,
                    timestamp=timestamp,
                    attributes=safe_attributes,
                )
            )
        if self._session_log_reporter is not None:
            summary = {
                key: value
                for key, value in event.items()
                if key in {"type", "time", "sequence", "turn", "step", "tool"}
            }
            self._session_log_reporter.report_session_log(
                SessionLogRecord(
                    session_id=conversation_id,
                    event=summary,
                    attributes={"deepspace.event.type": event_type},
                )
            )

    async def shutdown_channels(self) -> None:
        """Flush and stop independent event channels before process shutdown."""
        reporters = [
            reporter
            for reporter in (self._event_reporter, self._session_log_reporter)
            if reporter is not None
        ]
        results = await asyncio.gather(
            *(reporter.shutdown() for reporter in reporters),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Telemetry channel shutdown failed: %s", result)
        self._event_reporter = None
        self._session_log_reporter = None

    def shutdown(self) -> None:
        """Shutdown the telemetry provider."""
        if self._tracer_provider:
            self._tracer_provider.shutdown()
            self._tracer_provider = None
            self._tracer = None
            self._initialized = False

    @contextmanager
    def session_span(self, conversation_id: UUID, turn: int | None = None) -> Iterator[Any]:
        """Create a session-level span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs: dict[str, Any] = {
            "deepspace.conversation_id": str(conversation_id),
        }
        if turn is not None:
            attrs["deepspace.turn"] = turn

        with self._tracer.start_as_current_span(
            "deepspace.session",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        ) as span:
            yield span

    @contextmanager
    def turn_span(self, conversation_id: UUID, turn: int, step: int | None = None) -> Iterator[Any]:
        """Create a turn-level span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs: dict[str, Any] = {
            "deepspace.conversation_id": str(conversation_id),
            "deepspace.turn": turn,
        }
        if step is not None:
            attrs["deepspace.step"] = step

        with self._tracer.start_as_current_span(
            "deepspace.turn",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        ) as span:
            yield span

    @contextmanager
    def tool_span(
        self, conversation_id: UUID, turn: int, step: int, tool_name: str
    ) -> Iterator[Any]:
        """Create a tool call span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs: dict[str, Any] = {
            "deepspace.conversation_id": str(conversation_id),
            "deepspace.turn": turn,
            "deepspace.step": step,
            "deepspace.tool.name": tool_name,
        }

        with self._tracer.start_as_current_span(
            f"deepspace.tool.{tool_name}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        ) as span:
            yield span

    @contextmanager
    def provider_span(
        self, conversation_id: UUID, provider: str, model: str, turn: int
    ) -> Iterator[Any]:
        """Create a provider call span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs: dict[str, Any] = {
            "deepspace.conversation_id": str(conversation_id),
            "deepspace.provider": provider,
            "deepspace.model": model,
            "deepspace.turn": turn,
        }

        with self._tracer.start_as_current_span(
            f"deepspace.provider.{provider}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        ) as span:
            yield span

    def record_tool_result(
        self,
        span: Any,
        success: bool,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        """Record tool result on span."""
        if not span or not self._initialized:
            return
        span.set_attribute("deepspace.tool.success", success)
        span.set_attribute("deepspace.tool.duration_ms", duration_ms)
        if error:
            span.set_attribute("deepspace.tool.error", error)
            span.set_status(Status(StatusCode.ERROR, error))
        else:
            span.set_status(Status(StatusCode.OK))

    def record_provider_result(
        self,
        span: Any,
        success: bool,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """Record provider result on span."""
        if not span or not self._initialized:
            return
        span.set_attribute("deepspace.provider.success", success)
        if input_tokens is not None:
            span.set_attribute("deepspace.provider.input_tokens", input_tokens)
        if output_tokens is not None:
            span.set_attribute("deepspace.provider.output_tokens", output_tokens)
        if latency_ms is not None:
            span.set_attribute("deepspace.provider.latency_ms", latency_ms)
        if error:
            span.set_attribute("deepspace.provider.error", error)
            span.set_status(Status(StatusCode.ERROR, error))
        else:
            span.set_status(Status(StatusCode.OK))

    def add_event(self, span: Any, name: str, attributes: dict[str, Any] | None = None) -> None:
        """Add an event to a span."""
        if not span or not self._initialized:
            return
        span.add_event(name, attributes or {})


# Global instance
_TELEMETRY: SessionTelemetry | None = None


def get_session_telemetry(settings: Settings | None = None) -> SessionTelemetry:
    """Get or create the global session telemetry instance."""
    global _TELEMETRY
    if _TELEMETRY is None:
        cfg = SessionTelemetryConfig(
            enabled=getattr(settings or get_settings(), "deepspace_telemetry_enabled", False),
            otel_endpoint=getattr(
                settings or get_settings(), "deepspace_telemetry_otel_endpoint", None
            ),
            otel_headers=getattr(
                settings or get_settings(), "deepspace_telemetry_otel_headers", {}
            ),
            logs_endpoint=getattr(
                settings or get_settings(), "deepspace_telemetry_logs_endpoint", None
            ),
            logs_headers=getattr(
                settings or get_settings(), "deepspace_telemetry_logs_headers", {}
            ),
            service_name=getattr(
                settings or get_settings(), "deepspace_telemetry_service_name", "deepspace"
            ),
            environment=getattr(
                settings or get_settings(), "deepspace_telemetry_environment", "production"
            ),
            sample_rate=getattr(settings or get_settings(), "deepspace_telemetry_sample_rate", 1.0),
            export_console=getattr(
                settings or get_settings(), "deepspace_telemetry_export_console", False
            ),
            attributes=getattr(settings or get_settings(), "deepspace_telemetry_attributes", {}),
        )
        _TELEMETRY = SessionTelemetry(cfg, settings or get_settings())
        if cfg.enabled:
            _TELEMETRY.initialize()
    return _TELEMETRY


def init_session_telemetry(settings: Settings) -> SessionTelemetry:
    """Initialize session telemetry with settings."""
    global _TELEMETRY
    cfg = SessionTelemetryConfig(
        enabled=getattr(settings, "deepspace_telemetry_enabled", False),
        otel_endpoint=getattr(settings, "deepspace_telemetry_otel_endpoint", None),
        otel_headers=getattr(settings, "deepspace_telemetry_otel_headers", {}),
        logs_endpoint=getattr(settings, "deepspace_telemetry_logs_endpoint", None),
        logs_headers=getattr(settings, "deepspace_telemetry_logs_headers", {}),
        service_name=getattr(settings, "deepspace_telemetry_service_name", "deepspace"),
        environment=getattr(settings, "deepspace_telemetry_environment", "production"),
        sample_rate=getattr(settings, "deepspace_telemetry_sample_rate", 1.0),
        export_console=getattr(settings, "deepspace_telemetry_export_console", False),
        attributes=getattr(settings, "deepspace_telemetry_attributes", {}),
    )
    _TELEMETRY = SessionTelemetry(cfg, settings)
    if cfg.enabled:
        _TELEMETRY.initialize()
    return _TELEMETRY


__all__ = [
    "SessionTelemetryConfig",
    "SessionTelemetry",
    "SpanHandle",
    "get_session_telemetry",
    "init_session_telemetry",
]
