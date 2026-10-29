"""Tests for the DeepSpace agent presets and their resolution.

The checks cover the catalogue the API publishes, the tool allowlist each
preset carries, the deployment fallbacks a resolution applies, and the
conversation defaults a new conversation inherits.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.presets import (
    PRESET_IDS,
    PRESET_TOOL_NAMES,
    PRESETS,
    AgentPreset,
    PresetNotFoundError,
    PresetResolution,
    build_custom_preset,
    get_preset,
    list_presets,
    preset_defaults,
    resolve_preset,
    tool_allowed,
)

pytestmark = pytest.mark.unit_no_db

#: The read-only tool names the checks use when the native tool registry
#: cannot be imported.
FALLBACK_READ_TOOLS: tuple[str, ...] = (
    "analyze",
    "discover_tools",
    "find",
    "fs_glob",
    "fs_grep",
    "fs_read",
    "get_tool_result",
    "goal_read",
    "image_read",
    "job_list",
    "job_status",
    "list_agents",
    "plan_read",
    "read",
    "skill_list",
    "skill_load",
    "spill_read",
    "url_read",
    "web_fetch",
    "web_search",
)

#: The write tool names the checks use when the native tool registry cannot
#: be imported.
FALLBACK_WRITE_TOOLS: tuple[str, ...] = (
    "artifact_create",
    "bash",
    "delete",
    "edit",
    "extension_write",
    "fs_delete",
    "fs_edit",
    "fs_str_replace",
    "fs_write",
    "goal_set",
    "interrupt_agent",
    "job_kill",
    "plan_submit",
    "pwsh",
    "ralph_run",
    "run_code",
    "send_message",
    "subagent",
    "subagent_fork",
    "todo_write",
    "workflow_run",
    "write",
)


def _tool_modes() -> dict[str, str] | None:
    """Return the native tool modes, or None when the package is absent."""
    try:
        from app.deepspace.services.tools import NATIVE_TOOL_MODES
    except ImportError:
        return None
    return {str(name): str(mode) for name, mode in NATIVE_TOOL_MODES.items()}


def _read_tool_names() -> set[str]:
    """Return the read-only tool names the checks use."""
    modes = _tool_modes()
    if modes is None:
        return set(FALLBACK_READ_TOOLS)
    return {name for name, mode in modes.items() if mode == "read"} | {
        "analyze",
        "discover_tools",
        "find",
        "get_tool_result",
        "image_read",
        "read",
        "url_read",
        "web_search",
    }


def _write_tool_names() -> set[str]:
    """Return the write tool names the checks use."""
    modes = _tool_modes()
    if modes is None:
        return set(FALLBACK_WRITE_TOOLS)
    return {name for name, mode in modes.items() if mode == "write"} | {
        "artifact_create",
        "delete",
        "edit",
        "todo_write",
        "write",
    }


def test_every_preset_is_published_with_a_name_and_a_description() -> None:
    assert PRESET_IDS == ("standard", "ptc", "minimal", "creator", "custom")
    assert set(PRESETS) == set(PRESET_IDS)
    for preset_id, preset in PRESETS.items():
        assert isinstance(preset, AgentPreset)
        assert preset.id == preset_id
        assert preset.name.strip()
        assert preset.description.strip()
    assert len({preset.id for preset in PRESETS.values()}) == len(PRESETS)


def test_list_presets_returns_the_catalogue_in_order() -> None:
    assert [preset.id for preset in list_presets()] == list(PRESET_IDS)


def test_standard_preset_permits_every_tool() -> None:
    standard = get_preset("standard")
    assert standard.tool_allowlist is None
    assert standard.ptc_enabled is False
    assert standard.subagent_max_depth is None
    assert standard.plan_mode_by_default is False
    assert standard.sandbox_mode is None
    assert standard.reasoning_effort is None
    assert standard.spill_threshold_chars is None
    assert standard.editable is False
    assert tool_allowed("standard", "bash") is True
    assert tool_allowed("standard", "read") is True
    assert tool_allowed("standard", "a_tool_that_does_not_exist") is True
    assert tool_allowed(None, "bash") is True


def test_minimal_preset_permits_only_read_tools() -> None:
    minimal = get_preset("minimal")
    assert minimal.ptc_enabled is False
    assert minimal.subagent_max_depth == 0
    assert minimal.sandbox_mode == "read-only"
    assert minimal.editable is False
    for tool_name in _write_tool_names():
        assert tool_allowed("minimal", tool_name) is False, tool_name
    for tool_name in _read_tool_names():
        assert tool_allowed("minimal", tool_name) is True, tool_name
    assert tool_allowed("minimal", "bash") is False
    assert tool_allowed("minimal", "fs_write") is False
    assert tool_allowed("minimal", "write") is False
    assert tool_allowed("minimal", "read") is True
    assert tool_allowed("minimal", "fs_read") is True
    assert tool_allowed("minimal", "web_search") is True


def test_ptc_preset_enables_programmatic_calling() -> None:
    ptc = get_preset("ptc")
    assert ptc.ptc_enabled is True
    assert ptc.subagent_max_depth == 1
    assert ptc.reasoning_effort == "medium"
    assert ptc.editable is False
    assert tool_allowed("ptc", "run_code") is True
    assert ptc.tool_allowlist is not None
    assert "run_code" in ptc.tool_allowlist


def test_creator_preset_permits_the_content_tools() -> None:
    creator = get_preset("creator")
    assert creator.ptc_enabled is False
    assert creator.subagent_max_depth == 0
    assert creator.editable is False
    for tool_name in ("read", "find", "write", "edit", "delete", "artifact_create"):
        assert tool_allowed("creator", tool_name) is True, tool_name
    assert tool_allowed("creator", "bash") is False
    assert tool_allowed("creator", "pwsh") is False
    assert tool_allowed("creator", "subagent") is False
    assert tool_allowed("creator", "run_code") is False


def test_custom_preset_carries_the_callers_settings() -> None:
    custom = build_custom_preset(
        name="Research",
        description="A preset the researcher configured.",
        tool_allowlist=("read", "web_search"),
        ptc_enabled=True,
        subagent_max_depth=2,
        plan_mode_by_default=True,
        sandbox_mode="read-only",
        reasoning_effort="high",
        spill_threshold_chars=4_000,
    )
    assert custom.id == "custom"
    assert custom.name == "Research"
    assert custom.editable is True
    assert custom.tool_allowlist == ("read", "web_search")
    assert custom.ptc_enabled is True
    assert custom.subagent_max_depth == 2
    assert custom.plan_mode_by_default is True
    assert custom.sandbox_mode == "read-only"
    assert custom.reasoning_effort == "high"
    assert custom.spill_threshold_chars == 4_000
    assert custom.tool_allowlist is not None
    assert "read" in custom.tool_allowlist
    assert "bash" not in custom.tool_allowlist


def test_custom_preset_defaults_to_the_standard_settings() -> None:
    standard = get_preset("standard")
    custom = build_custom_preset()
    assert custom.id == "custom"
    assert custom.editable is True
    assert custom.tool_allowlist == standard.tool_allowlist
    assert custom.ptc_enabled == standard.ptc_enabled
    assert custom.subagent_max_depth == standard.subagent_max_depth
    assert custom.plan_mode_by_default == standard.plan_mode_by_default
    assert custom.sandbox_mode == standard.sandbox_mode
    assert custom.reasoning_effort == standard.reasoning_effort
    assert custom.spill_threshold_chars == standard.spill_threshold_chars


def test_an_unknown_preset_id_is_refused_and_none_is_the_default() -> None:
    assert get_preset(None).id == "standard"
    assert get_preset("").id == "standard"
    with pytest.raises(PresetNotFoundError):
        get_preset("does-not-exist")
    with pytest.raises(PresetNotFoundError):
        resolve_preset("does-not-exist")
    with pytest.raises(PresetNotFoundError):
        tool_allowed("does-not-exist", "read")


def test_resolve_preset_falls_back_to_the_deployment_settings() -> None:
    settings = SimpleNamespace(
        deepspace_subagent_max_depth=2,
        deepspace_sandbox_policy="danger-full-access",
        deepspace_permission_mode="read-only",
        deepspace_reasoning_effort="high",
        deepspace_spill_threshold_chars=9_000,
    )
    resolution = resolve_preset("standard", settings=settings)
    assert isinstance(resolution, PresetResolution)
    assert resolution.preset.id == "standard"
    assert resolution.subagent_max_depth == 2
    assert resolution.sandbox_mode == "danger-full-access"
    assert resolution.reasoning_effort == "high"
    assert resolution.spill_threshold_chars == 9_000
    assert resolution.ptc_enabled is False
    assert resolution.plan_mode_by_default is False
    assert resolution.tool_allowlist is None


def test_resolve_preset_keeps_the_preset_values_over_the_deployment() -> None:
    settings = SimpleNamespace(
        deepspace_subagent_max_depth=2,
        deepspace_permission_mode="workspace-write",
        deepspace_reasoning_effort="low",
        deepspace_spill_threshold_chars=9_000,
    )
    minimal = resolve_preset("minimal", settings=settings)
    assert minimal.subagent_max_depth == 0
    assert minimal.sandbox_mode == "read-only"
    ptc = resolve_preset("ptc", settings=settings)
    assert ptc.reasoning_effort == "medium"
    assert ptc.subagent_max_depth == 1
    assert ptc.ptc_enabled is True


def test_resolve_preset_works_without_settings() -> None:
    resolution = resolve_preset("standard")
    assert resolution.subagent_max_depth >= 0
    assert resolution.sandbox_mode in {
        "read-only",
        "workspace-write",
        "danger-full-access",
    }
    assert resolution.spill_threshold_chars is not None
    assert resolution.reasoning_effort is None


def test_preset_defaults_returns_the_conversation_defaults() -> None:
    defaults = preset_defaults("minimal")
    assert defaults["sandbox_mode"] == "read-only"
    assert defaults["plan_mode_by_default"] is False
    assert defaults["ptc_enabled"] is False
    assert defaults["subagent_max_depth"] == 0
    assert set(defaults) == {
        "sandbox_mode",
        "plan_mode_by_default",
        "reasoning_effort",
        "spill_threshold_chars",
        "ptc_enabled",
        "subagent_max_depth",
    }
    ptc_defaults = preset_defaults("ptc")
    assert ptc_defaults["ptc_enabled"] is True
    assert ptc_defaults["subagent_max_depth"] == 1
    assert ptc_defaults["reasoning_effort"] == "medium"


def test_preset_tool_names_publish_the_catalogue() -> None:
    assert set(PRESET_TOOL_NAMES) == set(PRESET_IDS)
    for names in PRESET_TOOL_NAMES.values():
        assert names
        assert len(names) == len(set(names))
    assert "run_code" in PRESET_TOOL_NAMES["ptc"]
    assert "bash" not in PRESET_TOOL_NAMES["minimal"]
    assert "bash" not in PRESET_TOOL_NAMES["creator"]
    assert "read" in PRESET_TOOL_NAMES["standard"]
    assert len(PRESET_TOOL_NAMES["minimal"]) < len(PRESET_TOOL_NAMES["standard"])


def test_preset_defaults_accept_the_deployment_settings() -> None:
    settings = SimpleNamespace(deepspace_spill_threshold_chars=11_000)
    defaults = preset_defaults("standard", settings=settings)
    assert defaults["spill_threshold_chars"] == 11_000


def test_the_presets_are_frozen() -> None:
    preset: Any = get_preset("standard")
    with pytest.raises(FrozenInstanceError):
        preset.name = "changed"
