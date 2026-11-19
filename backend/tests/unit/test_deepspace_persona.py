"""Tests for deployment and per-preset persona rows.

The checks cover the rendered prefix and suffix, the complete-prompt choice,
the runtime-context switch, and the rule that only a deployment-owned row can
replace the complete system prompt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import Settings
from app.deepspace.services.deployment_persona import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_UNCONFIGURED,
    deployment_persona_section,
    deployment_persona_state,
    resolve_conversation_persona,
)
from app.deepspace.services.presets import (
    PresetSettingsError,
    custom_preset_from_settings,
    preset_persona_row,
    resolve_preset,
)

pytestmark = pytest.mark.unit_no_db


def _settings(**values: Any) -> Settings:
    """Build deployment settings with a deterministic persona configuration."""
    return Settings(**values)


def test_a_disabled_persona_reports_disabled() -> None:
    state = deployment_persona_state(_settings())

    assert state["status"] == STATUS_DISABLED
    assert state["configured"] is False
    assert deployment_persona_section(_settings()) is None


def test_an_enabled_but_unwritten_persona_reports_unconfigured() -> None:
    settings = _settings(deepspace_deployment_persona_enabled=True)

    assert deployment_persona_state(settings)["status"] == STATUS_UNCONFIGURED
    assert deployment_persona_section(settings) is None


def test_the_deployment_prefix_and_suffix_are_rendered() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_title="Acme Engineering",
        deepspace_deployment_persona_instructions="Answer for Acme's backend engineers.",
        deepspace_deployment_persona_suffix="Close with the next step.",
    )

    persona = resolve_conversation_persona(settings)

    assert deployment_persona_state(settings)["status"] == STATUS_ACTIVE
    assert persona.source == "deployment"
    assert persona.prefix is not None
    assert "Acme Engineering" in persona.prefix
    assert "Acme's backend engineers" in persona.prefix
    assert "policy" in persona.prefix
    assert persona.suffix is not None
    assert "Close with the next step" in persona.suffix
    assert persona.complete is False
    assert persona.include_runtime_context is True


def test_a_complete_deployment_persona_is_the_policy_text() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_instructions="You are Acme's assistant.",
        deepspace_deployment_persona_suffix="Close with the next step.",
        deepspace_deployment_persona_complete=True,
    )

    persona = resolve_conversation_persona(settings)

    assert persona.complete is True
    assert persona.prefix == "You are Acme's assistant."
    assert persona.suffix is not None
    assert "Close with the next step" in persona.suffix
    assert deployment_persona_section(settings) == "You are Acme's assistant."


def test_a_deployment_persona_can_disable_runtime_context() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_instructions="Answer for the operator.",
        deepspace_deployment_persona_include_runtime_context=False,
    )

    persona = resolve_conversation_persona(settings)

    assert persona.include_runtime_context is False


def test_a_preset_persona_shadows_the_deployment_persona() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_instructions="Deployment persona text.",
        deepspace_deployment_persona_suffix="Deployment suffix.",
        deepspace_preset_personas=json.dumps(
            {
                "ptc": {
                    "prefix": "You program against host tools.",
                    "suffix": "",
                }
            }
        ),
    )
    resolution = resolve_preset("ptc", settings=settings)

    persona = resolve_conversation_persona(settings, resolution)

    assert persona.source == "preset"
    assert persona.prefix is not None
    assert "You program against host tools" in persona.prefix
    assert "Deployment persona text" not in persona.prefix
    # An empty preset suffix shadows the deployment suffix.
    assert persona.suffix is None


def test_a_preset_persona_row_is_keyed_by_preset_id() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_instructions="Deployment persona text.",
        deepspace_preset_personas=json.dumps({"ptc": {"prefix": "Only the PTC preset."}}),
    )
    resolution = resolve_preset("standard", settings=settings)

    persona = resolve_conversation_persona(settings, resolution)

    assert persona.source == "deployment"
    assert persona.prefix is not None
    assert "Deployment persona text" in persona.prefix
    assert preset_persona_row(settings, "ptc") == {"prefix": "Only the PTC preset."}


def test_malformed_preset_persona_json_is_ignored() -> None:
    settings = _settings(deepspace_preset_personas="{not json")
    resolution = resolve_preset("standard", settings=settings)

    persona = resolve_conversation_persona(settings, resolution)

    assert preset_persona_row(settings, "standard") is None
    assert persona.source == "none"


def test_a_custom_preset_carries_persona_fields() -> None:
    stored = {
        "persona_prefix": "You are the user's custom agent.",
        "persona_suffix": "Prefer short answers.",
        "persona_include_runtime_context": False,
    }
    preset = custom_preset_from_settings(stored)

    assert preset.persona_prefix == "You are the user's custom agent."
    assert preset.persona_suffix == "Prefer short answers."
    assert preset.persona_include_runtime_context is False
    assert preset.persona_complete is False

    settings = _settings()
    resolution = resolve_preset("custom", settings=settings, custom_settings=stored)
    persona = resolve_conversation_persona(settings, resolution)

    assert persona.source == "preset"
    assert persona.include_runtime_context is False
    assert persona.suffix is not None
    assert "Prefer short answers" in persona.suffix


def test_a_custom_preset_cannot_replace_the_complete_prompt() -> None:
    with pytest.raises(PresetSettingsError, match="complete system prompt"):
        custom_preset_from_settings(
            {
                "persona_prefix": "You are the whole system prompt.",
                "persona_complete": True,
            }
        )


def test_a_custom_preset_persona_must_be_text() -> None:
    with pytest.raises(PresetSettingsError, match="must be text"):
        custom_preset_from_settings({"persona_prefix": 42})


def test_a_deployment_row_can_mark_a_preset_complete() -> None:
    settings = _settings(
        deepspace_preset_personas=json.dumps(
            {
                "standard": {
                    "prefix": "You are the deployment's complete prompt.",
                    "complete": True,
                }
            }
        )
    )
    resolution = resolve_preset("standard", settings=settings)

    persona = resolve_conversation_persona(settings, resolution)

    assert persona.complete is True
    assert persona.prefix == "You are the deployment's complete prompt."
    assert persona.source == "preset"


def test_a_deployment_row_cannot_mark_a_custom_preset_complete() -> None:
    stored = {"persona_prefix": "A user-edited preset."}
    settings = _settings(
        deepspace_preset_personas=json.dumps(
            {"custom": {"prefix": "A user-edited preset.", "complete": True}}
        )
    )
    resolution = resolve_preset("custom", settings=settings, custom_settings=stored)

    persona = resolve_conversation_persona(settings, resolution)

    assert persona.complete is False


def test_an_over_limit_persona_is_not_rendered() -> None:
    settings = _settings(
        deepspace_deployment_persona_enabled=True,
        deepspace_deployment_persona_instructions="x" * 201,
        deepspace_deployment_persona_max_chars=200,
    )

    state = deployment_persona_state(settings)

    assert state["over_limit"] is True
    assert deployment_persona_section(settings) is None
