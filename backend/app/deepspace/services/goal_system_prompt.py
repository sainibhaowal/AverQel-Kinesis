"""The goal policy the model reads before it uses the goal tools.

A goal is the standing objective of a conversation.  The tools create and
advance it, but only the model knows when the objective is actually
achieved, when a blocker is real, and whether a request deserves a goal at
all.  This module holds the fixed policy the model receives, with the
deployment's blocked threshold interpolated into it.

The policy is guidance, not enforcement.  The threshold is also enforced
mechanically when a goal is marked blocked, so a model that ignores the
guidance still cannot claim a blocker before the threshold.
"""

from __future__ import annotations

#: The policy the model receives when the goal tools are in scope.
GOAL_SYSTEM_PROMPT = (
    "goal_set may infer goal intent from a direct human request in any language. "
    "After a session resume or fork, an active goal is disarmed: when a human asks "
    "to continue or resume in any wording or language, use goal_set with phase "
    "active to rearm it. Read a goal before you update one: copy the exact name "
    "and revision from a goal_read result, because a stale revision is rejected. "
    "Mark a goal complete only when its objective is actually achieved. Mark a goal "
    "blocked only after the same blocking condition persists for at least {threshold} "
    "consecutive goal rounds, and report that concrete condition in blocked_reason; "
    "difficulty, uncertainty, or useful remaining work is not blocked. A goal that a "
    "person paused stays paused until a person resumes it. The goal's round cap bounds "
    "automatic continuation: when the cap is reached, the goal is no longer continued, "
    "so say what remains instead of looping."
)


def goal_system_prompt(threshold: int) -> str:
    """Render the goal policy for one deployment.

    Args:
        threshold: The number of consecutive goal rounds the deployment
            requires before a goal may be marked blocked.

    Returns:
        The policy text the model receives.
    """
    return GOAL_SYSTEM_PROMPT.format(threshold=max(1, int(threshold)))


def blocked_threshold_from_settings(settings: object) -> int:
    """Read the blocked threshold the deployment configured.

    Args:
        settings: The deployment settings, or any object with the same
            attribute names.

    Returns:
        The configured threshold, at least one.
    """
    raw = getattr(settings, "deepspace_goal_blocked_threshold", None)
    if raw is None:
        return 3
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, value)


__all__ = [
    "GOAL_SYSTEM_PROMPT",
    "blocked_threshold_from_settings",
    "goal_system_prompt",
]
