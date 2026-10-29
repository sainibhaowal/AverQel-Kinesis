"""DeepSpace's native tool package.

Every tool the agent can call is declared once, in the module that owns its
behaviour, and collected here.  The collection is what the registry reads to
build the provider-facing schemas, what the policy reads to decide whether a
call is allowed, and what the dispatcher reads to run the call.

The three views of a tool name are derived from the same declaration, so a
tool cannot be exposed to the model without a policy mode, and cannot have a
policy mode without a handler.
"""

from __future__ import annotations

from typing import Any

from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolMode,
    define_native_tool,
    object_property,
    tool_definition,
)
from app.deepspace.services.tools.browser_automation import BROWSER_AUTOMATION_TOOLS
from app.deepspace.services.tools.compact import COMPACT_TOOLS
from app.deepspace.services.tools.computer_use import COMPUTER_USE_TOOLS
from app.deepspace.services.tools.delegation import DELEGATION_TOOLS
from app.deepspace.services.tools.dependencies import DEPENDENCY_TOOLS
from app.deepspace.services.tools.diff import DIFF_TOOLS
from app.deepspace.services.tools.extension import EXTENSION_TOOLS
from app.deepspace.services.tools.fs import FS_TOOLS
from app.deepspace.services.tools.fs_search import FS_SEARCH_TOOLS
from app.deepspace.services.tools.goal import GOAL_TOOLS
from app.deepspace.services.tools.hook import HOOK_TOOLS
from app.deepspace.services.tools.image import READ_IMAGE_TOOLS
from app.deepspace.services.tools.inspect import INSPECT_TOOLS
from app.deepspace.services.tools.instructions import INSTRUCTIONS_TOOLS
from app.deepspace.services.tools.job_output import JOB_OUTPUT_TOOLS
from app.deepspace.services.tools.jobs import JOBS_TOOLS
from app.deepspace.services.tools.lsp import LSP_TOOLS
from app.deepspace.services.tools.manage import MANAGE_TOOLS
from app.deepspace.services.tools.mcp_resource import MCP_RESOURCE_TOOLS
from app.deepspace.services.tools.plan import PLAN_TOOLS
from app.deepspace.services.tools.present import PRESENT_TOOLS
from app.deepspace.services.tools.ptc import RUN_CODE_TOOLS
from app.deepspace.services.tools.ralph import RALPH_TOOLS
from app.deepspace.services.tools.references import REFERENCE_TOOLS
from app.deepspace.services.tools.runtime_discover import RUNTIME_DISCOVER_TOOLS
from app.deepspace.services.tools.schedule import SCHEDULE_TOOLS
from app.deepspace.services.tools.session_query import SESSION_QUERY_TOOLS
from app.deepspace.services.tools.shell import SHELL_TOOLS
from app.deepspace.services.tools.skill import SKILL_TOOLS
from app.deepspace.services.tools.spill import SPILL_TOOLS
from app.deepspace.services.tools.ssh import SSH_STREAM_TOOLS, SSH_TOOLS
from app.deepspace.services.tools.ssh_fs import SSH_FS_TOOLS
from app.deepspace.services.tools.ssh_terminal import SSH_TERMINAL_TOOLS
from app.deepspace.services.tools.team import TEAM_TOOLS
from app.deepspace.services.tools.terminal import (
    TERMINAL_JOB_TOOLS,
    TERMINAL_SIGNAL_TOOLS,
    TERMINAL_TOOLS,
)
from app.deepspace.services.tools.type_graph import TYPE_GRAPH_TOOLS
from app.deepspace.services.tools.web import WEB_TOOLS
from app.deepspace.services.tools.workflow import WORKFLOW_TOOLS


def _plugin_tool_specs() -> list[NativeToolSpec]:
    """Return the tools the available plugins offer.

    A plugin that cannot be read is skipped, so a broken plugin never
    stops the product from starting.

    Returns:
        The specs.
    """
    from app.deepspace.services.plugins import plugin_registry, plugin_tool_specs

    try:
        manifests = plugin_registry().refresh()
    except Exception:  # noqa: BLE001 - a broken plugin must not stop the product
        return []
    return list(plugin_tool_specs(tuple(manifest.id for manifest in manifests)))


