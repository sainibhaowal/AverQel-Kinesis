"""Read-only inspection tools for DeepSpace's runtime.

Two tools: one lists the capabilities the runtime offers, the other reads
one of them in detail.  Neither of them changes anything, so the agent can
look at the runtime it works in before it acts.

A capability is one part of the runtime the agent can work with: the plugin
system, the native tool registry, the sandbox backends, the provider
routes, the terminal manager, and the schedule service.  Each one owns the
tools it offers, and each one answers with its settings and its state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

from app.deepspace.services.plugins import builtin_plugin_directory, plugin_registry
from app.deepspace.services.sandbox_backend import sandbox_status
from app.deepspace.services.terminals import terminal_manager
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.tools.manage import conversation_plugins, subagent_model_routes


@dataclass(frozen=True, slots=True)
class Capability:
    """One inspectable capability of the runtime.

    Attributes:
        name: The name the query tool takes.
        kind: What kind of capability it is.
        description: What the capability does, in one sentence.
        tools: The names of the tools the capability owns.
    """

    name: str
    kind: str
    description: str
    tools: tuple[str, ...]


class AverQelInspectListArguments(ToolArguments):
    """List the capabilities the runtime offers."""


class AverQelInspectQueryArguments(ToolArguments):
    """Read one capability of the runtime in detail."""

    capability: str = Field(min_length=1, max_length=64)


def _plugin_tools() -> tuple[str, ...]:
    """Return the names of the tools the installed plugins add."""
    return tuple(tool.name for manifest in plugin_registry().known() for tool in manifest.tools)


def _registry_tools() -> tuple[str, ...]:
    """Return the names of every tool the native registry holds.

    The registry is the chat service's catalogue: the product's own tools
    and the tools the tool package owns.

    Returns:
        The tool names, in a stable order.
    """
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY

    return tuple(sorted(NATIVE_TOOL_REGISTRY.names()))


def _tool_mode(name: str) -> str:
    """Return the execution mode of one tool.

    The policy is the authority: it holds the mode of the product's own
    tools and of the tools the tool package owns.  A tool the policy does
    not know is one a module owns on its own, and that module declares the
    mode.

    Args:
        name: The tool's name.

    Returns:
        The tool's execution mode.
    """
    from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
    from app.deepspace.services.tools.schedule import SCHEDULE_TOOLS

    mode = DeepSpaceToolPolicy.mode(name)
    if mode is not None:
        return str(mode)
    for spec in SCHEDULE_TOOLS:
        if spec.name == name:
            return spec.mode
    for manifest in plugin_registry().known():
        tool = manifest.tool(name)
        if tool is not None:
            return tool.mode
    return "read"


def _capabilities() -> list[Capability]:
    """Return the capabilities the runtime offers.

    Returns:
        The capabilities, in a stable order.
    """
    return [
        Capability(
            name="plugins",
            kind="plugin-system",
            description=(
                "The plugin system: the plugins this deployment ships, the tools they "
                "add, and the instructions they contribute."
            ),
            tools=("plugin_manager", *_plugin_tools()),
        ),
        Capability(
            name="tools",
            kind="tool-registry",
            description=(
                "The native tool registry: every tool the agent may call and the "
                "execution mode each one runs under."
            ),
            tools=_registry_tools(),
        ),
        Capability(
            name="sandbox",
            kind="sandbox",
            description=(
                "The operating-system sandboxes the shell tools run in, and the "
                "backend this host offers."
            ),
            tools=("bash", "pwsh", "sandbox_execute"),
        ),
        Capability(
            name="providers",
            kind="provider-routes",
            description=(
                "The provider routes the agent and the children it delegates to run under."
            ),
            tools=("subagent", "subagent_fork", "list_subagent_models"),
        ),
        Capability(
            name="terminal",
            kind="terminal",
            description=(
                "The persistent terminal sessions the agent opens and keeps between calls."
            ),
            tools=(
                "terminal_open",
                "terminal_read",
                "terminal_write",
                "terminal_close",
                "terminal_list",
            ),
        ),
        Capability(
            name="schedule",
            kind="schedule",
            description=("The recurring follow-ups the agent schedules for a conversation."),
            tools=(
                "schedule_create",
                "schedule_list",
                "schedule_update",
                "schedule_delete",
            ),
        ),
    ]


def _plugin_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the plugin system."""
    registry = plugin_registry()
    running = set(conversation_plugins(context))
    settings = {
        "enabled": bool(getattr(context.settings, "deepspace_plugins_enabled", True)),
        "directory": str(getattr(context.settings, "deepspace_plugin_directory", "") or ""),
        "builtin_directory": str(builtin_plugin_directory()),
        "enabled_by_default": list(registry.enabled_by_default()),
    }
    state = {
        "plugins": [
            {
                "id": manifest.id,
                "name": manifest.name,
                "enabled": manifest.id in running,
                "deployment_enabled": registry.enabled(manifest.id),
            }
            for manifest in registry.known()
        ],
        "conversation_enabled": sorted(running),
        "errors": registry.errors(),
    }
    return settings, state


