"""Subagent descriptor model for DeepSpace.

Python-native port of the DeepSeek Harness subagent descriptor system.
Provides versioned descriptors, folding from persisted events, composition
application, and cold-resume support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: The current descriptor format version.
SUBAGENT_DESCRIPTOR_VERSION = 3

#: Descriptor mode types.
SubagentMode = Literal["one-shot", "continuable"]

#: Reasoning effort levels.
ReasoningEffortId = str


@dataclass(frozen=True, slots=True)
class SubagentDescriptorBase:
    """Base fields shared by all descriptor types."""

    version: int
    mode: SubagentMode
    provider: str


@dataclass(frozen=True, slots=True)
class OneShotSubagentDescriptor(SubagentDescriptorBase):
    """A one-shot subagent descriptor (terminal, non-resumable)."""

    label: str | None = None
    agent_preset: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuableSubagentDescriptor(SubagentDescriptorBase):
    """A continuable subagent descriptor (resumable)."""

    label: str
    agent_provider: str | None = None
    agent_model: str | None = None
    agent_reasoning_effort: ReasoningEffortId | None = None
    persona: str | None = None
    tool_filter: dict[str, Any] | None = None
    agent_preset: str | None = None


SubagentDescriptor = OneShotSubagentDescriptor | ContinuableSubagentDescriptor


class DescriptorParseError(Exception):
    """Raised when a descriptor cannot be parsed or validated."""

    pass


def parse_subagent_descriptor(data: dict[str, Any]) -> SubagentDescriptor | None:
    """Parse a raw descriptor dict into a typed descriptor.

    Args:
        data: The raw descriptor data from a session event or DB row.

    Returns:
        The parsed descriptor, or None if version mismatch or invalid.

    Raises:
        DescriptorParseError: When a current-version payload doesn't match schema.
    """
    if not isinstance(data, dict):
        return None

    version = data.get("version")
    if version != SUBAGENT_DESCRIPTOR_VERSION:
        return None

    mode = data.get("mode")
    provider = data.get("provider")
    if mode not in ("one-shot", "continuable") or not isinstance(provider, str):
        return None

    if mode == "one-shot":
        label = data.get("label")
        if label is not None and not isinstance(label, str):
            return None
        agent_preset = data.get("agentPreset")
        if agent_preset is not None and not isinstance(agent_preset, str):
            return None
        return OneShotSubagentDescriptor(
            version=version,
            mode=mode,
            provider=provider,
            label=label,
            agent_preset=agent_preset,
        )

    # Continuable
    label = data.get("label")
    if not isinstance(label, str):
        raise DescriptorParseError("Continuable descriptor requires a string label")
    for key in ("agentProvider", "agentModel", "agentReasoningEffort", "persona", "agentPreset"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise DescriptorParseError(f"Continuable descriptor field {key} must be a string")
    tool_filter = data.get("toolFilter")
    if tool_filter is not None and not isinstance(tool_filter, dict):
        raise DescriptorParseError("Continuable descriptor field toolFilter must be an object")

    return ContinuableSubagentDescriptor(
        version=version,
        mode=mode,
        provider=provider,
        label=label,
        agent_provider=data.get("agentProvider"),
        agent_model=data.get("agentModel"),
        agent_reasoning_effort=data.get("agentReasoningEffort"),
        persona=data.get("persona"),
        tool_filter=tool_filter,
        agent_preset=data.get("agentPreset"),
    )


def fold_subagent_descriptor(events: list[dict[str, Any]]) -> SubagentDescriptor | None:
    """Fold a list of session events to find the subagent descriptor.

    The first 'subagent/descriptor' event is authoritative. A later event
    of the same type cannot rewrite the declared composition.

    Args:
        events: List of session events (dicts with 'type' and 'data' keys).

    Returns:
        The folded descriptor, or None if no descriptor event found or version mismatch.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "subagent/descriptor":
            continue
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        return parse_subagent_descriptor(data)
    return None