#: Every native tool this package owns, in a stable order.
NATIVE_TOOL_SPECS: list[NativeToolSpec] = [
    *FS_TOOLS,
    *FS_SEARCH_TOOLS,
    *WEB_TOOLS,
    *SHELL_TOOLS,
    *DELEGATION_TOOLS,
    *JOBS_TOOLS,
    *WORKFLOW_TOOLS,
    *RALPH_TOOLS,
    *GOAL_TOOLS,
    *SKILL_TOOLS,
    *SPILL_TOOLS,
    *RUN_CODE_TOOLS,
    *PLAN_TOOLS,
    *EXTENSION_TOOLS,
    *TERMINAL_TOOLS,
    *DIFF_TOOLS,
    *SSH_TOOLS,
    *HOOK_TOOLS,
    *TYPE_GRAPH_TOOLS,
    *SCHEDULE_TOOLS,
    *JOB_OUTPUT_TOOLS,
    *READ_IMAGE_TOOLS,
    *PRESENT_TOOLS,
    *MANAGE_TOOLS,
    *INSPECT_TOOLS,
    *DEPENDENCY_TOOLS,
    *SESSION_QUERY_TOOLS,
    *TERMINAL_SIGNAL_TOOLS,
    *TERMINAL_JOB_TOOLS,
    *MCP_RESOURCE_TOOLS,
    *TEAM_TOOLS,
    *RUNTIME_DISCOVER_TOOLS,
    *SSH_FS_TOOLS,
    *SSH_TERMINAL_TOOLS,
    *SSH_STREAM_TOOLS,
    *INSTRUCTIONS_TOOLS,
    *LSP_TOOLS,
    *REFERENCE_TOOLS,
    *COMPACT_TOOLS,
    *BROWSER_AUTOMATION_TOOLS,
    *COMPUTER_USE_TOOLS,
]

#: The tools the available plugins offer.
#:
#: A plugin's tool is a native tool for the registry, the policy, and the
#: dispatcher, so it is declared and validated exactly like a built-in one.
#: Whether a conversation may call one is the conversation's own decision,
#: which the runtime policy applies.
PLUGIN_TOOL_SPECS: list[NativeToolSpec] = _plugin_tool_specs()

NATIVE_TOOL_SPECS = [*NATIVE_TOOL_SPECS, *PLUGIN_TOOL_SPECS]

#: The provider-facing definitions of every tool this package owns.
NATIVE_TOOL_DEFINITIONS: list[ToolDefinition] = [spec.definition for spec in NATIVE_TOOL_SPECS]

#: The typed argument model of every tool this package owns.
NATIVE_TOOL_ARGUMENT_MODELS: dict[str, type] = {
    spec.name: spec.arguments_model for spec in NATIVE_TOOL_SPECS
}

#: The execution mode of every tool this package owns.
NATIVE_TOOL_MODES: dict[str, ToolMode] = {spec.name: spec.mode for spec in NATIVE_TOOL_SPECS}

#: The deadline each tool declares for itself, in seconds.  A tool that
#: declares one runs under that budget instead of the deployment's default.
NATIVE_TOOL_DEADLINES: dict[str, float] = {
    spec.name: float(spec.deadline_seconds)
    for spec in NATIVE_TOOL_SPECS
    if spec.deadline_seconds is not None
}

#: The handler of every tool this package owns.
NATIVE_TOOL_HANDLERS: dict[str, ToolHandler] = {
    spec.name: spec.handler for spec in NATIVE_TOOL_SPECS
}

