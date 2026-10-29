"""Management tools for DeepSpace's runtime.

Two tools live here.  ``plugin_manager`` lists the plugins the deployment
ships and switches one on or off for the conversation, and
``list_subagent_models`` reports the provider routes a delegated child may
run under, so a parent agent can name a route that exists.

The dependency report lives in its own module, because it is about the
libraries the deployment ships rather than about the plugins, and is
re-exported here for the convenience of a caller that collects the
management tools in one place.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.plugins import (
    PluginError,
    PluginStateService,
    plugin_registry,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    require_db,
    string_property,
)
from app.deepspace.services.tools.dependencies import LOAD_WORKSPACE_DEPENDENCIES_TOOL


class PluginManagerArguments(ToolArguments):
    """List the plugins, or switch one on or off for the conversation."""

    action: Literal["list", "enable", "disable", "remove"] = "list"
    plugin_id: str | None = Field(default=None, max_length=200)


class ListSubagentModelsArguments(ToolArguments):
    """List the model routes a delegated child may use."""


def _state_service(context: ToolContext) -> PluginStateService:
    """Return the plugin state service for the calling request.

    Args:
        context: The calling request's tool context.

    Returns:
        The service.

    Raises:
        RuntimeError: When the runtime has no database session.
    """
    return PluginStateService(require_db(context))


def conversation_plugins(context: ToolContext) -> tuple[str, ...]:
    """Return the plugins this conversation runs.

    Args:
        context: The calling request's tool context.

    Returns:
        The plugin ids.  The deployment's default set is reported when the
        recorded set cannot be read.
    """
    if not bool(getattr(context.settings, "deepspace_plugins_enabled", True)):
        return ()
    try:
        return _state_service(context).effective_plugins(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        )
    except Exception:  # noqa: BLE001 - the list still answers without the recorded set
        return plugin_registry().enabled_by_default()


def _plugin_entries(context: ToolContext) -> list[dict[str, Any]]:
    """Describe every plugin the deployment ships.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per plugin, with the tools it adds.
    """
    registry = plugin_registry()
    running = set(conversation_plugins(context))
    return [
        {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "source": manifest.source,
            "enabled": manifest.id in running,
            "deployment_enabled": registry.enabled(manifest.id),
            "tools": [tool.name for tool in manifest.tools],
        }
        for manifest in registry.known()
    ]


def _route_models(selection: Any, context: ToolContext, candidate: Any) -> list[str]:
    """Return the chat models one provider route offers.

    Args:
        selection: The provider selection service.
        context: The calling request's tool context.
        candidate: The route's provider candidate.

    Returns:
        The model names, with the route's own model first.
    """
    if candidate.provider_config_id is None:
        return [candidate.model_name] if candidate.model_name else []
    try:
        rows = selection.model_cache.list_models(
            tenant_id=context.auth.tenant_id,
            provider_config_id=candidate.provider_config_id,
            model_kind="chat",
        )
    except Exception:  # noqa: BLE001 - the route still names its own model
        return [candidate.model_name] if candidate.model_name else []
    names = [str(row.model_name) for row in rows]
    if candidate.model_name and candidate.model_name not in names:
        names.insert(0, candidate.model_name)
    return names


def subagent_model_routes(context: ToolContext) -> list[dict[str, Any]]:
    """Return the provider routes a delegated child may run under.

    A child runs under one of the routes the selection service resolves, so
    the routes are read from the same resolution the child uses.

    Args:
        context: The calling request's tool context.

    Returns:
        One entry per route, with the models the route offers.
    """
    selection = context.provider_selection
    if selection is None:
        return []
    resolved = selection.resolve_chat(
        tenant_id=context.auth.tenant_id,
        workspace_id=None,
        actor_user_id=context.auth.user_id,
        allow_live_model_discovery=False,
        record_audit=False,
    )
    return [
        {
            "provider_id": str(candidate.provider_config_id or ""),
            "provider_type": candidate.provider_type,
            "model_name": candidate.model_name,
            "models": _route_models(selection, context, candidate),
        }
        for candidate in resolved.candidates
    ]


async def _plugin_manager(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the plugins, or switch one on or off for this conversation."""
    action = str(arguments.get("action") or "list")
    if action == "list":
        return {"status": "ok", "plugins": _plugin_entries(context)}
    if not bool(getattr(context.settings, "deepspace_plugins_enabled", True)):
        return {"status": "error", "message": "The deployment has disabled plugins."}
    plugin_id = str(arguments.get("plugin_id") or "").strip()
    if not plugin_id:
        return {"status": "error", "message": f"The action {action!r} needs a plugin_id."}
    registry = plugin_registry()
    if registry.get(plugin_id) is None:
        return {
            "status": "error",
            "message": f"No plugin with the id {plugin_id!r} is installed.",
        }
    try:
        service = _state_service(context)
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}
    try:
        enabled = action == "enable"
        if enabled:
            # The deployment offers the plugin again, so the conversation may
            # run it.
            registry.set_enabled(plugin_id, True)
        service.set_plugin_enabled(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            plugin_id=plugin_id,
            enabled=enabled,
        )
        if action == "remove":
            registry.remove(plugin_id)
        return {"status": "ok", "plugin_id": plugin_id, "enabled": enabled}
    except PluginError as exc:
        return {"status": "error", "message": str(exc)}


async def _list_subagent_models(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the model routes a delegated child may use."""
    del arguments
    try:
        routes = subagent_model_routes(context)
    except Exception as exc:  # noqa: BLE001 - a tool reports its failure to the model
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "routes": routes}


PLUGIN_MANAGER_DESCRIPTION = (
    "List the plugins this deployment ships, and switch one on or off for this "
    "conversation. A plugin adds tools, instructions, and tool restrictions to the "
    "agent. Use action 'list' to see the plugins and the ones this conversation runs; "
    "use 'enable' or 'disable' with a plugin_id to switch one on or off here; use "
    "'remove' to drop a plugin from the deployment for good. A plugin takes effect on "
    "the next turn."
)
LIST_SUBAGENT_MODELS_DESCRIPTION = (
    "List the model routes a delegated child agent may run under, with the models each "
    "route offers. Use it to choose the provider and model you give a child when you "
    "start one."
)

MANAGE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="plugin_manager",
        description=PLUGIN_MANAGER_DESCRIPTION,
        properties={
            "action": string_property(
                "What to do: list the plugins, or enable, disable, or remove one.",
                enum=["list", "enable", "disable", "remove"],
            ),
            "plugin_id": string_property(
                "The plugin's id. Required for every action except 'list'.",
                min_length=1,
                max_length=200,
            ),
        },
        required=["action"],
        arguments_model=PluginManagerArguments,
        mode="write",
        handler=_plugin_manager,
    ),
    define_native_tool(
        name="list_subagent_models",
        description=LIST_SUBAGENT_MODELS_DESCRIPTION,
        properties={},
        arguments_model=ListSubagentModelsArguments,
        mode="read",
        handler=_list_subagent_models,
    ),
]

__all__ = [
    "LIST_SUBAGENT_MODELS_DESCRIPTION",
    "LOAD_WORKSPACE_DEPENDENCIES_TOOL",
    "MANAGE_TOOLS",
    "PLUGIN_MANAGER_DESCRIPTION",
    "ListSubagentModelsArguments",
    "PluginManagerArguments",
    "conversation_plugins",
    "subagent_model_routes",
]
