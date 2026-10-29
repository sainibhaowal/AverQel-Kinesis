"""The deployment's own statement about who the agent is for its users.

A preset names how an agent runs: which tools, which sandbox, which
reasoning effort.  It does not name *who the agent is for this deployment*.
A deployment that serves one company's engineers, or one department, needs one
short statement of that audience, and the statement belongs to the
deployment rather than to each conversation.

This module turns that statement into a single system-prompt section that is
injected before every turn, under its own key, so repeated provider rounds
replace it instead of accumulating it.  It never replaces the product's own
policy, and an empty or disabled configuration injects nothing.

A persona that is enabled but not written is a misconfiguration.  Nothing is
injected, and the read reports ``unconfigured`` so a deployment can show the
deployment owner why the statement never reached the model.
"""

from __future__ import annotations

from typing import Any

#: The persona status when the deployment did not configure one.
STATUS_UNCONFIGURED = "unconfigured"

#: The persona status when the deployment disabled it explicitly.
STATUS_DISABLED = "disabled"

#: The persona status when a statement is in force.
STATUS_ACTIVE = "active"


def deployment_persona_state(settings: Any) -> dict[str, Any]:
    """Report the deployment persona's state without rendering it.

    Args:
        settings: The deployment settings in force.

    Returns:
        The state: whether the feature is enabled, whether a statement is
        configured, its status, its title, how many characters it carries,
        and the ceiling it may reach.
    """
    enabled = bool(getattr(settings, "deepspace_deployment_persona_enabled", False))
    title = str(getattr(settings, "deepspace_deployment_persona_title", "") or "").strip()
    instructions = str(
        getattr(settings, "deepspace_deployment_persona_instructions", "") or ""
    ).strip()
    ceiling = int(getattr(settings, "deepspace_deployment_persona_max_chars", 4_000) or 4_000)
    if not enabled:
        status = STATUS_DISABLED
    elif not instructions:
        status = STATUS_UNCONFIGURED
    else:
        status = STATUS_ACTIVE
    return {
        "enabled": enabled,
        "configured": bool(instructions),
        "status": status,
        "title": title,
        "characters": len(instructions),
        "max_characters": ceiling,
        "over_limit": len(instructions) > ceiling,
    }


def deployment_persona_section(settings: Any) -> str | None:
    """Render the deployment persona as one system-prompt section.

    The section is short and subordinate by design.  The product's policy
    runs first and its instruction still holds when the two disagree; the
    statement says who the agent is talking to, not what the agent may do.

    Args:
        settings: The deployment settings in force.

    Returns:
        The section text, or None when the deployment carries no statement or
        disabled the feature.
    """
    state = deployment_persona_state(settings)
    if state["status"] != STATUS_ACTIVE or state["over_limit"]:
        return None
    title = str(state["title"] or "This deployment")
    instructions = str(
        getattr(settings, "deepspace_deployment_persona_instructions", "") or ""
    ).strip()
    return (
        f"Deployment persona ({title}). This statement comes from the "
        "deployment that runs this conversation. Read it as the audience and "
        "the voice the answers should use. It never replaces the policy above: "
        f"when the two disagree, the policy wins.\n\n{instructions}"
    )
