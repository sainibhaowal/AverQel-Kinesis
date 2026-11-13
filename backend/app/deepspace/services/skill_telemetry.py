"""Content-free telemetry for skill discovery and loading.

The catalog tells the model what exists; telemetry tells the operator whether
it is used and whether loading works.  Records carry the skill name, provider,
source, action, outcome, and latency - never the instructions themselves.

The module keeps a small in-process counter set so a diagnostics endpoint or
test can read aggregate use without a configured exporter.  Configured
session telemetry additionally receives the event type.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillTelemetryEvent:
    """One skill action and its outcome."""

    action: str
    name: str
    provider: str = ""
    source: str = ""
    outcome: str = "ok"
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the event as a structured log or API payload."""
        return {
            "action": self.action,
            "name": self.name,
            "provider": self.provider,
            "source": self.source,
            "outcome": self.outcome,
            "duration_ms": round(self.duration_ms, 2),
        }


_COUNTERS: dict[str, int] = {}
_COUNTER_LOCK = threading.Lock()


def _increment(key: str) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + 1


def skill_counters() -> dict[str, int]:
    """Return a copy of the in-process action counters."""
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


def reset_skill_counters() -> None:
    """Clear the counters.  Used by tests and diagnostics."""
    with _COUNTER_LOCK:
        _COUNTERS.clear()


def record_skill_event(
    event: SkillTelemetryEvent,
    *,
    conversation_id: Any = None,
    settings: Any = None,
) -> None:
    """Record one skill action.

    Args:
        event: The action, skill, outcome, and latency.
        conversation_id: The owning conversation, when one is available.
        settings: The deployment settings; the event reaches session
            telemetry only when skill telemetry is enabled.
    """
    _increment(f"{event.action}:{event.outcome}")
    if event.provider:
        _increment(f"provider:{event.provider}")
    logger.info(
        "deepspace.skill %s %s",
        event.action,
        event.outcome,
        extra={
            "deepspace.skill.action": event.action,
            "deepspace.skill.name": event.name,
            "deepspace.skill.provider": event.provider,
            "deepspace.skill.source": event.source,
            "deepspace.skill.outcome": event.outcome,
            "deepspace.skill.duration_ms": round(event.duration_ms, 2),
        },
    )
    if settings is None or conversation_id is None:
        return
    if not bool(getattr(settings, "deepspace_skill_telemetry_enabled", True)):
        return
    try:
        from app.deepspace.services.session_telemetry import get_session_telemetry

        get_session_telemetry(settings).record_session_event(
            conversation_id=str(conversation_id),
            event={
                "type": f"skill_{event.action}",
                "time": int(time.time() * 1000),
                "tool": event.name,
            },
        )
    except Exception:  # noqa: BLE001 - telemetry must never break a tool call.
        logger.debug("Skill telemetry export failed", exc_info=True)


__all__ = [
    "SkillTelemetryEvent",
    "record_skill_event",
    "reset_skill_counters",
    "skill_counters",
]
