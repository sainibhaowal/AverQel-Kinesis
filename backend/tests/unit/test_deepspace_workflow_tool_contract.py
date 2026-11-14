"""Tests for the model-facing workflow contract.

The workflow tool's description and the guidance the agent reads are the
only places that tell a model how to write a workflow script.  These tests
pin the parts a script depends on, so a later edit cannot quietly remove a
hook the model was told about.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.tools.workflow import (
    WORKFLOW_SYSTEM_GUIDANCE,
    WORKFLOW_TOOLS,
)

pytestmark = pytest.mark.unit_no_db


def _description() -> str:
    """The description the model reads for the workflow tool."""
    return WORKFLOW_TOOLS[0].definition["function"]["description"]


def test_the_tool_description_documents_every_script_hook() -> None:
    """Each hook a script may call is named in the tool description."""
    description = _description()
    for hook in (
        "workflow.agent",
        "workflow.parallel",
        "workflow.pipeline",
        "workflow.phase",
        "workflow.log",
        "workflow.args",
    ):
        assert hook in description, hook


def test_the_tool_description_documents_the_agent_options() -> None:
    """The agent hook's options are named, including the schema option."""
    description = _description()
    for option in (
        "label",
        "phase",
        "mode",
        "persona",
        "model",
        "provider",
        "tool_allow",
        "tool_deny",
        "schema",
    ):
        assert option in description, option


def test_the_tool_description_names_the_supported_schema_keywords() -> None:
    """The supported schema subset is named so a model can stay inside it."""
    description = _description()
    for keyword in (
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "oneOf",
    ):
        assert keyword in description, keyword


def test_the_tool_description_says_how_the_script_ends() -> None:
    """A model is told that the script ends with a return statement."""
    assert "return <value>" in _description()


def test_the_tool_description_says_when_to_prefer_a_direct_delegation() -> None:
    """The description separates a workflow from one or two delegations."""
    assert "subagent tool directly" in _description()


def test_the_system_guidance_points_at_the_tool_description() -> None:
    """The guidance tells the model to read the description before scripting."""
    assert "tool description" in WORKFLOW_SYSTEM_GUIDANCE
    assert "workflow.phase" in WORKFLOW_SYSTEM_GUIDANCE
    assert "workflow.log" in WORKFLOW_SYSTEM_GUIDANCE


def test_the_guidance_is_offered_when_the_workflow_tool_is_available() -> None:
    """A turn that may call the workflow tool receives the guidance."""
    service = object.__new__(DeepSpaceChatService)
    state = SimpleNamespace(native_tool_names={"workflow_run", "fs_read"})

    assert service._workflow_guidance(state) == WORKFLOW_SYSTEM_GUIDANCE


def test_the_guidance_is_withheld_when_the_workflow_tool_is_absent() -> None:
    """A turn that cannot call the workflow tool receives no guidance."""
    service = object.__new__(DeepSpaceChatService)
    state = SimpleNamespace(native_tool_names={"fs_read"})

    assert service._workflow_guidance(state) is None


def test_the_workflow_tool_is_present_in_the_standard_preset() -> None:
    """The workflow tool is one of the tools a standard conversation gets."""
    from app.deepspace.services.presets import PRESET_TOOL_NAMES

    assert "workflow_run" in PRESET_TOOL_NAMES["standard"]
