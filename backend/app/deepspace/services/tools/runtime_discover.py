"""The runtime discovery tool for DeepSpace.

``runtime_discover`` tells the model what the runtime it works in can do.
It answers with the tools the agent may call and the mode each one runs
under, the plugins the deployment and the conversation's own workspace
hold, the model routes a delegated child may run under, and the extension
manifests installed for the conversation.

The tool only reads.  It never runs a plugin, a command, or a tool, so the
model can look at the runtime before it acts on it.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.extensions import extensions_for
from app.deepspace.services.plugins import conversation_plugin_scope
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.tools.manage import subagent_model_routes

#: The kinds of thing the discovery tool reports.
DISCOVERY_KINDS = ("tools", "plugins", "extensions", "routes")


class RuntimeDiscoverArguments(ToolArguments):
    """Ask what the runtime can do."""

    kind: str | None = Field(default=None, max_length=64)


def _tool_entries(context: ToolContext) -> list[dict[str, Any]]:
    """Return the tools the agent may call and the mode each one runs under.

    The native registry holds the product's own tools and the tools the
    deployment's plugins add.  The tools the conversation's own workspace
    holds join them, because the model may call those too.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per tool, in a stable order.
    """
    from app.deepspace.services.tools import NATIVE_TOOL_MODES

    modes: dict[str, str] = {name: str(mode) for name, mode in NATIVE_TOOL_MODES.items()}
    scope = conversation_plugin_scope(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
    )
    for manifest in scope.workspace:
        for tool in manifest.tools:
            modes[tool.name] = tool.mode
    return [{"name": name, "mode": mode} for name, mode in sorted(modes.items())]


def _plugin_entries(context: ToolContext) -> list[dict[str, Any]]:
    """Return the plugins the deployment and the conversation hold.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per plugin, in a stable order.
    """
    scope = conversation_plugin_scope(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
    )
    return [
        {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "source": manifest.source,
            "tools": [tool.name for tool in manifest.tools],
            "handler_kind": manifest.handler_kind,
        }
        for manifest in scope.known()
    ]


def _extension_entries(context: ToolContext) -> list[dict[str, Any]]:
    """Return the extension manifests installed for the conversation.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per extension, in a stable order.
    """
    loaded = extensions_for(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
    )
    return [
        {
            "name": extension.name,
            "description": extension.description,
            "aliases": [alias.name for alias in extension.aliases],
            "deny_tools": list(extension.denied_tools),
            "approval_tools": list(extension.approval_tools),
        }
        for extension in loaded.extensions
    ]


def _route_entries(context: ToolContext) -> list[dict[str, Any]]:
    """Return the model routes a delegated child may run under.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per route, in a stable order.
    """
    try:
        return subagent_model_routes(context)
    except Exception:  # noqa: BLE001 - the routes are a convenience, not the answer
        return []


async def _runtime_discover(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Report what the runtime can do."""
    requested = str(arguments.get("kind") or "").strip().lower()
    if requested and requested not in DISCOVERY_KINDS:
        return {
            "status": "error",
            "message": (
                f"Unknown kind {requested!r}. The runtime reports: "
                f"{', '.join(DISCOVERY_KINDS)}, or nothing for all of them."
            ),
        }
    result: dict[str, Any] = {"status": "ok", "kind": requested or "all"}
    if not requested or requested == "tools":
        result["tools"] = _tool_entries(context)
    if not requested or requested == "plugins":
        result["plugins"] = _plugin_entries(context)
    if not requested or requested == "extensions":
        result["extensions"] = _extension_entries(context)
    if not requested or requested == "routes":
        result["routes"] = _route_entries(context)
    return result


RUNTIME_DISCOVER_DESCRIPTION = (
    "Report what this runtime can do: the tools you may call and the mode each one "
    "runs under, the plugins this deployment and this conversation hold, the model "
    "routes a delegated child may run under, and the extensions installed for this "
    "conversation. Pass kind 'tools', 'plugins', 'extensions', or 'routes' to read "
    "one part, or leave it out to read all of them. The report is read-only: it "
    "runs nothing."
)

RUNTIME_DISCOVER_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="runtime_discover",
        description=RUNTIME_DISCOVER_DESCRIPTION,
        properties={
            "kind": string_property(
                "What to look for: tools, plugins, extensions, or routes. "
                "Omit it to read all of them.",
                enum=list(DISCOVERY_KINDS),
            )
        },
        arguments_model=RuntimeDiscoverArguments,
        mode="read",
        handler=_runtime_discover,
    )
]

__all__ = [
    "DISCOVERY_KINDS",
    "RUNTIME_DISCOVER_DESCRIPTION",
    "RUNTIME_DISCOVER_TOOLS",
    "RuntimeDiscoverArguments",
]
