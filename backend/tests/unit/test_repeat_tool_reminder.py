"""Tests for the advisory repeat-tool reminder.

The reminder is the nudge that arrives before the turn's own loop guard
refuses the call.  It never blocks a call, it only asks the model to change
approach.
"""

from __future__ import annotations

import pytest

from app.deepspace.services.repeat_tool_reminder import (
    DEFAULT_THRESHOLDS,
    FIRST_THRESHOLD_REMINDER,
    LATER_THRESHOLD_REMINDER,
    MAXIMUM_ARGUMENTS_PREVIEW_CHARS,
    MINIMUM_THRESHOLD,
    RepeatToolReminder,
    RepeatToolReminderConfig,
    RepeatToolReminderError,
    RepeatToolReminderRegistry,
    canonical_arguments,
    repeat_tool_reminder_registry,
)

pytestmark = pytest.mark.unit_no_db


class _Settings:
    """A settings object that carries only the fields a test names."""

    def __init__(self, **values: object) -> None:
        for name, value in values.items():
            setattr(self, name, value)


def _tracker(**overrides: object) -> RepeatToolReminder:
    """Build a tracker with the given configuration overrides."""
    config = RepeatToolReminderConfig(**overrides)  # type: ignore[arg-type]
    config.validate()
    return RepeatToolReminder(config=config)


def test_the_default_thresholds_are_the_documented_ones() -> None:
    assert DEFAULT_THRESHOLDS == (3, 5, 8)


def test_a_call_below_the_first_threshold_produces_nothing() -> None:
    tracker = _tracker()

    assert tracker.record("agent", "read", {"path": "a.md"}) is None
    assert tracker.record("agent", "read", {"path": "a.md"}) is None


def test_the_first_threshold_produces_the_short_reminder() -> None:
    tracker = _tracker()

    assert tracker.record("agent", "read", {"path": "a.md"}) is None
    assert tracker.record("agent", "read", {"path": "a.md"}) is None
    assert tracker.record("agent", "read", {"path": "a.md"}) == FIRST_THRESHOLD_REMINDER


def test_a_later_threshold_produces_the_detailed_reminder() -> None:
    tracker = _tracker()

    for _ in range(4):
        tracker.record("agent", "read", {"path": "a.md"})

    reminder = tracker.record("agent", "read", {"path": "a.md"})

    assert reminder is not None
    assert "- tool: read" in reminder
    assert "- consecutive_calls: 5" in reminder
    assert "- arguments: " in reminder
    assert "choose a different action" in reminder


def test_the_detailed_reminder_names_the_configured_count() -> None:
    tracker = _tracker()

    for _ in range(4):
        tracker.record("agent", "read", {"path": "a.md"})

    reminder = tracker.record("agent", "read", {"path": "a.md"})

    assert reminder == LATER_THRESHOLD_REMINDER.format(
        tool="read",
        count=5,
        arguments=canonical_arguments({"path": "a.md"}),
    )


def test_argument_order_does_not_change_the_identity() -> None:
    tracker = _tracker()

    tracker.record("agent", "read", {"path": "a.md", "mode": "text"})
    tracker.record("agent", "read", {"mode": "text", "path": "a.md"})

    assert tracker.record("agent", "read", {"path": "a.md", "mode": "text"}) == (
        FIRST_THRESHOLD_REMINDER
    )


def test_a_different_tool_resets_the_chain() -> None:
    tracker = _tracker()

    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "find", {"query": "x"})

    assert tracker.record("agent", "read", {"path": "a.md"}) is None


def test_different_arguments_reset_the_chain() -> None:
    tracker = _tracker()

    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "b.md"})

    assert tracker.record("agent", "read", {"path": "a.md"}) is None


def test_an_excluded_tool_neither_counts_nor_resets() -> None:
    tracker = _tracker(exclude=("todo_write",))

    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "todo_write", {"todo": "x"})

    assert tracker.record("agent", "read", {"path": "a.md"}) == FIRST_THRESHOLD_REMINDER


def test_an_excluded_tool_is_never_tracked() -> None:
    tracker = _tracker(exclude=("todo_write",))

    for _ in range(10):
        assert tracker.record("agent", "todo_write", {"todo": "x"}) is None


def test_an_include_list_limits_the_tracked_tools() -> None:
    tracker = _tracker(include=("read",))

    tracker.record("agent", "find", {"query": "x"})
    tracker.record("agent", "find", {"query": "x"})

    assert tracker.record("agent", "find", {"query": "x"}) is None


def test_agents_keep_separate_chains() -> None:
    tracker = _tracker()

    tracker.record("agent-a", "read", {"path": "a.md"})
    tracker.record("agent-a", "read", {"path": "a.md"})

    assert tracker.record("agent-b", "read", {"path": "a.md"}) is None
    assert tracker.record("agent-a", "read", {"path": "a.md"}) == FIRST_THRESHOLD_REMINDER


def test_a_reset_forgets_the_chain() -> None:
    tracker = _tracker()

    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "a.md"})
    tracker.reset("agent")

    assert tracker.record("agent", "read", {"path": "a.md"}) is None


def test_a_reset_of_an_unknown_agent_is_harmless() -> None:
    tracker = _tracker()

    tracker.reset("never-seen")