def _tool_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the native tool registry."""
    from app.deepspace.services.tools import NATIVE_TOOL_MODES, NATIVE_TOOL_SPECS

    settings = {
        "permission_mode": str(getattr(context.settings, "deepspace_permission_mode", "")),
        "approval_mode": str(getattr(context.settings, "deepspace_approval_mode", "")),
        "max_concurrent_tools": int(getattr(context.settings, "deepspace_max_concurrent_tools", 8)),
    }
    by_mode: dict[str, list[str]] = {"read": [], "write": [], "interactive": []}
    for name, mode in NATIVE_TOOL_MODES.items():
        by_mode.setdefault(str(mode), []).append(name)
    state = {
        "tool_count": len(NATIVE_TOOL_SPECS),
        "modes": {mode: sorted(names) for mode, names in by_mode.items()},
    }
    return settings, state


def _sandbox_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the sandbox backends."""
    settings = {
        "configured_backend": str(getattr(context.settings, "deepspace_sandbox_backend", "auto")),
        "network": bool(getattr(context.settings, "deepspace_sandbox_network", True)),
        "image": str(getattr(context.settings, "deepspace_sandbox_image", "") or ""),
        "required": bool(getattr(context.settings, "deepspace_sandbox_required", False)),
    }
    return settings, dict(sandbox_status(context.settings))


def _provider_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the provider routes."""
    settings = {
        "provider": str(getattr(context.settings, "llm_provider", "")),
        "model": str(getattr(context.settings, "llm_model", "")),
        "base_url": str(getattr(context.settings, "llm_api_base_url", "")),
    }
    routes = subagent_model_routes(context)
    return settings, {"route_count": len(routes), "routes": routes}


def _terminal_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the terminal manager."""
    settings = {
        "enabled": bool(getattr(context.settings, "deepspace_terminal_enabled", True)),
        "max_sessions": int(getattr(context.settings, "deepspace_terminal_max_sessions", 32)),
        "max_sessions_per_conversation": int(
            getattr(context.settings, "deepspace_terminal_max_sessions_per_conversation", 4)
        ),
        "idle_timeout_seconds": float(
            getattr(context.settings, "deepspace_terminal_idle_timeout_seconds", 900.0)
        ),
    }
    sessions = terminal_manager().list(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    state = {
        "live_sessions": len(sessions),
        "sessions": [
            {"id": session.id, "shell": session.shell, "pty": session.pty} for session in sessions
        ],
    }
    return settings, state


def _schedule_detail(context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings and the state of the schedule service."""
    from app.deepspace.services.tools.schedule import (
        DEFAULT_INTERVAL_MINUTES,
        MAX_INTERVAL_MINUTES,
        MIN_INTERVAL_MINUTES,
        ScheduleService,
    )

    settings = {
        "default_interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_interval_minutes": MAX_INTERVAL_MINUTES,
    }
    count = 0
    if context.db is not None:
        try:
            service = ScheduleService(context.db)
            count = len(
                [
                    schedule
                    for schedule in service.list_schedules(context.auth)
                    if schedule.conversation_id == context.conversation_id
                ]
            )
        except Exception:  # noqa: BLE001 - the count is a convenience, not the answer
            count = 0
    return settings, {"schedule_count": count}


#: The detail each capability answers with.
_CAPABILITY_DETAIL: dict[str, Any] = {
    "plugins": _plugin_detail,
    "tools": _tool_detail,
    "sandbox": _sandbox_detail,
    "providers": _provider_detail,
    "terminal": _terminal_detail,
    "schedule": _schedule_detail,
}


async def _averqel_inspect_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the capabilities the runtime offers."""
    del arguments
    del context
    return {
        "status": "ok",
        "capabilities": [
            {
                "name": capability.name,
                "kind": capability.kind,
                "description": capability.description,
                "tools": list(capability.tools),
            }
            for capability in _capabilities()
        ],
    }


async def _averqel_inspect_query(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one capability of the runtime in detail."""
    name = str(arguments.get("capability") or "").strip()
    capabilities = {capability.name: capability for capability in _capabilities()}
    capability = capabilities.get(name)
    if capability is None:
        return {
            "status": "error",
            "message": (
                f"No capability named {name!r}. The runtime offers: "
                f"{', '.join(sorted(capabilities))}."
            ),
        }
    settings, state = _CAPABILITY_DETAIL[capability.name](context)
    return {
        "status": "ok",
        "capability": {
            "name": capability.name,
            "kind": capability.kind,
            "description": capability.description,
            "tools": [{"name": tool, "mode": _tool_mode(tool)} for tool in capability.tools],
            "settings": settings,
            "state": state,
        },
    }


AVERQEL_INSPECT_LIST_DESCRIPTION = (
    "List the capabilities this runtime offers: the plugin system, the native tool "
    "registry, the sandbox backends, the provider routes, the terminal manager, and "
    "the schedule service. Each capability names the tools it owns. Read one of them "
    "with averqel_inspect_query."
)
AVERQEL_INSPECT_QUERY_DESCRIPTION = (
    "Read one capability of this runtime in detail: the tools it owns and the mode "
    "each one runs under, the settings that configure it, and its current state. The "
    "capability is one of the names averqel_inspect_list returns."
)

INSPECT_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="averqel_inspect_list",
        description=AVERQEL_INSPECT_LIST_DESCRIPTION,
        properties={},
        arguments_model=AverQelInspectListArguments,
        mode="read",
        handler=_averqel_inspect_list,
    ),
    define_native_tool(
        name="averqel_inspect_query",
        description=AVERQEL_INSPECT_QUERY_DESCRIPTION,
        properties={
            "capability": string_property(
                "The capability to read, as averqel_inspect_list names it.",
                min_length=1,
                max_length=64,
            )
        },
        required=["capability"],
        arguments_model=AverQelInspectQueryArguments,
        mode="read",
        handler=_averqel_inspect_query,
    ),
]

__all__ = [
    "AVERQEL_INSPECT_LIST_DESCRIPTION",
    "AVERQEL_INSPECT_QUERY_DESCRIPTION",
    "INSPECT_TOOLS",
    "Capability",
    "AverQelInspectListArguments",
    "AverQelInspectQueryArguments",
]
