"""Tests for the DeepSpace settings the runtime reads.

The checks cover the settings that were read with ``getattr`` and never
declared, the settings that were declared and read nowhere, and the values
the readers fall back to.  A declared setting has to be settable from the
environment, has to refuse a value outside its range, and has to carry the
default the reader already used.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.deepspace.services.chat_service import DEFAULT_AGENT_TIMEOUT_SECONDS
from app.deepspace.services.presets import REASONING_EFFORTS, resolve_preset
from app.deepspace.services.runtime_store import DEFAULT_RETAINED_STEPS, DeepSpaceRuntimeStore
from app.deepspace.services.terminals import DEFAULT_MAX_BUFFER_BYTES, _setting_int

pytestmark = pytest.mark.unit_no_db

#: The settings that the runtime read with ``getattr`` and never declared.
UNDECLARED_SETTINGS: tuple[str, ...] = (
    "deepspace_agent_max_runtime_seconds",
    "deepspace_agent_retained_steps",
    "deepspace_terminal_max_output_bytes",
    "deepspace_reasoning_effort",
    "deepspace_default_reasoning_effort",
)

#: The settings that were declared and read nowhere.
UNUSED_SETTINGS: tuple[str, ...] = (
    "deepspace_research_max_queries",
    "deepspace_research_max_candidates",
    "deepspace_research_fetch_count",
)


def _declared_default(name: str) -> object:
    """Return the default the settings class declares for one setting.

    Args:
        name: The setting's name.

    Returns:
        The declared default.
    """
    return Settings.model_fields[name].default


def test_every_setting_the_runtime_reads_is_now_declared() -> None:
    for name in UNDECLARED_SETTINGS:
        assert name in Settings.model_fields, f"{name} is read but never declared"


def test_the_environment_sets_every_setting_the_runtime_reads(monkeypatch) -> None:
    values = {
        "AKS_DEEPSPACE_AGENT_MAX_RUNTIME_SECONDS": "600",
        "AKS_DEEPSPACE_AGENT_RETAINED_STEPS": "2500",
        "AKS_DEEPSPACE_TERMINAL_MAX_OUTPUT_BYTES": "4096",
        "AKS_DEEPSPACE_REASONING_EFFORT": "high",
        "AKS_DEEPSPACE_DEFAULT_REASONING_EFFORT": "low",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.deepspace_agent_max_runtime_seconds == 600
    assert settings.deepspace_agent_retained_steps == 2500
    assert settings.deepspace_terminal_max_output_bytes == 4096
    assert settings.deepspace_reasoning_effort == "high"
    assert settings.deepspace_default_reasoning_effort == "low"


def test_the_agent_runtime_budget_carries_the_readers_fallback() -> None:
    assert _declared_default("deepspace_agent_max_runtime_seconds") == DEFAULT_AGENT_TIMEOUT_SECONDS


def test_the_agent_runtime_budget_refuses_a_value_outside_its_range() -> None:
    with pytest.raises(ValidationError):
        Settings(deepspace_agent_max_runtime_seconds=29)
    with pytest.raises(ValidationError):
        Settings(deepspace_agent_max_runtime_seconds=86_401)
    assert (
        Settings(deepspace_agent_max_runtime_seconds=30).deepspace_agent_max_runtime_seconds == 30
    )
    assert (
        Settings(deepspace_agent_max_runtime_seconds=86_400).deepspace_agent_max_runtime_seconds
        == 86_400
    )


def test_the_retained_steps_setting_carries_the_readers_fallback() -> None:
    assert _declared_default("deepspace_agent_retained_steps") == DEFAULT_RETAINED_STEPS


def test_the_retained_steps_setting_refuses_a_value_outside_its_range() -> None:
    with pytest.raises(ValidationError):
        Settings(deepspace_agent_retained_steps=99)
    with pytest.raises(ValidationError):
        Settings(deepspace_agent_retained_steps=10_001)


def test_the_runtime_store_keeps_the_declared_number_of_steps() -> None:
    settings = Settings(deepspace_agent_retained_steps=250)
    store = DeepSpaceRuntimeStore(
        cast(Session, None),
        retained_steps=int(settings.deepspace_agent_retained_steps),
    )
    assert store.retained_steps == 250


def test_the_terminal_output_cap_carries_the_readers_fallback() -> None:
    assert _declared_default("deepspace_terminal_max_output_bytes") == DEFAULT_MAX_BUFFER_BYTES


def test_the_terminal_output_cap_refuses_a_value_outside_its_range() -> None:
    with pytest.raises(ValidationError):
        Settings(deepspace_terminal_max_output_bytes=1_023)
    with pytest.raises(ValidationError):
        Settings(deepspace_terminal_max_output_bytes=20_000_001)


def test_the_terminal_manager_reads_the_declared_output_cap() -> None:
    settings = Settings(deepspace_terminal_max_output_bytes=4_096)
    assert (
        _setting_int(settings, "deepspace_terminal_max_output_bytes", DEFAULT_MAX_BUFFER_BYTES)
        == 4_096
    )


@pytest.mark.parametrize(
    "name", ["deepspace_reasoning_effort", "deepspace_default_reasoning_effort"]
)
def test_the_reasoning_effort_settings_carry_no_default(name: str) -> None:
    assert _declared_default(name) is None


@pytest.mark.parametrize(
    "name", ["deepspace_reasoning_effort", "deepspace_default_reasoning_effort"]
)
def test_the_reasoning_effort_settings_accept_the_preset_values(name: str) -> None:
    for value in REASONING_EFFORTS:
        assert getattr(Settings(**{name: value}), name) == value


@pytest.mark.parametrize(
    "name", ["deepspace_reasoning_effort", "deepspace_default_reasoning_effort"]
)
def test_the_reasoning_effort_settings_refuse_an_unknown_value(name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{name: "extreme"})


def test_the_declared_reasoning_effort_reaches_the_preset_resolution() -> None:
    named = Settings(deepspace_reasoning_effort="high")
    assert resolve_preset("standard", settings=named).reasoning_effort == "high"
    fallback = Settings(deepspace_default_reasoning_effort="low")
    assert resolve_preset("standard", settings=fallback).reasoning_effort == "low"


def test_the_first_reasoning_effort_setting_wins() -> None:
    settings = Settings(deepspace_reasoning_effort="high", deepspace_default_reasoning_effort="low")
    assert resolve_preset("standard", settings=settings).reasoning_effort == "high"


def test_the_settings_that_were_read_nowhere_are_no_longer_declared() -> None:
    for name in UNUSED_SETTINGS:
        assert name not in Settings.model_fields, f"{name} is declared but read nowhere"
        assert not hasattr(Settings(), name)


def test_the_proactive_daemon_settings_are_declared() -> None:
    assert _declared_default("deepspace_proactive_daemon_enabled") is False
    assert _declared_default("deepspace_proactive_daemon_interval_seconds") == 60


def test_the_proactive_daemon_interval_refuses_a_value_outside_its_range() -> None:
    with pytest.raises(ValidationError):
        Settings(deepspace_proactive_daemon_interval_seconds=0)
    with pytest.raises(ValidationError):
        Settings(deepspace_proactive_daemon_interval_seconds=3_601)
