"""Tests for the goal policy the model reads.

The policy is guidance for the model.  The threshold it names is also the
hard lower bound the goal tool enforces when a goal is marked blocked, so
the model and the runtime never disagree about the same number.
"""

from __future__ import annotations

import pytest

from app.deepspace.services.goal_system_prompt import (
    GOAL_SYSTEM_PROMPT,
    blocked_threshold_from_settings,
    goal_system_prompt,
)

pytestmark = pytest.mark.unit_no_db


class _Settings:
    """A settings object that carries only the fields a test names."""

    def __init__(self, **values: object) -> None:
        for name, value in values.items():
            setattr(self, name, value)


def test_the_policy_names_the_configured_threshold() -> None:
    prompt = goal_system_prompt(5)

    assert "at least 5 consecutive goal rounds" in prompt


def test_the_policy_keeps_its_fixed_rules() -> None:
    prompt = goal_system_prompt(3)

    assert "Mark a goal complete only when its objective is actually achieved" in prompt
    assert "difficulty, uncertainty, or useful remaining work is not blocked" in prompt
    assert "revision" in prompt
    assert "goal_read" in prompt
    assert "goal_set" in prompt


def test_the_policy_names_the_rearm_rule() -> None:
    prompt = goal_system_prompt(3)

    assert "an active goal is disarmed" in prompt
    assert "rearm it" in prompt


def test_the_policy_template_carries_the_threshold_placeholder() -> None:
    assert "{threshold}" in GOAL_SYSTEM_PROMPT


def test_a_threshold_below_one_is_raised_to_one() -> None:
    assert "at least 1 consecutive goal rounds" in goal_system_prompt(0)


def test_a_threshold_of_zero_is_raised_to_one() -> None:
    settings = _Settings(deepspace_goal_blocked_threshold=0)

    assert blocked_threshold_from_settings(settings) == 1


def test_a_negative_threshold_is_raised_to_one() -> None:
    assert "at least 1 consecutive goal rounds" in goal_system_prompt(-5)


def test_the_settings_supply_the_threshold() -> None:
    settings = _Settings(deepspace_goal_blocked_threshold=7)

    assert blocked_threshold_from_settings(settings) == 7


def test_settings_without_the_field_keep_the_default() -> None:
    assert blocked_threshold_from_settings(_Settings()) == 3


def test_a_non_numeric_threshold_keeps_the_default() -> None:
    settings = _Settings(deepspace_goal_blocked_threshold="three")

    assert blocked_threshold_from_settings(settings) == 3


def test_a_numeric_string_threshold_is_read() -> None:
    settings = _Settings(deepspace_goal_blocked_threshold="4")

    assert blocked_threshold_from_settings(settings) == 4
