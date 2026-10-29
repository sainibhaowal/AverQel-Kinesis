"""Extension tools for DeepSpace.

The agent can install, read, and remove its own extensions.  An extension
adds guidance to the system prompt, registers an alias for a tool, or
tightens the tool policy for the conversation.  The deployment's own policy
always wins, so an extension can narrow what the agent may do but never
widen it.

One extension runs code.  ``plugin_install`` writes a plugin manifest into
the conversation's workspace, and a manifest may name a Python module the
workspace holds or a command the workspace runs.  That is how the agent
extends itself with a tool of its own.  The deployment's own policy still
wins: a deployment that refuses extension code refuses the install.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.extensions import (
    MAX_GUIDANCE_CHARS,
    ExtensionError,
    extensions_for,
    remove_extension,
    write_extension,
)
from app.deepspace.services.plugins import (
    MANIFEST_FILE_NAME,
    WORKSPACE_PLUGIN_DIRECTORY,
    PluginError,
    parse_manifest,
    plugin_code_allowed,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    boolean_property,
    define_native_tool,
    integer_property,
    object_property,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)


class ExtensionListArguments(ToolArguments):
    """List the extensions this conversation runs with."""


class ExtensionWriteArguments(ToolArguments):
    """Install or change one extension."""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1_000)
    guidance: str | None = Field(default=None, max_length=MAX_GUIDANCE_CHARS)
    aliases: dict[str, Any] | None = Field(default=None)
    deny_tools: list[str] | None = Field(default=None, max_length=64)
    approval_tools: list[str] | None = Field(default=None, max_length=64)


class ExtensionRemoveArguments(ToolArguments):
    """Remove one extension."""

    name: str = Field(min_length=1, max_length=64)


class PluginInstallArguments(ToolArguments):
    """Install a plugin into this conversation's workspace."""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1_000)
    guidance: str | None = Field(default=None, max_length=MAX_GUIDANCE_CHARS)
    handler: str | None = Field(default=None, max_length=1_000)
    handler_module: str | None = Field(default=None, max_length=1_024)
    handler_kind: Literal["python", "command"] | None = Field(default=None)
    handler_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=32)
    enabled_by_default: bool | None = Field(default=None)