#: The tools a deployment offers only when a setting allows them.
#: The names are still registered, so the registry and the policy stay in
#: step, but the chat runtime keeps them out of the catalog it offers.
SETTINGS_GATED_TOOLS: dict[str, str] = {
    "bash": "deepspace_shell_enabled",
    "pwsh": "deepspace_shell_enabled",
    # The remote runtime: the file, terminal, and streaming tools on a
    # reviewed remote host.
    "ssh_fs_read": "deepspace_ssh_runtime_enabled",
    "ssh_fs_write": "deepspace_ssh_runtime_enabled",
    "ssh_fs_list": "deepspace_ssh_runtime_enabled",
    "ssh_fs_delete": "deepspace_ssh_runtime_enabled",
    "ssh_terminal_open": "deepspace_ssh_runtime_enabled",
    "ssh_terminal_read": "deepspace_ssh_runtime_enabled",
    "ssh_terminal_write": "deepspace_ssh_runtime_enabled",
    "ssh_terminal_signal": "deepspace_ssh_runtime_enabled",
    "ssh_terminal_close": "deepspace_ssh_runtime_enabled",
    "ssh_stream_read": "deepspace_ssh_runtime_enabled",
    "ssh_stream_close": "deepspace_ssh_runtime_enabled",
}


def unavailable_tool_names(settings: Any) -> set[str]:
    """List the tools this deployment does not offer.

    Args:
        settings: The deployment settings in force.

    Returns:
        The names of the tools that are registered but not offered.
    """
    return {
        name
        for name, setting_name in SETTINGS_GATED_TOOLS.items()
        if not bool(getattr(settings, setting_name, True))
    }


async def dispatch_native_tool(
    name: str, arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any] | None:
    """Run one native tool this package owns.

    Args:
        name: The tool name the model called.
        arguments: The validated arguments.
        context: The calling request's tool context.

    Returns:
        The tool's result, or None when this package does not own the name.
        A None result lets the caller's existing tool chain continue.
    """
    handler = NATIVE_TOOL_HANDLERS.get(name)
    if handler is None:
        return None
    return await handler(arguments, context)


__all__ = [
    "DELEGATION_TOOLS",
    "DIFF_TOOLS",
    "EXTENSION_TOOLS",
    "FS_SEARCH_TOOLS",
    "FS_TOOLS",
    "GOAL_TOOLS",
    "JOBS_TOOLS",
    "NATIVE_TOOL_ARGUMENT_MODELS",
    "NATIVE_TOOL_DEADLINES",
    "NATIVE_TOOL_DEFINITIONS",
    "NATIVE_TOOL_HANDLERS",
    "NATIVE_TOOL_MODES",
    "NATIVE_TOOL_SPECS",
    "DEPENDENCY_TOOLS",
    "INSPECT_TOOLS",
    "INSTRUCTIONS_TOOLS",
    "LSP_TOOLS",
    "REFERENCE_TOOLS",
    "COMPACT_TOOLS",
    "MANAGE_TOOLS",
    "MCP_RESOURCE_TOOLS",
    "PLAN_TOOLS",
    "PRESENT_TOOLS",
    "RALPH_TOOLS",
    "RUN_CODE_TOOLS",
    "SETTINGS_GATED_TOOLS",
    "RUNTIME_DISCOVER_TOOLS",
    "SESSION_QUERY_TOOLS",
    "SHELL_TOOLS",
    "READ_IMAGE_TOOLS",
    "SCHEDULE_TOOLS",
    "SKILL_TOOLS",
    "HOOK_TOOLS",
    "SSH_FS_TOOLS",
    "SSH_STREAM_TOOLS",
    "SSH_TERMINAL_TOOLS",
    "SSH_TOOLS",
    "SPILL_TOOLS",
    "TEAM_TOOLS",
    "TERMINAL_JOB_TOOLS",
    "TERMINAL_SIGNAL_TOOLS",
    "TERMINAL_TOOLS",
    "TYPE_GRAPH_TOOLS",
    "BROWSER_AUTOMATION_TOOLS",
    "COMPUTER_USE_TOOLS",
    "NativeToolSpec",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
    "ToolMode",
    "WEB_TOOLS",
    "WORKFLOW_TOOLS",
    "define_native_tool",
    "dispatch_native_tool",
    "tool_definition",
    "unavailable_tool_names",
    "object_property",
]