def snapshot_subagent_descriptor(
    mode: SubagentMode,
    provider: str,
    label: str,
    agent_provider: str | None = None,
    agent_model: str | None = None,
    agent_reasoning_effort: str | None = None,
    persona: str | None = None,
    tool_filter: dict[str, Any] | None = None,
    agent_preset: str | None = None,
) -> dict[str, Any]:
    """Create a versioned descriptor dict ready for persistence.

    Args:
        mode: The subagent mode.
        provider: The provider name.
        label: The task label.
        agent_provider: Optional agent provider override.
        agent_model: Optional agent model override.
        agent_reasoning_effort: Optional reasoning effort.
        persona: Optional persona.
        tool_filter: Optional tool filter.

    Returns:
        A versioned descriptor dict.
    """
    base = {
        "version": SUBAGENT_DESCRIPTOR_VERSION,
        "mode": mode,
        "provider": provider,
    }
    if mode == "one-shot":
        result = {**base, "label": label}
        if agent_preset is not None:
            result["agentPreset"] = agent_preset
        return result

    result = {**base, "label": label}
    if agent_provider is not None:
        result["agentProvider"] = agent_provider
    if agent_model is not None:
        result["agentModel"] = agent_model
    if agent_reasoning_effort is not None:
        result["agentReasoningEffort"] = agent_reasoning_effort
    if persona is not None:
        result["persona"] = persona
    if tool_filter is not None:
        result["toolFilter"] = tool_filter
    if agent_preset is not None:
        result["agentPreset"] = agent_preset
    return result


def has_independent_preset_selection(events: list[dict[str, Any]]) -> bool:
    """Check if events contain an independent agent-preset selection.

    A preset selection that appears before any subagent/descriptor event
    indicates independent selection (not derived from parent).

    Args:
        events: List of session events.

    Returns:
        True if an independent preset selection exists.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "subagent/descriptor":
            return False
        if event.get("type") in {"agent-preset/selected", "subagent/preset"}:
            return True
    return False


def apply_child_composition(
    child: Any,
    parent: Any,
    composition: dict[str, Any],
) -> None:
    """Apply composition (persona, toolFilter, agentPreset) to a child agent.

    Args:
        child: The child agent to compose.
        parent: The parent agent.
        composition: Composition dict with optional persona, toolFilter, agentPreset.
    """
    if composition.get("persona") is not None:
        if hasattr(child, "persona"):
            child.persona = composition["persona"]
    if composition.get("toolFilter") is not None:
        if hasattr(child, "tool_filter"):
            child.tool_filter = composition["toolFilter"]
    if composition.get("agentPreset") is not None:
        if hasattr(child, "agent_preset"):
            child.agent_preset = composition["agentPreset"]
        elif hasattr(child, "descriptor"):
            descriptor = getattr(child, "descriptor", None)
            child.descriptor = {
                **(descriptor if isinstance(descriptor, dict) else {}),
                "agentPreset": composition["agentPreset"],
            }
    if composition.get("agentProvider") is not None and hasattr(child, "agent_provider"):
        child.agent_provider = composition["agentProvider"]
    if composition.get("agentModel") is not None and hasattr(child, "agent_model"):
        child.agent_model = composition["agentModel"]
    if composition.get("agentReasoningEffort") is not None and hasattr(child, "reasoning_effort"):
        child.reasoning_effort = composition["agentReasoningEffort"]


def descriptor_to_run_spec(descriptor: SubagentDescriptor) -> dict[str, Any]:
    """Convert a descriptor to a SubagentRunSpec-compatible dict.

    Args:
        descriptor: The parsed descriptor.

    Returns:
        A dict with provider, label, mode, and composition fields.
    """
    result = {
        "provider": descriptor.provider,
        "mode": descriptor.mode,
    }
    if isinstance(descriptor, ContinuableSubagentDescriptor):
        result["label"] = descriptor.label
        if descriptor.agent_provider is not None:
            result["agent_provider"] = descriptor.agent_provider
        if descriptor.agent_model is not None:
            result["agent_model"] = descriptor.agent_model
        if descriptor.agent_reasoning_effort is not None:
            result["agent_reasoning_effort"] = descriptor.agent_reasoning_effort
        if descriptor.persona is not None:
            result["persona"] = descriptor.persona
        if descriptor.tool_filter is not None:
            result["tool_filter"] = descriptor.tool_filter
        if descriptor.agent_preset is not None:
            result["agent_preset"] = descriptor.agent_preset
    elif isinstance(descriptor, OneShotSubagentDescriptor):
        result["label"] = descriptor.label
        if descriptor.agent_preset is not None:
            result["agent_preset"] = descriptor.agent_preset
    return result


__all__ = [
    "SUBAGENT_DESCRIPTOR_VERSION",
    "SubagentMode",
    "ReasoningEffortId",
    "SubagentDescriptorBase",
    "OneShotSubagentDescriptor",
    "ContinuableSubagentDescriptor",
    "SubagentDescriptor",
    "DescriptorParseError",
    "parse_subagent_descriptor",
    "fold_subagent_descriptor",
    "snapshot_subagent_descriptor",
    "has_independent_preset_selection",
    "apply_child_composition",
    "descriptor_to_run_spec",
]
