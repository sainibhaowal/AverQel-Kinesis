"""The advisory repeat-tool reminder for DeepSpace.

An agent that keeps calling the same tool with identical arguments stops
making progress while still paying for every call.  The reminder notices the
exact repetition and asks the model to inspect the previous result and either
change approach or finish.

The reminder is advisory.  It never blocks or delays a legitimate repeated
call: the turn's own :mod:`app.deepspace.services.loop_guard` owns the hard
refusal, and this module only supplies the nudge that arrives first.

Repeats are tracked separately for each agent and are cleared by a new user
message, so a fresh instruction is never treated as a loop.  The tracker is
in-memory only.  A conversation resumed from persistence starts with a fresh
chain, which is the accepted cost of a heuristic nudge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The reminder shown at the first configured repeat threshold.
FIRST_THRESHOLD_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. Carefully analyze "
    "the previous result before calling again: if the task is not complete, try a different "
    "approach or different arguments instead of repeating the call."
)

#: The reminder shown at every threshold after the first one.
LATER_THRESHOLD_REMINDER = (
    "Repeated tool call detected:\n"
    "- tool: {tool}\n"
    "- consecutive_calls: {count}\n"
    "- arguments: {arguments}\n"
    "The repeated calls are not making progress. Do not call this tool with these exact "
    "arguments again. Inspect the latest result and choose a different action, different "
    "arguments, or finish the task if enough evidence has been gathered."
)

#: The default repeat counts that trigger a reminder.
DEFAULT_THRESHOLDS: tuple[int, ...] = (3, 5, 8)

#: The default number of argument characters a detailed reminder shows.
DEFAULT_ARGUMENTS_PREVIEW_CHARS = 500

#: The smallest repeat count a threshold may name.
MINIMUM_THRESHOLD = 2

#: The largest number of argument characters a detailed reminder may show.
MAXIMUM_ARGUMENTS_PREVIEW_CHARS = 20_000


class RepeatToolReminderError(ValueError):
    """Raised when a deployment configures the reminder with unusable values."""


@dataclass(frozen=True, slots=True)
class RepeatToolReminderConfig:
    """The reminder's configuration.

    Attributes:
        thresholds: The consecutive-repeat counts that trigger a reminder.
            The list must not be empty, every count must be at least
            :data:`MINIMUM_THRESHOLD`, and no count may repeat.
        include: The tool names to track.  An empty list tracks every tool.
        exclude: The tool names never to track.  A call to an excluded tool
            neither increments nor resets a chain.
        arguments_preview_chars: How many characters of the repeated arguments
            a detailed reminder shows.
    """

    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    arguments_preview_chars: int = DEFAULT_ARGUMENTS_PREVIEW_CHARS

    @classmethod
    def from_settings(cls, settings: Any) -> RepeatToolReminderConfig:
        """Read the configuration from the deployment settings.

        A settings object that does not carry a field keeps the default, so
        the reminder works before the settings land.  Invalid values raise
        :class:`RepeatToolReminderError` rather than silently changing the
        reminder's behavior.

        Args:
            settings: The deployment settings, or any object with the same
                attribute names.

        Returns:
            The configuration the deployment runs under.

        Raises:
            RepeatToolReminderError: When a configured value is unusable.
        """
        thresholds = _thresholds_setting(settings)
        include = _name_list_setting(settings, "deepspace_repeat_tool_reminder_include")
        exclude = _name_list_setting(settings, "deepspace_repeat_tool_reminder_exclude")
        preview = _preview_setting(settings)
        return cls(
            thresholds=thresholds,
            include=include,
            exclude=exclude,
            arguments_preview_chars=preview,
        )

    def tracks(self, tool_name: str) -> bool:
        """Whether one tool call is part of the tracked chains.

        Args:
            tool_name: The tool the model called.

        Returns:
            Whether the call counts towards, or resets, a chain.
        """
        if tool_name in self.exclude:
            return False
        if self.include and tool_name not in self.include:
            return False
        return True

    def validate(self) -> None:
        """Check that the configuration is usable.

        Raises:
            RepeatToolReminderError: When a configured value is unusable.
        """
        if not self.thresholds:
            raise RepeatToolReminderError(
                "The repeat-tool reminder needs at least one repeat threshold."
            )
        if len(set(self.thresholds)) != len(self.thresholds):
            raise RepeatToolReminderError(
                "The repeat-tool reminder thresholds must not repeat a count."
            )
        for threshold in self.thresholds:
            if threshold < MINIMUM_THRESHOLD:
                raise RepeatToolReminderError(
                    f"A repeat-tool reminder threshold must be at least {MINIMUM_THRESHOLD}: "
                    f"{threshold}."
                )
        if not (0 < self.arguments_preview_chars <= MAXIMUM_ARGUMENTS_PREVIEW_CHARS):
            raise RepeatToolReminderError(
                "The repeat-tool reminder argument preview must be between 1 and "
                f"{MAXIMUM_ARGUMENTS_PREVIEW_CHARS} characters."
            )


@dataclass(slots=True)
class _Chain:
    """One agent's tracked repetition chain.

    Attributes:
        tool_name: The tool the chain currently tracks.
        canonical_arguments: The canonical arguments the chain currently tracks.
        count: How many consecutive identical calls the chain has seen.
    """

    tool_name: str = ""
    canonical_arguments: str = ""
    count: int = 0


@dataclass(slots=True)
class RepeatToolReminder:
    """The advisory nudge for an agent stuck in an identical tool-call loop.

    The tracker keeps one chain per agent.  A chain counts consecutive calls
    that carry the same tool and canonically identical arguments.  A different
    tracked call resets the count to one.  A call to an untracked tool neither
    increments nor resets the chain, so bookkeeping tools interleaved into a
    loop do not launder it.

    The tracker takes no lock: one conversation's turn runs on one event loop,
    and the counters are plain integers.
    """

    config: RepeatToolReminderConfig = field(default_factory=RepeatToolReminderConfig)
    _chains: dict[str, _Chain] = field(default_factory=dict)

    def record(self, agent_key: str, tool_name: str, arguments: Mapping[str, Any]) -> str | None:
        """Record one completed tool call and return the reminder it triggers.

        The call counts towards the agent's chain when the configuration
        tracks its tool.  A call that reaches a configured threshold produces
        the reminder for that threshold; a call below every threshold produces
        nothing.

        Args:
            agent_key: The agent the call belongs to.
            tool_name: The tool the model called.
            arguments: The call's arguments.

        Returns:
            The reminder text, or None when no threshold was reached.
        """
        if not self.config.tracks(tool_name):
            return None
        canonical = canonical_arguments(arguments)
        chain = self._chains.get(agent_key)
        if chain is None or chain.tool_name != tool_name or chain.canonical_arguments != canonical:
            chain = _Chain(tool_name=tool_name, canonical_arguments=canonical, count=0)
            self._chains[agent_key] = chain
        chain.count += 1
        count = chain.count
        if count not in self.config.thresholds:
            return None
        if count == min(self.config.thresholds):
            return FIRST_THRESHOLD_REMINDER
        return LATER_THRESHOLD_REMINDER.format(
            tool=tool_name,
            count=count,
            arguments=self._preview(canonical),
        )

    def reset(self, agent_key: str) -> None:
        """Forget one agent's chain.

        A new user message calls this, so a fresh instruction is never
        treated as a loop.

        Args:
            agent_key: The agent whose chain is forgotten.
        """
        self._chains.pop(agent_key, None)

    def forget(self, agent_key: str) -> None:
        """Forget one agent's chain and every count it held.

        Args:
            agent_key: The agent whose chain is forgotten.
        """
        self.reset(agent_key)

    def chain_for(self, agent_key: str) -> _Chain | None:
        """Return one agent's current chain.

        Args:
            agent_key: The agent whose chain is read.

        Returns:
            The chain, or None when the agent has no tracked repetition.
        """
        return self._chains.get(agent_key)

    def _preview(self, canonical: str) -> str:
        """Bound the argument text a detailed reminder shows.

        A capped preview ends exactly with ``… (+<omitted> more chars)``.

        Args:
            canonical: The canonical arguments of the repeated call.

        Returns:
            The bounded argument text.
        """
        limit = self.config.arguments_preview_chars
        if len(canonical) <= limit:
            return canonical
        omitted = len(canonical) - limit
        return f"{canonical[:limit]}… (+{omitted} more chars)"


class RepeatToolReminderRegistry:
    """The process-wide registry of per-conversation reminder trackers.

    The registry creates a tracker on the conversation's first use and keeps
    it until the conversation's tracker is dropped.  A tracker that already
    exists keeps the configuration it was created with, so one conversation's
    reminder behaves the same way for every turn that runs in the process.
    """

    def __init__(self) -> None:
        self._trackers: dict[str, RepeatToolReminder] = {}

    def get(
        self,
        conversation_id: str,
        config: RepeatToolReminderConfig | None = None,
    ) -> RepeatToolReminder:
        """Return the conversation's tracker.

        Args:
            conversation_id: The conversation the tracker watches.
            config: The configuration for a tracker the registry creates now;
                it is ignored for a tracker that already exists.

        Returns:
            The conversation's tracker.
        """
        tracker = self._trackers.get(conversation_id)
        if tracker is None:
            tracker = RepeatToolReminder(
                config=config if config is not None else RepeatToolReminderConfig()
            )
            self._trackers[conversation_id] = tracker
        return tracker

    def drop(self, conversation_id: str) -> None:
        """Forget the conversation's tracker.

        Args:
            conversation_id: The conversation whose tracker is forgotten.
        """
        self._trackers.pop(conversation_id, None)

    def clear(self) -> None:
        """Forget every tracker."""
        self._trackers.clear()


#: The registry the process shares.
_REGISTRY = RepeatToolReminderRegistry()


def repeat_tool_reminder_registry() -> RepeatToolReminderRegistry:
    """Return the process-wide registry of per-conversation reminder trackers."""
    return _REGISTRY


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """Return the stable string form of one call's arguments.

    The keys are sorted and the values are rendered as text, so two calls
    that carry the same arguments in a different order are the same call.

    Args:
        arguments: The call's arguments.

    Returns:
        The canonical form of the arguments.
    """
    return json.dumps(dict(arguments), sort_keys=True, default=str, ensure_ascii=False)


def _thresholds_setting(settings: Any) -> tuple[int, ...]:
    """Read the repeat counts the deployment configured.

    Args:
        settings: The deployment settings.

    Returns:
        The configured repeat counts.

    Raises:
        RepeatToolReminderError: When the configured list is unusable.
    """
    raw = getattr(settings, "deepspace_repeat_tool_reminder_thresholds", None)
    if raw is None:
        return DEFAULT_THRESHOLDS
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise RepeatToolReminderError(
            "The repeat-tool reminder thresholds must be a list of repeat counts."
        )
    thresholds: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise RepeatToolReminderError(
                "A repeat-tool reminder threshold must be a whole number."
            )
        thresholds.append(item)
    if not thresholds:
        raise RepeatToolReminderError("The repeat-tool reminder thresholds must not be empty.")
    if len(set(thresholds)) != len(thresholds):
        raise RepeatToolReminderError(
            "The repeat-tool reminder thresholds must not repeat a count."
        )
    for threshold in thresholds:
        if threshold < MINIMUM_THRESHOLD:
            raise RepeatToolReminderError(
                f"A repeat-tool reminder threshold must be at least {MINIMUM_THRESHOLD}: "
                f"{threshold}."
            )
    return tuple(thresholds)


def _name_list_setting(settings: Any, name: str) -> tuple[str, ...]:
    """Read one tool-name list the deployment configured.

    Args:
        settings: The deployment settings.
        name: The setting's name.

    Returns:
        The configured tool names.

    Raises:
        RepeatToolReminderError: When the configured list is unusable.
    """
    raw = getattr(settings, name, None)
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise RepeatToolReminderError(f"The setting {name!r} must be a list of tool names.")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RepeatToolReminderError(f"The setting {name!r} must hold tool names.")
        names.append(item.strip())
    return tuple(names)


def _preview_setting(settings: Any) -> int:
    """Read the argument preview length the deployment configured.

    Args:
        settings: The deployment settings.

    Returns:
        The configured preview length.

    Raises:
        RepeatToolReminderError: When the configured value is unusable.
    """
    raw: object = getattr(settings, "deepspace_repeat_tool_reminder_arguments_preview_chars", None)
    if raw is None:
        return DEFAULT_ARGUMENTS_PREVIEW_CHARS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RepeatToolReminderError(
            "The repeat-tool reminder argument preview must be a whole number."
        )
    if not 0 < raw <= MAXIMUM_ARGUMENTS_PREVIEW_CHARS:
        raise RepeatToolReminderError(
            "The repeat-tool reminder argument preview must be between 1 and "
            f"{MAXIMUM_ARGUMENTS_PREVIEW_CHARS} characters."
        )
    return raw


__all__ = [
    "DEFAULT_ARGUMENTS_PREVIEW_CHARS",
    "DEFAULT_THRESHOLDS",
    "FIRST_THRESHOLD_REMINDER",
    "LATER_THRESHOLD_REMINDER",
    "MAXIMUM_ARGUMENTS_PREVIEW_CHARS",
    "MINIMUM_THRESHOLD",
    "RepeatToolReminder",
    "RepeatToolReminderConfig",
    "RepeatToolReminderError",
    "RepeatToolReminderRegistry",
    "canonical_arguments",
    "repeat_tool_reminder_registry",
]