def test_a_capped_preview_names_the_omitted_characters() -> None:
    tracker = _tracker(arguments_preview_chars=10)
    arguments = {"path": "a" * 40}
    canonical = canonical_arguments(arguments)

    for _ in range(4):
        tracker.record("agent", "read", arguments)

    reminder = tracker.record("agent", "read", arguments)

    assert reminder is not None
    omitted = len(canonical) - 10
    assert f"… (+{omitted} more chars)" in reminder
    assert reminder.count("more chars") == 1


def test_a_preview_under_the_cap_shows_the_whole_argument() -> None:
    tracker = _tracker(arguments_preview_chars=500)
    arguments = {"path": "a.md"}

    for _ in range(4):
        tracker.record("agent", "read", arguments)

    reminder = tracker.record("agent", "read", arguments)

    assert reminder is not None
    assert "more chars" not in reminder


def test_the_chain_reports_the_tracked_call() -> None:
    tracker = _tracker()

    tracker.record("agent", "read", {"path": "a.md"})
    tracker.record("agent", "read", {"path": "a.md"})

    chain = tracker.chain_for("agent")
    assert chain is not None
    assert chain.tool_name == "read"
    assert chain.count == 2


def test_an_unknown_agent_has_no_chain() -> None:
    tracker = _tracker()

    assert tracker.chain_for("never-seen") is None


def test_the_settings_supply_the_configuration() -> None:
    settings = _Settings(
        deepspace_repeat_tool_reminder_thresholds=[4, 6],
        deepspace_repeat_tool_reminder_include=["read"],
        deepspace_repeat_tool_reminder_exclude=["write"],
        deepspace_repeat_tool_reminder_arguments_preview_chars=120,
    )

    config = RepeatToolReminderConfig.from_settings(settings)

    assert config.thresholds == (4, 6)
    assert config.include == ("read",)
    assert config.exclude == ("write",)
    assert config.arguments_preview_chars == 120


def test_settings_without_the_fields_keep_the_defaults() -> None:
    config = RepeatToolReminderConfig.from_settings(_Settings())

    assert config.thresholds == DEFAULT_THRESHOLDS
    assert config.include == ()
    assert config.exclude == ()
    assert config.arguments_preview_chars == 500


def test_an_empty_threshold_list_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds=[])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_threshold_below_the_minimum_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds=[1])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_duplicate_threshold_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds=[3, 3])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_non_numeric_threshold_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds=["three"])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_threshold_that_is_not_a_list_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds="3,5")

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_booleans_threshold_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_thresholds=[True])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_preview_below_one_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_arguments_preview_chars=0)

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_preview_above_the_cap_is_refused() -> None:
    settings = _Settings(
        deepspace_repeat_tool_reminder_arguments_preview_chars=(MAXIMUM_ARGUMENTS_PREVIEW_CHARS + 1)
    )

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_non_numeric_preview_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_arguments_preview_chars="500")

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_tool_name_list_that_is_not_a_list_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_include="read")

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_a_tool_name_list_with_an_empty_name_is_refused() -> None:
    settings = _Settings(deepspace_repeat_tool_reminder_exclude=["  "])

    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig.from_settings(settings)


def test_the_configured_minimum_threshold_is_two() -> None:
    assert MINIMUM_THRESHOLD == 2


def test_a_threshold_of_two_is_accepted() -> None:
    config = RepeatToolReminderConfig(thresholds=(2,))
    config.validate()

    tracker = RepeatToolReminder(config=config)
    assert tracker.record("agent", "read", {"path": "a.md"}) is None
    assert tracker.record("agent", "read", {"path": "a.md"}) == FIRST_THRESHOLD_REMINDER


def test_the_validation_rejects_an_empty_threshold_list() -> None:
    with pytest.raises(RepeatToolReminderError):
        RepeatToolReminderConfig(thresholds=()).validate()


def test_the_registry_keeps_one_tracker_per_conversation() -> None:
    registry = RepeatToolReminderRegistry()

    first = registry.get("conversation-a")
    second = registry.get("conversation-a")

    assert first is second
    assert registry.get("conversation-b") is not first


def test_the_registry_keeps_the_configuration_a_tracker_was_created_with() -> None:
    registry = RepeatToolReminderRegistry()

    registry.get("conversation-a", RepeatToolReminderConfig(thresholds=(2,)))
    tracker = registry.get("conversation-a", RepeatToolReminderConfig(thresholds=(9,)))

    assert tracker.config.thresholds == (2,)


def test_the_registry_drops_a_tracker() -> None:
    registry = RepeatToolReminderRegistry()

    registry.get("conversation-a")
    registry.drop("conversation-a")

    assert registry.get("conversation-a").chain_for("conversation-a") is None


def test_the_registry_clears_every_tracker() -> None:
    registry = RepeatToolReminderRegistry()

    registry.get("conversation-a")
    registry.get("conversation-b")
    registry.clear()

    assert registry.get("conversation-a").chain_for("conversation-a") is None


def test_the_process_registry_is_shared() -> None:
    assert repeat_tool_reminder_registry() is repeat_tool_reminder_registry()


def test_the_canonical_form_sorts_the_keys() -> None:
    assert canonical_arguments({"b": 1, "a": 2}) == canonical_arguments({"a": 2, "b": 1})


def test_the_canonical_form_renders_unserializable_values_as_text() -> None:
    class _Opaque:
        def __str__(self) -> str:
            return "opaque"

    assert "opaque" in canonical_arguments({"value": _Opaque()})
