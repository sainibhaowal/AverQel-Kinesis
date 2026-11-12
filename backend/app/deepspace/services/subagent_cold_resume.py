"""Cold resume for continuable subagents in DeepSpace.

Python-native port of the DeepSeek Harness cold resume logic. Reconstructs
a continuable child agent from persisted descriptors and session events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.deepspace.services.subagent_descriptor import (
    ContinuableSubagentDescriptor,
    DescriptorParseError,
    apply_child_composition,
    descriptor_to_run_spec,
    fold_subagent_descriptor,
    has_independent_preset_selection,
    parse_subagent_descriptor,
)

logger = logging.getLogger(__name__)


@dataclass
class ColdResumeResult:
    """Result of a cold resume attempt."""

    success: bool
    child_id: str
    descriptor: ContinuableSubagentDescriptor | None = None
    run_spec: dict[str, Any] | None = None
    error: str | None = None
    composition: dict[str, Any] = field(default_factory=dict)


def cold_resume_from_descriptor(
    child_id: str,
    events: list[dict[str, Any]],
    parent: Any = None,
) -> ColdResumeResult:
    """Cold-resume a persisted child from its descriptor events.

    Args:
        child_id: The child session ID.
        events: The child's persisted session events.
        parent: The live parent agent (for authorization).

    Returns:
        A ColdResumeResult with success/failure and reconstructed spec.
    """
    # Fold events to descriptor
    descriptor = fold_subagent_descriptor(events)
    if descriptor is None:
        return ColdResumeResult(
            success=False,
            child_id=child_id,
            error=f'subagent "{child_id}" has no supported continuation state',
        )

    if not isinstance(descriptor, ContinuableSubagentDescriptor):
        return ColdResumeResult(
            success=False,
            child_id=child_id,
            error=f'subagent "{child_id}" is a one-shot agent and cannot be resumed',
        )

    # Check for independent preset selection
    independently_selected = has_independent_preset_selection(events)
    agent_preset = descriptor.agent_preset
    if independently_selected:
        # Look for the independent child-preset event written before its
        # descriptor. Older SDK-shaped logs use agent-preset/selected.
        for event in events:
            if isinstance(event, dict) and event.get("type") in {
                "agent-preset/selected",
                "subagent/preset",
            }:
                data = event.get("data", {})
                if isinstance(data, dict):
                    agent_preset = data.get("agentPreset", data.get("preset_id"))
                break

    if independently_selected and not isinstance(agent_preset, str):
        return ColdResumeResult(
            success=False,
            child_id=child_id,
            error=f'subagent "{child_id}" has no agent preset projection for restoration',
        )

    # Convert descriptor to run spec and build the matching composition.
    run_spec = descriptor_to_run_spec(descriptor)
    composition: dict[str, Any] = {}
    for descriptor_key, run_spec_key in (
        ("agent_provider", "agentProvider"),
        ("agent_model", "agentModel"),
        ("agent_reasoning_effort", "agentReasoningEffort"),
    ):
        value = run_spec.get(descriptor_key)
        if value is not None:
            composition[run_spec_key] = value
    if agent_preset is not None:
        composition["agentPreset"] = agent_preset
    if descriptor.persona is not None:
        composition["persona"] = descriptor.persona
    if descriptor.tool_filter is not None:
        composition["toolFilter"] = descriptor.tool_filter

    return ColdResumeResult(
        success=True,
        child_id=child_id,
        descriptor=descriptor,
        run_spec=run_spec,
        composition=composition,
    )


def cold_resume_from_db_row(
    child_row: Any,
    events: list[dict[str, Any]] | None = None,
) -> ColdResumeResult:
    """Cold-resume a persisted child from its DB row.

    Args:
        child_row: A DeepSpaceSubagentRun ORM row (or dict).
        events: Optional session events for descriptor folding.

    Returns:
        A ColdResumeResult.
    """
    # Extract fields from row or dict
    if isinstance(child_row, dict):
        child_id = str(child_row.get("id", ""))
        mode = child_row.get("mode", "one-shot")
        descriptor_data = child_row.get("descriptor", {})
        persona = child_row.get("persona")
        tool_filter = child_row.get("tool_filter")
        agent_provider = child_row.get("agent_provider")
        agent_model = child_row.get("agent_model")
        reasoning_effort = child_row.get("reasoning_effort")
        label = child_row.get("label")
        provider = child_row.get("provider", "in-process")
    else:
        child_id = str(getattr(child_row, "id", ""))
        mode = getattr(child_row, "mode", "one-shot")
        descriptor_data = getattr(child_row, "descriptor", {}) or {}
        persona = getattr(child_row, "persona", None)
        tool_filter = getattr(child_row, "tool_filter", None)
        agent_provider = getattr(child_row, "agent_provider", None)
        agent_model = getattr(child_row, "agent_model", None)
        reasoning_effort = getattr(child_row, "reasoning_effort", None)
        label = getattr(child_row, "label", None)
        provider = (
            getattr(child_row, "provider", "in-process")
            if hasattr(child_row, "provider")
            else "in-process"
        )

    # The row descriptor is authoritative. Fall back to typed events for older
    # or incomplete rows that predate durable descriptors.
    descriptor = None
    if isinstance(descriptor_data, dict) and descriptor_data:
        try:
            descriptor = parse_subagent_descriptor(descriptor_data)
        except DescriptorParseError as exc:
            return ColdResumeResult(
                success=False,
                child_id=child_id,
                error=f'subagent "{child_id}" has an invalid continuation descriptor: {exc}',
            )

    if descriptor is None and events:
        event_descriptor = fold_subagent_descriptor(events)
        if event_descriptor is not None:
            event_result = cold_resume_from_descriptor(child_id, events)
            if event_result.success or event_result.descriptor is not None:
                return event_result
            descriptor = event_descriptor

    if descriptor is None:
        # Build descriptor from DB row fields
        if mode == "continuable":
            descriptor = ContinuableSubagentDescriptor(
                version=3,  # SUBAGENT_DESCRIPTOR_VERSION
                mode="continuable",
                provider=provider,
                label=label or "",
                agent_provider=agent_provider,
                agent_model=agent_model,
                agent_reasoning_effort=reasoning_effort,
                persona=persona,
                tool_filter=tool_filter,
                agent_preset=(
                    descriptor_data.get("agentPreset")
                    if isinstance(descriptor_data, dict)
                    else None
                ),
            )
        else:
            from app.deepspace.services.subagent_descriptor import OneShotSubagentDescriptor

            descriptor = OneShotSubagentDescriptor(
                version=3,
                mode="one-shot",
                provider=provider,
                label=label,
            )

    if not isinstance(descriptor, ContinuableSubagentDescriptor):
        return ColdResumeResult(
            success=False,
            child_id=child_id,
            error=f'subagent "{child_id}" is a one-shot agent and cannot be resumed',
        )

    run_spec = descriptor_to_run_spec(descriptor)
    composition: dict[str, Any] = {}
    for descriptor_key, run_spec_key in (
        ("agent_provider", "agentProvider"),
        ("agent_model", "agentModel"),
        ("agent_reasoning_effort", "agentReasoningEffort"),
    ):
        value = run_spec.get(descriptor_key)
        if value is not None:
            composition[run_spec_key] = value
    agent_preset = getattr(descriptor, "agent_preset", None)
    # Older v3 rows can have a valid descriptor without the independently
    # selected preset. Recover it only from an event that precedes that
    # descriptor; later events cannot rewrite the original composition.
    if events and has_independent_preset_selection(events):
        for event in events:
            if not isinstance(event, dict) or event.get("type") not in {
                "agent-preset/selected",
                "subagent/preset",
            }:
                continue
            data = event.get("data")
            selected = (
                data.get("agentPreset", data.get("preset_id")) if isinstance(data, dict) else None
            )
            if isinstance(selected, str) and selected.strip():
                agent_preset = selected.strip()
            break
    if agent_preset is not None:
        composition["agentPreset"] = agent_preset
    # Prefer the immutable persisted composition; use row columns only for
    # older descriptors that did not carry those fields.
    if descriptor.persona is not None:
        composition["persona"] = descriptor.persona
    elif persona is not None:
        composition["persona"] = persona
    if descriptor.tool_filter is not None:
        composition["toolFilter"] = descriptor.tool_filter
    elif tool_filter is not None:
        composition["toolFilter"] = tool_filter

    return ColdResumeResult(
        success=True,
        child_id=child_id,
        descriptor=descriptor,
        run_spec=run_spec,
        composition=composition,
    )


def apply_cold_resume_composition(
    child: Any,
    cold_result: ColdResumeResult,
) -> None:
    """Apply cold resume composition to a restored child agent.

    Args:
        child: The restored child agent.
        cold_result: The cold resume result with composition.
    """
    if cold_result.composition:
        apply_child_composition(child, None, cold_result.composition)


__all__ = [
    "ColdResumeResult",
    "cold_resume_from_descriptor",
    "cold_resume_from_db_row",
    "apply_cold_resume_composition",
]