def _root(context: ToolContext) -> Any:
    """Return the workspace root of the calling conversation."""
    if context.workspace_root is not None:
        return context.workspace_root
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _plugin_name(raw: str) -> str:
    """Return the directory name one plugin is installed under.

    Args:
        raw: The name the model supplied.

    Returns:
        The name.

    Raises:
        ExtensionError: When the name is not a plain directory name.
    """
    name = str(raw or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ExtensionError("A plugin name may not be a path.")
    return name


async def _extension_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the extensions this conversation runs with."""
    loaded = extensions_for(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
    )
    return {
        "status": "ok",
        "extension_count": len(loaded.extensions),
        **loaded.as_dict(),
    }


async def _extension_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Install or change one extension."""
    document: dict[str, Any] = {"name": str(arguments["name"])}
    if arguments.get("description") is not None:
        document["description"] = str(arguments["description"])
    if arguments.get("guidance") is not None:
        document["guidance"] = str(arguments["guidance"])
    if arguments.get("aliases") is not None:
        document["aliases"] = arguments["aliases"]
    if arguments.get("deny_tools") is not None:
        document["deny_tools"] = list(arguments["deny_tools"])
    if arguments.get("approval_tools") is not None:
        document["approval_tools"] = list(arguments["approval_tools"])
    try:
        extension = write_extension(_root(context), document)
    except (ExtensionError, WorkspacePathError) as exc:
        return {"status": "error", "message": str(exc)}
    return {
        "status": "ok",
        "extension": extension.as_dict(),
        "message": (
            "The extension is installed. Its guidance applies from the next turn, and its "
            "tool restrictions apply immediately."
        ),
    }


async def _extension_remove(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Remove one extension."""
    try:
        removed = remove_extension(_root(context), str(arguments["name"]))
    except (ExtensionError, WorkspacePathError) as exc:
        return {"status": "error", "message": str(exc)}
    if not removed:
        return {
            "status": "not_found",
            "name": str(arguments["name"]),
            "message": "No extension with that name is installed in this workspace.",
        }
    return {
        "status": "ok",
        "name": str(arguments["name"]),
        "removed": True,
        "message": "The extension is removed. Its guidance no longer applies.",
    }


async def _plugin_install(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Install a plugin into this conversation's workspace."""
    try:
        name = _plugin_name(str(arguments["name"]))
    except ExtensionError as exc:
        return {"status": "error", "message": str(exc)}
    document: dict[str, Any] = {"id": name, "name": name}
    for field in (
        "description",
        "guidance",
        "handler",
        "handler_module",
        "handler_kind",
        "handler_timeout_seconds",
    ):
        value = arguments.get(field)
        if value is not None:
            document[field] = value
    if arguments.get("tools") is not None:
        document["tools"] = list(arguments["tools"])
    if arguments.get("enabled_by_default") is not None:
        document["enabled_by_default"] = bool(arguments["enabled_by_default"])
    else:
        document["enabled_by_default"] = True
    try:
        manifest = parse_manifest(document, source="workspace")
    except PluginError as exc:
        return {"status": "error", "message": str(exc)}
    runs_code = bool(manifest.handler) or manifest.handler_kind == "command"
    if runs_code and not plugin_code_allowed(manifest, context.settings):
        return {
            "status": "error",
            "message": "This deployment does not run extension code.",
        }
    try:
        directory = resolve_within_root(_root(context), f"{WORKSPACE_PLUGIN_DIRECTORY}/{name}")
    except WorkspacePathError as exc:
        return {"status": "error", "message": str(exc)}
    directory.absolute.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (directory.absolute / MANIFEST_FILE_NAME).write_text(payload, encoding="utf-8")
    if manifest.handler_module:
        message = (
            "The plugin is installed in this workspace and runs from the next turn. "
            "Write the handler module into the workspace before the plugin's tool is "
            "called."
        )
    else:
        message = "The plugin is installed in this workspace and runs from the next turn."
    return {
        "status": "ok",
        "plugin": {
            "id": manifest.id,
            "name": manifest.name,
            "source": manifest.source,
            "handler_kind": manifest.handler_kind,
            "handler_module": manifest.handler_module,
            "tools": [tool.name for tool in manifest.tools],
        },
        "path": str(directory.absolute / MANIFEST_FILE_NAME),
        "message": message,
    }


EXTENSION_LIST_DESCRIPTION = (
    "List the extensions this conversation runs with. An extension adds guidance to your "
    "system prompt, registers an alias for a tool, or restricts which tools you may use."
)
EXTENSION_WRITE_DESCRIPTION = (
    "Install or change one extension for this conversation. Give it a name, optional "
    "guidance that is added to your instructions, optional aliases that map a name you "
    "like onto a tool you already have, and optional tool restrictions. A restriction "
    "can only narrow what you may do; it cannot widen the deployment's own policy."
)
EXTENSION_REMOVE_DESCRIPTION = "Remove one extension from this conversation."
PLUGIN_INSTALL_DESCRIPTION = (
    "Install a plugin into this conversation's workspace, so the plugin's own tools "
    "become tools you may call from the next turn. Give the plugin a name, the tools "
    "it offers with their argument schemas, and optionally a handler: a Python "
    "function written as 'module:function' with handler_module naming the file in "
    "this workspace that holds it, or a command line with handler_kind 'command'. "
    "Write the handler file into the workspace with fs_write first. The deployment's "
    "policy always wins: a deployment that refuses extension code refuses the "
    "install."
)

EXTENSION_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="extension_list",
        description=EXTENSION_LIST_DESCRIPTION,
        properties={},
        arguments_model=ExtensionListArguments,
        mode="read",
        handler=_extension_list,
    ),
    define_native_tool(
        name="extension_write",
        description=EXTENSION_WRITE_DESCRIPTION,
        properties={
            "name": string_property(
                "The extension's name, unique within this workspace.",
                min_length=1,
                max_length=64,
            ),
            "description": string_property("What the extension changes.", max_length=1_000),
            "guidance": string_property(
                "Text added to your instructions while the extension is installed.",
                max_length=MAX_GUIDANCE_CHARS,
            ),
            "aliases": object_property(
                "Aliases that map a name you use onto a tool you already have. Each value "
                "is either a tool name or an object with a 'tool' and optional 'arguments'."
            ),
            "deny_tools": array_property(
                "Tools the extension refuses you from calling.", max_items=64
            ),
            "approval_tools": array_property(
                "Tools the extension always asks the user about.", max_items=64
            ),
        },
        required=["name"],
        arguments_model=ExtensionWriteArguments,
        mode="write",
        handler=_extension_write,
    ),
    define_native_tool(
        name="extension_remove",
        description=EXTENSION_REMOVE_DESCRIPTION,
        properties={"name": string_property("The extension's name.", min_length=1, max_length=64)},
        required=["name"],
        arguments_model=ExtensionRemoveArguments,
        mode="write",
        handler=_extension_remove,
    ),
    define_native_tool(
        name="plugin_install",
        description=PLUGIN_INSTALL_DESCRIPTION,
        properties={
            "name": string_property(
                "The plugin's id, unique within this workspace.",
                min_length=1,
                max_length=64,
            ),
            "description": string_property("What the plugin does.", max_length=1_000),
            "guidance": string_property(
                "Text added to your instructions while the plugin runs.",
                max_length=MAX_GUIDANCE_CHARS,
            ),
            "handler": string_property(
                "The handler, written as 'module:function', or the command line a "
                "command handler runs.",
                min_length=1,
                max_length=1_000,
            ),
            "handler_module": string_property(
                "The workspace file that holds the handler's module, for example 'my_plugin.py'.",
                min_length=1,
                max_length=1_024,
            ),
            "handler_kind": string_property(
                "How the handler runs: 'python' imports the module, 'command' runs the "
                "command line.",
                enum=["python", "command"],
            ),
            "handler_timeout_seconds": integer_property(
                "The longest a command handler may take.", minimum=1, maximum=300
            ),
            "tools": array_property(
                "The tools the plugin offers. Each one names a 'name', a "
                "'description', a 'mode' of read, write or interactive, and the "
                "'parameters' object of its arguments.",
                item_type="object",
                max_items=32,
            ),
            "enabled_by_default": boolean_property(
                "Whether the conversation runs the plugin when nothing else says."
            ),
        },
        required=["name"],
        arguments_model=PluginInstallArguments,
        mode="write",
        handler=_plugin_install,
    ),
]

__all__ = [
    "EXTENSION_TOOLS",
    "PLUGIN_INSTALL_DESCRIPTION",
    "ExtensionListArguments",
    "ExtensionRemoveArguments",
    "ExtensionWriteArguments",
    "PluginInstallArguments",
]
