"""Session telemetry and OpenTelemetry integration for DeepSpace.

Provides OTel-backed telemetry for sessions, turns, tools, and provider calls.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.config import Settings, get_settings

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    from opentelemetry.trace import Span, SpanKind, Status, StatusCode

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    trace = None
    Span = None
    SpanKind = None
    Status = None
    StatusCode = None
    TraceIdRatioBased = None

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

    def initialize(self) -> bool:
        """Initialize the telemetry provider."""
        if not OTEL_AVAILABLE:
            logger.warning("OpenTelemetry not available; session telemetry disabled")
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

    def shutdown(self) -> None:
        """Shutdown the telemetry provider."""
        if self._tracer_provider:
            self._tracer_provider.shutdown()
            self._tracer_provider = None
            self._tracer = None
            self._initialized = False

    @contextmanager
    def session_span(self, conversation_id: UUID, turn: int | None = None):
        """Create a session-level span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs = {
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
    def turn_span(self, conversation_id: UUID, turn: int, step: int | None = None):
        """Create a turn-level span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs = {
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
    def tool_span(self, conversation_id: UUID, turn: int, step: int, tool_name: str):
        """Create a tool call span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs = {
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
    def provider_span(self, conversation_id: UUID, provider: str, model: str, turn: int):
        """Create a provider call span."""
        if not self._initialized or not self._tracer:
            yield None
            return

        attrs = {
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
