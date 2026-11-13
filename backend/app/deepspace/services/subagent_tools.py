"""Subagent tool definitions for DeepSpace.

Python-native port of the AverQel KineSis tool-subagent and
tool-subagent-control packages. Provides tool definitions for subagent
delegation, control, and listing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SubagentToolSpec:
    """Tool specification for subagent operations."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., Any] | None = None


# Tool names that subagents never receive (prevent recursive delegation)
DELEGATION_TOOL_NAMES = frozenset(
    {
        "subagent",
        "subagent_fork",
        "send_message",
        "interrupt_agent",
        "list_agents",
        "get_agent",
    }
)

# Tools that run a child's turn inside the caller's own call
CHILD_TURN_TOOL_NAMES = frozenset({"subagent", "subagent_fork"})

# Team tools a child never receives
TEAM_TOOL_NAMES = frozenset(
    {
        "spawn_teammate",
        "team_send_message",
        "list_teammates",
        "team_list_members",
        "team_get_member",
        "wait_agent",
        "team_task_create",
        "team_task_list",
        "team_task_update",
    }
)

# Tools a child never receives (no user channel)
CHILD_EXCLUDED_TOOL_NAMES = frozenset({"ask_user"})


def list_agents_tool(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Tool handler: list all active subagent runs.

    Args:
        agents: List of agent info dicts.

    Returns:
        A dict with the agent list.
    """
    return {
        "status": "ok",
        "agents": agents,
        "count": len(agents),
    }


def get_agent_tool(agent_id: str, agent_info: dict[str, Any] | None) -> dict[str, Any]:
    """Tool handler: get info about a specific subagent.

    Args:
        agent_id: The agent ID to look up.
        agent_info: The agent info dict, or None if not found.

    Returns:
        A dict with agent details or not-found status.
    """
    if agent_info is None:
        return {"status": "not_found", "agent_id": agent_id}
    return {"status": "ok", "agent": agent_info}


def interrupt_agent_tool(agent_id: str, reason: str = "user request") -> dict[str, Any]:
    """Tool handler: interrupt a running subagent.

    Args:
        agent_id: The agent to interrupt.
        reason: The reason for interruption.

    Returns:
        A dict with interruption status.
    """
    return {
        "status": "interrupted",
        "agent_id": agent_id,
        "reason": reason,
    }


def model_selection_settings(
    provider: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build model selection settings for a subagent.

    Args:
        provider: The provider name.
        model: The model name.
        reasoning_effort: The reasoning effort level.

    Returns:
        A settings dict.
    """
    result: dict[str, Any] = {}
    if provider is not None:
        result["provider"] = provider
    if model is not None:
        result["model"] = model
    if reasoning_effort is not None:
        result["reasoning_effort"] = reasoning_effort
    return result


def preset_selection(preset: str | None = None) -> dict[str, Any]:
    """Build preset selection for a subagent.

    Args:
        preset: The preset name.

    Returns:
        A preset dict.
    """
    if preset is None:
        return {}
    return {"agentPreset": preset}


def subagent_tool_definitions() -> list[SubagentToolSpec]:
    """Project the live native catalog's six child/control tools.

    The runtime's authoritative implementations live in ``delegation`` and
    ``team`` so their tenant checks, validation, and persistence cannot drift
    from this convenience surface.
    """
    from app.deepspace.services.tools.delegation import DELEGATION_TOOLS
    from app.deepspace.services.tools.team import TEAM_TOOLS

    names = (
        "subagent",
        "subagent_fork",
        "send_message",
        "interrupt_agent",
        "list_agents",
        "get_agent",
    )
    active = {tool.name: tool for tool in (*DELEGATION_TOOLS, *TEAM_TOOLS)}
    missing = [name for name in names if name not in active]
    if missing:
        raise RuntimeError(f"The active DeepSpace tool catalog is missing: {', '.join(missing)}")

    projected: list[SubagentToolSpec] = []
    for name in names:
        tool = active[name]
        function = tool.definition.get("function", {})
        projected.append(
            SubagentToolSpec(
                name=name,
                description=str(function.get("description") or ""),
                parameters=dict(function.get("parameters") or {}),
                handler=tool.handler,
            )
        )
    return projected


__all__ = [
    "SubagentToolSpec",
    "DELEGATION_TOOL_NAMES",
    "CHILD_TURN_TOOL_NAMES",
    "TEAM_TOOL_NAMES",
    "CHILD_EXCLUDED_TOOL_NAMES",
    "list_agents_tool",
    "get_agent_tool",
    "interrupt_agent_tool",
    "model_selection_settings",
    "preset_selection",
    "subagent_tool_definitions",
]
