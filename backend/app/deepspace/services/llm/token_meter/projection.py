"""Token metering projections for DeepSpace LLM service."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.deepspace.services.llm.brand import ProviderRoute


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """Token usage for a single turn."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    provider: ProviderRoute
    model: str
    timestamp: int


@dataclass(frozen=True, slots=True)
class RoutePricing:
    """Pricing for a provider/model route."""

    provider: ProviderRoute
    model: str
    input_price_per_1k: float
    output_price_per_1k: float
    image_price_per_1k: float | None = None


@dataclass(frozen=True, slots=True)
class MeasurementProjection:
    """Aggregated usage measurements."""

    total_turns: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    by_route: dict[str, TurnUsage]


@dataclass(frozen=True, slots=True)
class ContextBreakdownProjection:
    """Token breakdown by context type."""

    system_tokens: int
    user_tokens: int
    assistant_tokens: int
    tool_tokens: int
    image_tokens: int


@dataclass(frozen=True, slots=True)
class SurfaceProjection:
    """Usage per surface (chat, command, tool)."""

    chat: TurnUsage
    command: TurnUsage
    tool: TurnUsage


@dataclass(frozen=True, slots=True)
class UsageProjection:
    """Total usage projection for a session/conversation."""

    session_id: str
    turn_usages: list[TurnUsage] = field(default_factory=list)
    total_cost_usd: float = 0.0

    def add_turn(self, usage: TurnUsage) -> UsageProjection:
        new = UsageProjection(
            session_id=self.session_id,
            turn_usages=self.turn_usages + [usage],
            total_cost_usd=self.total_cost_usd,
        )
        return new


__all__ = [
    "TurnUsage",
    "RoutePricing",
    "MeasurementProjection",
    "ContextBreakdownProjection",
    "SurfaceProjection",
    "UsageProjection",
]
