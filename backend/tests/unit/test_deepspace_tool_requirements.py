from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.deepspace.services.tool_requirements import (
    excludes_mcp_for_prompt,
    is_mcp_catalog_lookup,
    requires_connected_service_action,
    requires_tool_execution,
)

pytestmark = pytest.mark.unit_no_db


@pytest.mark.parametrize(
    "prompt",
    [
        "Find sales-500.csv and logs-simple.csv on my laptop.",
        "Check the NVIDIA GPU status.",
        "Read /home/user/project/README.md and summarize it.",
        "Run the frontend tests.",
        "Search online for the latest provider documentation.",
        "Inspect the tools catalog and report what is available.",
        "What is the current NVIDIA GPU status?",
        "What connected tools are available?",
        "Get the current weather in Berlin.",
        "Use the browser to open the project website.",
        "Check the latest price of the configured stock.",
    ],
)
def test_explicit_live_checks_require_a_tool(prompt: str) -> None:
    assert requires_tool_execution(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain what a GPU is.",
        "What does workspace-write mean?",
        "Write a short poem about computers.",
        "How should I design a safe agentic loop?",
        "What needs fixing in this system?",
    ],
)
def test_explanations_do_not_require_a_tool(prompt: str) -> None:
    assert requires_tool_execution(prompt) is False


def test_empty_prompt_does_not_require_a_tool() -> None:
    assert requires_tool_execution(None) is False
    assert requires_tool_execution("  \n ") is False


def test_mcp_catalog_lookup_uses_the_broker_without_requiring_a_service_action() -> None:
    prompt = "Search the connected MCP tools for a calendar event lookup tool."
    bindings = {"calendar_lookup": SimpleNamespace(server_name="Calendar")}

    assert requires_tool_execution(prompt) is True
    assert is_mcp_catalog_lookup(prompt) is True
    assert requires_connected_service_action(prompt, bindings) is False


def test_explicit_attached_service_action_requires_a_real_mcp_call() -> None:
    bindings = {"gmail_search": SimpleNamespace(server_name="Gmail")}

    assert requires_connected_service_action("Search my Gmail for the latest invoice.", bindings)


def test_natural_inbox_request_routes_to_the_matching_connected_service() -> None:
    bindings = {"gmail_search": SimpleNamespace(server=SimpleNamespace(name="Google Gmail"))}

    assert requires_connected_service_action("Check my inbox for recent invoices.", bindings)


def test_local_gpu_check_does_not_route_through_an_unrelated_mcp_service() -> None:
    bindings = {"gmail_search": SimpleNamespace(server_name="Gmail")}
    prompt = "Check the NVIDIA GPU status."

    assert requires_tool_execution(prompt)
    assert not requires_connected_service_action(prompt, bindings)
    assert excludes_mcp_for_prompt(prompt, bindings)


@pytest.mark.parametrize(
    "prompt",
    [
        "Do not use MCP; check the GPU with bash.",
        "Find this file on my laptop without connected tools.",
        "Run nvidia-smi via the terminal.",
    ],
)
def test_explicit_local_or_mcp_opt_out_suppresses_mcp_tools(prompt: str) -> None:
    bindings = {"gmail_search": SimpleNamespace(server_name="Gmail")}

    assert excludes_mcp_for_prompt(prompt, bindings)


def test_a_named_connected_service_is_not_suppressed_by_local_terms_elsewhere() -> None:
    bindings = {"gmail_search": SimpleNamespace(server_name="Gmail")}

    assert requires_connected_service_action(
        "Search Gmail, then check GPU status locally.", bindings
    )
