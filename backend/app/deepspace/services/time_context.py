"""Per-step time context for DeepSpace.

Ported from NeoSIS @averqel/neosis-time-context.
Provides current time and elapsed time readings injected into request history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class TimeContextProjection:
    """Persisted time-context projection state."""

    last_message_time: float | None = None
    last_injection_time: float | None = None
    last_turn_injection_time: float | None = None


@dataclass(frozen=True, slots=True)
class BrowserTimeZoneContext:
    """Browser-provided time zone context."""

    time_zone: str
    offset_minutes: int
    is_dst: bool


def format_timestamp(ts: float, time_zone: str) -> str:
    """Format a Unix timestamp in the given time zone."""
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(elapsed_ms: float) -> str:
    """Format elapsed milliseconds as compact human-readable string."""
    seconds = int(max(0, elapsed_ms) / 1000)
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    seconds %= 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def render_browser_time_zone(browser_context: BrowserTimeZoneContext | None) -> str:
    """Render browser time zone context for prompt."""
    if browser_context is None:
        return "Browser time zone: unavailable"
    return (
        f"Browser time zone: {browser_context.time_zone} "
        f"(UTC{browser_context.offset_minutes // 60:+03d}:{abs(browser_context.offset_minutes) % 60:02d})"
    )


def format_injection(
    now: float,
    turn: int,
    step: int,
    previous_event_time: float | None,
    time_zone: str,
    browser_context: BrowserTimeZoneContext | None,
) -> str:
    """Format the time-context injection text for system prompt."""
    elapsed = (
        "unavailable"
        if previous_event_time is None
        else format_duration((now - previous_event_time) * 1000)
    )
    baseline = "model-visible message" if step == 1 else "step context"
    browser_text = render_browser_time_zone(browser_context)
    timestamp = format_timestamp(now, time_zone)
    return (
        f"Time sampled while preparing turn {turn}, step {step}: {timestamp}\n"
        f"{browser_text}\n"
        f"Elapsed since the preceding {baseline}: {elapsed}."
    )


class TimeContextService:
    """Service for managing time-context projections and injections.

    Uses an in-memory cache with conversation-scoped keys.
    For persistence across restarts, the projection state could be stored
    in the conversation context summary table, but for now we keep it simple.
    """

    def __init__(self):
        self._projections: dict[UUID, TimeContextProjection] = {}

    def get_projection(self, conversation_id: UUID) -> TimeContextProjection:
        """Get the time-context projection for a conversation."""
        return self._projections.get(conversation_id, TimeContextProjection())

    def update_projection(
        self,
        conversation_id: UUID,
        *,
        last_message_time: float | None = None,
        last_injection_time: float | None = None,
        last_turn_injection_time: float | None = None,
    ) -> None:
        """Update the time-context projection."""
        current = self.get_projection(conversation_id)
        updated = TimeContextProjection(
            last_message_time=(
                last_message_time if last_message_time is not None else current.last_message_time
            ),
            last_injection_time=(
                last_injection_time
                if last_injection_time is not None
                else current.last_injection_time
            ),
            last_turn_injection_time=(
                last_turn_injection_time
                if last_turn_injection_time is not None
                else current.last_turn_injection_time
            ),
        )
        self._projections[conversation_id] = updated

    def should_inject(
        self,
        conversation_id: UUID,
        turn: int,
        step: int,
        refresh_interval_ms: int,
    ) -> bool:
        """Check if time context should be injected this step."""
        if refresh_interval_ms <= 0:
            return True  # Inject at every eligible step

        projection = self.get_projection(conversation_id)
        if projection.last_turn_injection_time is None:
            return True  # First injection in this turn

        now = time.time()
        elapsed_ms = (now - projection.last_turn_injection_time) * 1000
        return elapsed_ms >= refresh_interval_ms

    def prepare_injection(
        self,
        conversation_id: UUID,
        turn: int,
        step: int,
        previous_event_time: float | None,
        settings: Settings,
        browser_context: BrowserTimeZoneContext | None = None,
    ) -> str | None:
        """Prepare time-context injection text if injection is due."""
        refresh_interval_ms = getattr(settings, "deepspace_time_context_refresh_interval_ms", 0)
        time_zone = getattr(settings, "deepspace_time_context_time_zone", "UTC")

        if not getattr(settings, "deepspace_time_context_enabled", True):
            return None

        if not self.should_inject(conversation_id, turn, step, refresh_interval_ms):
            return None

        now = time.time()
        injection_text = format_injection(
            now, turn, step, previous_event_time, time_zone, browser_context
        )

        # Update projection
        self.update_projection(
            conversation_id,
            last_injection_time=now,
            last_turn_injection_time=now,
        )

        return injection_text

    def record_message_time(self, conversation_id: UUID, timestamp: float) -> None:
        """Record the time of a model-visible message."""
        projection = self.get_projection(conversation_id)
        self.update_projection(conversation_id, last_message_time=timestamp)


def parse_browser_time_zone(data: dict[str, Any]) -> BrowserTimeZoneContext | None:
    """Parse browser time zone context from client data."""
    if not data:
        return None
    try:
        return BrowserTimeZoneContext(
            time_zone=str(data.get("timeZone", "UTC")),
            offset_minutes=int(data.get("offsetMinutes", 0)),
            is_dst=bool(data.get("isDST", False)),
        )
    except (ValueError, TypeError):
        return None
