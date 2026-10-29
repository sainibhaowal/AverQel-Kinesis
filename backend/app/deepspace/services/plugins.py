"""The plugin system for DeepSpace.

A plugin is a unit of agentic behaviour the deployment can add without
changing the product: a set of tools the model may call, guidance the model
reads, and a policy the calls run under.  The product's own behaviour stays
in the product; a plugin never replaces it, it adds to it.

A plugin declares itself in a manifest.  Four sources are read:

* the built-in plugins that ship with the product,
* the deployment's own plugin directory,
* an installed Python package that declares a ``deepspace.plugins`` entry
  point,
* the conversation's own workspace, at
  ``<workspace>/.deepspace/plugins/<name>/deepspace-plugin.json``.

A plugin found in a workspace is offered to that conversation only.  The
other three sources are process-wide, so the registry holds them.

A manifest is data, never code, and it is validated before the product
reads a single field of it.  A plugin that carries behaviour points at a
handler in three ways:

* a Python handler, ``module:function``, imported and called with the same
  arguments a native tool handler receives,
  * a command that speaks JSON-RPC over stdio, which is how a vendor plugin
    written in another language joins the product.  An AverQel plugin uses this
  shape, and
* an executable handler that runs inside the conversation's workspace: a
  Python module the workspace holds, named by ``handler_module``, or a
  command line, named by ``handler_kind`` ``command``.

An executable handler is how the agent extends itself.  The module is
imported from the workspace and never from an installed package, and a
module path that leaves the workspace is refused.  A command handler runs
in the workspace with the reduced environment the plugin system uses, so it
never sees a deployment secret, and it is bound by a timeout.

The deployment's policy always wins.  The setting
``deepspace_plugin_code_enabled`` refuses extension code outright: a
plugin the workspace holds, and a command handler, do not run when the
deployment says so.

Whether a plugin is on is a per-conversation state, recorded in the typed
session log as a ``plugins/enabled`` event, so it survives a restart by
replay and cannot be changed out of band.  The deployment's policy always
wins: a plugin that the deployment disabled cannot be switched on from a
conversation, and a plugin's own deny list always wins over its allow list.
The registry also holds the deployment's own choices, the plugins it
disabled and the plugins it removed, and both survive a reload.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import SessionEventStore
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)

#: The event name for a plugin enablement change.
PLUGINS_ENABLED = "plugins/enabled"

#: The entry point group an installed package declares its plugins in.
PLUGIN_ENTRY_POINT_GROUP = "deepspace.plugins"

#: The manifest file name a plugin directory carries.
MANIFEST_FILE_NAME = "deepspace-plugin.json"

#: The directory under a workspace root that holds the conversation's plugins.
WORKSPACE_PLUGIN_DIRECTORY = ".deepspace/plugins"

#: The tool modes a plugin may declare.
PLUGIN_TOOL_MODES = ("read", "write", "interactive")

#: The kinds of handler a plugin may declare.
PLUGIN_HANDLER_KINDS = ("python", "command")

#: The largest manifest the product reads.
MAX_MANIFEST_BYTES = 256_000

#: The longest a plugin's command may take to answer one call.
PLUGIN_CALL_TIMEOUT_SECONDS = 60.0

#: The longest a command handler may be given, whatever its manifest says.
MAX_PLUGIN_COMMAND_TIMEOUT_SECONDS = 300.0

# These are DeepSpace capabilities that are part of the product runtime. They
# are reported separately from optional plugin manifests so the inventory does
# not imply that sessions, policy, or the event log are user-installed plugins.
CORE_RUNTIME_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    (
        "agent-loop",
        "Agent loop",
        "Runs model rounds, tool calls, policy checks, and turn completion.",
    ),
    (
        "session-log",
        "Session log",
        "Records typed conversation, tool, policy, and runtime events.",
    ),
    (
        "tool-policy",
        "Tool policy",
        "Applies preset, deployment, approval, and per-conversation tool rules.",
    ),
    (
        "sandbox",
        "Sandbox and permissions",
        "Controls filesystem, shell, network, and extension execution access.",
    ),
    (
        "presets",
        "Presets and agent modes",
        "Resolves Standard, PTC, Minimal, Creator, and Custom settings.",
    ),
    (
        "workers",
        "Workers and queue",
        "Runs queued turns, delegated work, schedules, and other background jobs.",
    ),
    (
        "mcp-bridge",
        "MCP bridge",
        "Applies conversation policy while connected MCP servers provide tools and resources.",
    ),
    (
        "plugin-registry",
        "Plugin registry",
        "Discovers, validates, and scopes optional DeepSpace extensions.",
    ),
)


class PluginError(ValueError):
    """A plugin manifest or a plugin call was rejected."""


@dataclass(frozen=True, slots=True)
class PluginTool:
    """One tool a plugin offers the model.

    Attributes:
        name: The tool name the model calls.
        description: What the tool does, in one sentence.
        parameters: The JSON schema of the tool's arguments.
        mode: The execution mode the policy reads: ``read``, ``write`` or
            ``interactive``.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    mode: Literal["read", "write", "interactive"]


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """One plugin's declaration.

    Attributes:
        id: The plugin's unique id.
        name: The plugin's human name.
        version: The plugin's version.
        description: What the plugin does.
        source: Where the plugin came from: ``builtin``, ``deployment``,
            ``package`` or ``workspace``.
        path: The manifest's path, when it has one.
        tools: The tools the plugin offers.
        guidance: The instructions the plugin adds to the system prompt.
        allow_tools: The tool names the plugin permits, empty for no extra
            permission.
        deny_tools: The tool names the plugin refuses, which always win.
        handler: The handler, ``module:function``.  A command handler holds
            the command line instead.
        handler_module: The workspace file that holds the handler's module.
        handler_kind: How the handler runs: ``python``, ``command`` or the
            product-owned ``native`` adapter.
        handler_timeout_seconds: The longest a command handler may take.
        command: The command that answers a tool call over stdio JSON-RPC.
        settings: The plugin's own settings and their defaults.
        enabled_by_default: Whether a conversation runs the plugin when the
            deployment has no opinion.
        native_tool: The native registry tool this manifest represents, when
            this is a generated built-in adapter.
    """

    id: str
    name: str
    version: str
    description: str
    source: Literal["builtin", "deployment", "package", "workspace"]
    path: str | None = None
    tools: tuple[PluginTool, ...] = ()
    guidance: str = ""
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    handler: str | None = None
    handler_module: str | None = None
    handler_kind: Literal["python", "command", "native"] = "python"
    handler_timeout_seconds: float = PLUGIN_CALL_TIMEOUT_SECONDS
    command: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)
    enabled_by_default: bool = False
    native_tool: str | None = None

    def tool(self, name: str) -> PluginTool | None:
        """Return one of the plugin's tools.

        Args:
            name: The tool name.

        Returns:
            The tool, or None when the plugin does not offer it.
        """
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


def _require_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    """Return one manifest field as a mapping.

    Args:
        value: The field's value.
        field_name: The field's name, for the error message.

    Returns:
        The mapping.

    Raises:
        PluginError: When the field is not a mapping.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginError(f"The plugin manifest field {field_name!r} must be an object.")
    return value


def _require_string(value: object, *, field_name: str) -> str:
    """Return one manifest field as a string.

    Args:
        value: The field's value.
        field_name: The field's name, for the error message.

    Returns:
        The string, empty when the field is absent.

    Raises:
        PluginError: When the field is not a string.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PluginError(f"The plugin manifest field {field_name!r} must be a string.")
    return value.strip()


def _require_string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    """Return one manifest field as a list of strings.

    Args:
        value: The field's value.
        field_name: The field's name, for the error message.

    Returns:
        The strings, empty when the field is absent.

    Raises:
        PluginError: When the field is not a list of strings.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginError(f"The plugin manifest field {field_name!r} must be a list of strings.")
    return tuple(item.strip() for item in value if item.strip())


def _workspace_module_path(raw: str) -> str:
    """Normalize the workspace file that holds a plugin's handler.

    The path is relative to the workspace root.  A path that is absolute,
    or that walks out of the workspace, is refused here, before the file is
    ever looked for.

    Args:
        raw: The path the manifest declared.

    Returns:
        The normalized POSIX-style relative path.

    Raises:
        PluginError: When the path leaves the workspace.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise PluginError("A plugin handler_module needs a file name.")
    if text.startswith("/"):
        raise PluginError("A plugin handler_module is a path inside the workspace.")
    segments = [segment for segment in text.split("/") if segment not in {"", "."}]
    if not segments:
        raise PluginError("A plugin handler_module needs a file name.")
    if any(segment == ".." for segment in segments):
        raise PluginError("A plugin handler_module may not leave the workspace.")
    return "/".join(segments)


def _handler_timeout(value: object) -> float:
    """Read the timeout one manifest gives its handler.

    Args:
        value: The manifest's field.

    Returns:
        The timeout in seconds.

    Raises:
        PluginError: When the field is not a usable timeout.
    """
    if value is None:
        return PLUGIN_CALL_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PluginError("The plugin manifest field 'handler_timeout_seconds' must be a number.")
    timeout = float(value)
    if timeout <= 0 or timeout > MAX_PLUGIN_COMMAND_TIMEOUT_SECONDS:
        raise PluginError(
            "The plugin manifest field 'handler_timeout_seconds' must be between 0 and "
            f"{MAX_PLUGIN_COMMAND_TIMEOUT_SECONDS:g} seconds."
        )
    return timeout


def parse_manifest(
    raw: object,
    *,
    source: Literal["builtin", "deployment", "package", "workspace"],
    path: str | None = None,
) -> PluginManifest:
    """Parse and validate one plugin manifest.

    Args:
        raw: The manifest's decoded content.
        source: Where the manifest came from.
        path: The manifest's path, when it has one.

    Returns:
        The validated manifest.

    Raises:
        PluginError: When the manifest is not usable.
    """
    manifest = _require_mapping(raw, field_name="manifest")
    plugin_id = _require_string(manifest.get("id"), field_name="id")
    if not plugin_id:
        raise PluginError("A plugin manifest needs an id.")
    if not all(character.isalnum() or character in "-_." for character in plugin_id):
        raise PluginError(
            "A plugin id may only hold letters, digits, dashes, underscores and dots."
        )
    name = _require_string(manifest.get("name"), field_name="name") or plugin_id

    tools: list[PluginTool] = []
    raw_tools = manifest.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, list):
            raise PluginError("The plugin manifest field 'tools' must be a list.")
        for entry in raw_tools:
            tool_map = _require_mapping(entry, field_name="tools[]")
            tool_name = _require_string(tool_map.get("name"), field_name="tools[].name")
            if not tool_name:
                raise PluginError("A plugin tool needs a name.")
            mode = _require_string(tool_map.get("mode"), field_name="tools[].mode") or "read"
            if mode not in PLUGIN_TOOL_MODES:
                raise PluginError(
                    f"A plugin tool mode must be read, write, or interactive, not {mode!r}."
                )
            parameters = _require_mapping(
                tool_map.get("parameters"), field_name="tools[].parameters"
            )
            tools.append(
                PluginTool(
                    name=tool_name,
                    description=_require_string(
                        tool_map.get("description"), field_name="tools[].description"
                    ),
                    parameters=parameters,
                    mode=mode,  # type: ignore[arg-type]
                )
            )

    command: tuple[str, ...] = ()
    raw_command = manifest.get("command")
    if raw_command is not None:
        if not isinstance(raw_command, list) or not all(
            isinstance(item, str) for item in raw_command
        ):
            raise PluginError("The plugin manifest field 'command' must be a list of strings.")
        command = tuple(item for item in raw_command if item)

    handler = _require_string(manifest.get("handler"), field_name="handler") or None
    handler_module = (
        _require_string(manifest.get("handler_module"), field_name="handler_module") or None
    )
    handler_kind = (
        _require_string(manifest.get("handler_kind"), field_name="handler_kind") or "python"
    )
    if handler_kind not in PLUGIN_HANDLER_KINDS:
        raise PluginError(f"A plugin handler kind must be python or command, not {handler_kind!r}.")
    if handler_module is not None:
        handler_module = _workspace_module_path(handler_module)
        if not handler:
            raise PluginError(
                "A plugin with a handler_module needs a handler, written as 'module:function'."
            )
        module_name, separator, attribute = handler.partition(":")
        if not separator:
            attribute = handler
            module_name = ""
        if not attribute.strip():
            raise PluginError("A plugin handler must name the function it calls.")
        if module_name and module_name != Path(handler_module).stem:
            raise PluginError("A plugin handler_module and its handler must name the same module.")
    if handler_kind == "command" and not handler:
        raise PluginError("A command handler needs the command line it runs.")
    if not handler and not command:
        # A plugin with no behaviour is a valid manifest: it contributes
        # guidance and policy only.
        handler = None

    return PluginManifest(
        id=plugin_id,
        name=name,
        version=_require_string(manifest.get("version"), field_name="version") or "0.0.0",
        description=_require_string(manifest.get("description"), field_name="description"),
        source=source,
        path=path,
        tools=tuple(tools),
        guidance=_require_string(manifest.get("guidance"), field_name="guidance"),
        allow_tools=_require_string_list(manifest.get("allow_tools"), field_name="allow_tools"),
        deny_tools=_require_string_list(manifest.get("deny_tools"), field_name="deny_tools"),
        handler=handler,
        handler_module=handler_module,
        handler_kind=handler_kind,  # type: ignore[arg-type]
        handler_timeout_seconds=_handler_timeout(manifest.get("handler_timeout_seconds")),
        command=command,
        settings=_require_mapping(manifest.get("settings"), field_name="settings"),
        enabled_by_default=bool(manifest.get("enabled_by_default")),
    )


def read_manifest_file(
    path: Path, *, source: Literal["builtin", "deployment", "package", "workspace"]
) -> PluginManifest:
    """Read and validate one manifest file.

    Args:
        path: The manifest's path.
        source: Where the manifest came from.

    Returns:
        The validated manifest.

    Raises:
        PluginError: When the manifest cannot be read or is not usable.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PluginError(f"The plugin manifest {str(path)!r} could not be read.") from exc
    if size > MAX_MANIFEST_BYTES:
        raise PluginError(f"The plugin manifest {str(path)!r} is too large to read.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginError(f"The plugin manifest {str(path)!r} is not valid JSON.") from exc
    return parse_manifest(raw, source=source, path=str(path))


def discover_directory_plugins(
    directory: Path,
    *,
    source: Literal["builtin", "deployment", "package", "workspace"] = "deployment",
) -> list[PluginManifest]:
    """Read every plugin manifest in one directory.

    A directory holds one plugin: the manifest file inside it.  A directory
    whose manifest is broken is skipped, so one bad plugin cannot take the
    others down with it.

    Args:
        directory: The directory to read.
        source: Where the plugins in the directory came from.

    Returns:
        The manifests that were usable.
    """
    if not directory.is_dir():
        return []
    manifests: list[PluginManifest] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILE_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifests.append(read_manifest_file(manifest_path, source=source))
        except PluginError:
            continue
    return manifests


def workspace_plugin_directory(root: Path) -> Path:
    """Return the directory a workspace holds its plugins in.

    Args:
        root: The workspace root.

    Returns:
        The directory path.
    """
    return root / WORKSPACE_PLUGIN_DIRECTORY


def discover_workspace_plugins(root: Path) -> list[PluginManifest]:
    """Read the plugins one workspace holds.

    The workspace is the fourth plugin source, and it belongs to one
    conversation: a plugin found here is offered to that conversation only.
    A plugin whose manifest is broken is skipped, so one bad plugin cannot
    take the conversation down with it.

    Args:
        root: The workspace root.

    Returns:
        The manifests the workspace holds, in a stable order.
    """
    return discover_directory_plugins(workspace_plugin_directory(root), source="workspace")


def discover_package_plugins() -> list[PluginManifest]:
    """Read the manifests of every installed package that declares plugins.

    A package declares its plugins in the ``deepspace.plugins`` entry point
    group.  The entry point's value is the path of a manifest file inside
    the package, relative to the package's directory.

    Returns:
        The manifests that were usable.
    """
    manifests: list[PluginManifest] = []
    try:
        entry_points = importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - a broken environment must not break the product
        return []
    for entry_point in entry_points:
        try:
            target = entry_point.load()
        except Exception:  # noqa: BLE001 - a broken plugin must not break the product
            continue
        if not isinstance(target, str):
            continue
        try:
            manifest_path = Path(target)
            if not manifest_path.is_absolute():
                package = entry_point.dist
                if package is None:
                    continue
                manifest_path = Path(str(package.locate_file(""))) / target
            if not manifest_path.is_file():
                continue
            manifests.append(read_manifest_file(manifest_path, source="package"))
        except (OSError, PluginError):
            continue
    return manifests


def builtin_plugin_directory() -> Path:
    """Return the directory the built-in plugins live in.

    Returns:
        The directory path.
    """
    return Path(__file__).resolve().parent.parent / "plugins" / "builtin"


def deployment_plugin_directory(settings: Any = None) -> Path:
    """Return the deployment's own plugin directory.

    Args:
        settings: The deployment settings, when the caller has them.

    Returns:
        The directory path.
    """
    configured = ""
    if settings is not None:
        configured = str(getattr(settings, "deepspace_plugin_directory", "") or "")
    if configured:
        return Path(configured).expanduser()
    from app.core.averqel_home import get_averqel_home

    return Path(get_averqel_home()) / "deepspace" / "plugins"


def _native_plugin_manifest(
    *,
    tool_name: str,
    definition: Mapping[str, Any],
    mode: str,
) -> PluginManifest:
    """Build the plugin view of one existing DeepSpace tool.

    The provider definition and policy mode come from the same registry that
    validates and dispatches the native call.  This adapter only gives the
    plugin inventory and preset policy a stable record to select; it never
    creates another handler.

    Args:
        tool_name: The native registry name.
        definition: The provider-facing tool definition.
        mode: The native registry execution mode.

    Returns:
        The generated built-in plugin manifest.
    """
    function = definition.get("function")
    function_map = function if isinstance(function, dict) else {}
    description = str(function_map.get("description") or f"Native DeepSpace tool {tool_name}.")
    parameters = function_map.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return PluginManifest(
        id=f"native.{tool_name}",
        name=f"DeepSpace native · {tool_name}",
        version="native",
        description=description,
        source="builtin",
        tools=(
            PluginTool(
                name=tool_name,
                description=description,
                parameters=dict(parameters),
                mode=mode,  # type: ignore[arg-type]
            ),
        ),
        handler_kind="native",
        enabled_by_default=True,
        native_tool=tool_name,
    )


def _native_plugin_manifests(
    *,
    existing_tool_names: set[str],
) -> tuple[PluginManifest, ...]:
    """Read native tools after their owning registries have initialized.

    DeepSpace imports the plugin registry while assembling the native tool
    package.  Importing the package again at that point would observe a
    partially initialized module, so an unavailable registry simply means
    that this pass will be retried on the next registry lookup.

    Args:
        existing_tool_names: Tool names already owned by declared plugins.

    Returns:
        Generated manifests for native tools not declared by another plugin.
    """
    try:
        from app.deepspace.services import tools as native_package
    except ImportError:
        native_specs: tuple[Any, ...] = ()
    else:
        # The package is imported while it is still assembling its list.  The
        # handler and mode maps are the completion markers: before they exist,
        # wait for a later registry lookup instead of publishing a partial
        # catalogue.
        package_ready = all(
            hasattr(native_package, attribute)
            for attribute in (
                "NATIVE_TOOL_SPECS",
                "NATIVE_TOOL_HANDLERS",
                "NATIVE_TOOL_MODES",
            )
        )
        native_specs = tuple(native_package.NATIVE_TOOL_SPECS) if package_ready else ()

    manifests: list[PluginManifest] = []
    generated_names = set(existing_tool_names)
    for spec in native_specs:
        name = str(getattr(spec, "name", "")).strip()
        if not name or name in generated_names:
            continue
        definition = getattr(spec, "definition", None)
        mode = str(getattr(spec, "mode", "")).strip()
        if not isinstance(definition, dict) or mode not in PLUGIN_TOOL_MODES:
            continue
        manifests.append(_native_plugin_manifest(tool_name=name, definition=definition, mode=mode))
        generated_names.add(name)

    # The compatibility registry owns DeepSpace's original content, media,
    # discovery, and legacy tool contracts.  Add those definitions too, but
    # only once the chat service has completed its module initialization.
    try:
        from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY
    except (ImportError, AttributeError):
        compatibility_registry = None
    else:
        compatibility_registry = NATIVE_TOOL_REGISTRY
    if compatibility_registry is not None:
        for name in compatibility_registry.names():
            if name in generated_names:
                continue
            definition = compatibility_registry.emit(name)
            mode = str(compatibility_registry.spec(name).mode)
            if mode not in PLUGIN_TOOL_MODES:
                continue
            manifests.append(
                _native_plugin_manifest(tool_name=name, definition=definition, mode=mode)
            )
            generated_names.add(name)
    return tuple(manifests)


def _manifest_tool_name(tool: object) -> str:
    """Return a tool name from a validated tool or a legacy test fixture."""
    if isinstance(tool, PluginTool):
        return tool.name
    if isinstance(tool, Mapping):
        return str(tool.get("name") or "").strip()
    return ""


class PluginRegistry:
    """The plugins the product can offer.

    The registry is process-wide.  It reads the three plugin sources and
    keeps the manifests that were usable.  A plugin whose id appears twice
    is refused, so a deployment plugin cannot silently replace a built-in
    one.

    The deployment's own decisions live here as well: a plugin the
    deployment disabled is no longer offered, and a plugin the deployment
    removed is forgotten.  Both decisions survive a reload, because a
    reload re-reads the sources and must not undo what the deployment
    chose.
    """

    def __init__(self, *, include_native_adapters: bool = False) -> None:
        """Create an empty registry.

        Args:
            include_native_adapters: Whether this process-wide registry also
                publishes the core native tool adapters.  Isolated registries
                used for plugin discovery and management contain only declared
                optional plugins.
        """
        self._manifests: dict[str, PluginManifest] = {}
        self._errors: list[str] = []
        self._disabled: set[str] = set()
        self._removed: set[str] = set()
        self._include_native_adapters = include_native_adapters

    def _ensure_native_plugins(self) -> None:
        """Add adapters for native tools once their registries are ready."""
        if not self._include_native_adapters:
            return
        existing_tool_names = {
            name
            for manifest in self._manifests.values()
            for tool in manifest.tools
            if (name := _manifest_tool_name(tool))
        }
        declared_tool_names = {
            name
            for manifest in self._manifests.values()
            if manifest.native_tool is None
            for tool in manifest.tools
            if (name := _manifest_tool_name(tool))
        }
        for manifest in _native_plugin_manifests(existing_tool_names=existing_tool_names):
            if (
                manifest.native_tool not in declared_tool_names
                and manifest.id not in self._removed
                and manifest.id not in self._manifests
            ):
                self._manifests[manifest.id] = manifest
        # A re-entrant refresh can discover a declared plugin after the
        # native pass began.  Keep the declared plugin as the single owner of
        # a tool even in that startup order.
        declared_tool_names = {
            name
            for manifest in self._manifests.values()
            if manifest.native_tool is None
            for tool in manifest.tools
            if (name := _manifest_tool_name(tool))
        }
        for plugin_id, manifest in tuple(self._manifests.items()):
            if manifest.native_tool in declared_tool_names:
                del self._manifests[plugin_id]

    def refresh(self, *, settings: Any = None) -> list[PluginManifest]:
        """Read every plugin source again.

        The deployment's decisions survive the refresh: a plugin the
        deployment disabled stays disabled, and a plugin the deployment
        removed stays forgotten.

        Args:
            settings: The deployment settings, when the caller has them.

        Returns:
            The manifests the deployment now offers.
        """
        manifests: list[PluginManifest] = []
        errors: list[str] = []
        for found in (
            discover_directory_plugins(builtin_plugin_directory(), source="builtin"),
            discover_directory_plugins(deployment_plugin_directory(settings), source="deployment"),
            discover_package_plugins(),
        ):
            for manifest in found:
                if manifest.id in {item.id for item in manifests}:
                    errors.append(
                        f"The plugin {manifest.id!r} is declared more than once; "
                        "the first declaration wins."
                    )
                    continue
                manifests.append(manifest)
        self._manifests = {
            manifest.id: manifest for manifest in manifests if manifest.id not in self._removed
        }
        self._errors = errors
        self._ensure_native_plugins()
        return self.all()

    def all(self) -> list[PluginManifest]:
        """Return every plugin the deployment offers.

        A plugin the deployment disabled is not offered, whatever the
        sources declare.

        Returns:
            The manifests, in a stable order.
        """
        self._ensure_native_plugins()
        return [
            manifest for manifest in self._manifests.values() if manifest.id not in self._disabled
        ]

    def known(self) -> list[PluginManifest]:
        """Return every plugin the registry read, offered or not.

        A plugin the deployment disabled stays in this list, so a caller
        can tell a plugin that is switched off from a plugin that does not
        exist.

        Returns:
            The manifests, in a stable order.
        """
        self._ensure_native_plugins()
        return list(self._manifests.values())

    def get(self, plugin_id: str) -> PluginManifest | None:
        """Return one plugin.

        Args:
            plugin_id: The plugin's id.

        Returns:
            The manifest, or None when the plugin is not known.
        """
        self._ensure_native_plugins()
        return self._manifests.get(plugin_id)

    def enabled(self, plugin_id: str) -> bool:
        """Return whether the deployment offers one plugin.

        Args:
            plugin_id: The plugin's id.

        Returns:
            Whether the plugin is known and the deployment offers it.
        """
        self._ensure_native_plugins()
        if plugin_id not in self._manifests:
            return False
        return plugin_id not in self._disabled

    def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        """Mark one plugin enabled or disabled for the deployment.

        A disabled plugin is not offered, so a conversation cannot switch
        it on.  The decision is remembered in the registry and survives a
        reload.

        Args:
            plugin_id: The plugin's id.
            enabled: Whether the deployment offers the plugin.

        Returns:
            Whether the plugin is known to the registry.
        """
        if plugin_id not in self._manifests:
            return False
        if enabled:
            self._disabled.discard(plugin_id)
        else:
            self._disabled.add(plugin_id)
        return True

    def remove(self, plugin_id: str) -> bool:
        """Forget one plugin for the whole deployment.

        The plugin stays forgotten across a reload, because the sources
        still declare it and the deployment chose to drop it.

        Args:
            plugin_id: The plugin's id.

        Returns:
            Whether the plugin was there.
        """
        if plugin_id not in self._manifests:
            return False
        del self._manifests[plugin_id]
        self._disabled.discard(plugin_id)
        self._removed.add(plugin_id)
        return True

    def reload(self, settings: Any = None) -> list[PluginManifest]:
        """Re-read every plugin source, keeping the deployment's decisions.

        Args:
            settings: The deployment settings, when the caller has them.

        Returns:
            The manifests the deployment now offers.
        """
        return self.refresh(settings=settings)

    def errors(self) -> list[str]:
        """Return the problems the last refresh found.

        Returns:
            The messages, empty when every source was clean.
        """
        return list(self._errors)

    def enabled_by_default(self) -> tuple[str, ...]:
        """Return the plugins a conversation runs when nothing else says.

        Returns:
            The plugin ids.
        """
        self._ensure_native_plugins()
        return tuple(
            manifest.id
            for manifest in self._manifests.values()
            if manifest.enabled_by_default and manifest.id not in self._disabled
        )


def plugin_registry() -> PluginRegistry:
    """Return the process-wide plugin registry.

    Returns:
        The registry.
    """
    return _PLUGIN_REGISTRY


_PLUGIN_REGISTRY = PluginRegistry(include_native_adapters=True)


@dataclass(frozen=True, slots=True)
class PluginScope:
    """The plugins one conversation may run.

    The deployment's plugins are process-wide, so they come from the
    registry.  A plugin the conversation's own workspace holds belongs to
    that conversation alone, so the scope holds it itself.

    Attributes:
        registry: The process-wide registry.
        workspace: The plugins the conversation's workspace holds.
    """

    registry: PluginRegistry
    workspace: tuple[PluginManifest, ...] = ()

    def get(self, plugin_id: str) -> PluginManifest | None:
        """Return one plugin.

        Args:
            plugin_id: The plugin's id.

        Returns:
            The manifest, or None when the plugin is not known.
        """
        for manifest in self.workspace:
            if manifest.id == plugin_id:
                return manifest
        return self.registry.get(plugin_id)

    def known(self) -> list[PluginManifest]:
        """Return every plugin the scope holds, offered or not.

        Returns:
            The manifests, in a stable order.
        """
        return [*self.registry.known(), *self.workspace]

    def all(self) -> list[PluginManifest]:
        """Return every plugin the scope offers.

        Returns:
            The manifests, in a stable order.
        """
        return [*self.registry.all(), *self.workspace]

    def enabled_by_default(self) -> tuple[str, ...]:
        """Return the plugins a conversation runs when nothing else says.

        Returns:
            The plugin ids.
        """
        return (
            *self.registry.enabled_by_default(),
            *(manifest.id for manifest in self.workspace if manifest.enabled_by_default),
        )


def conversation_plugin_scope(
    settings: Any, *, tenant_id: Any, user_id: Any, root: Path | None = None
) -> PluginScope:
    """Return the plugins one conversation may run.

    The scope holds the deployment's plugins and the plugins the
    conversation's own workspace holds.  A deployment without a workspace
    still gets the deployment's plugins.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        root: The workspace the workspace plugins are read from, when the
            caller knows it.

    Returns:
        The scope.
    """
    manifests: list[PluginManifest] = []
    try:
        workspace = root if root is not None else workspace_root_for(settings, tenant_id, user_id)
    except Exception:  # noqa: BLE001 - a deployment without a workspace still has plugins
        workspace = None
    if workspace is not None:
        manifests = discover_workspace_plugins(workspace)
    return PluginScope(registry=plugin_registry(), workspace=tuple(manifests))


def resolve_plugin_manifest(
    plugin_id: str, *, context: Any = None, scope: PluginScope | None = None
) -> PluginManifest | None:
    """Find the manifest one plugin id names.

    The deployment's plugins come from the registry.  A plugin the
    conversation's workspace holds is found through the scope, or through
    the workspace the calling request belongs to.

    Args:
        plugin_id: The plugin's id.
        context: The calling request's tool context, when there is one.
        scope: The conversation's plugin scope, when the caller has one.

    Returns:
        The manifest, or None when the plugin is not known.
    """
    if scope is not None:
        found = scope.get(plugin_id)
        if found is not None:
            return found
    found = plugin_registry().get(plugin_id)
    if found is not None:
        return found
    if context is not None:
        settings = getattr(context, "settings", None)
        auth = getattr(context, "auth", None)
        if settings is not None and auth is not None:
            try:
                fresh = conversation_plugin_scope(
                    settings,
                    tenant_id=getattr(auth, "tenant_id", None),
                    user_id=getattr(auth, "user_id", None),
                )
            except Exception:  # noqa: BLE001 - the plugin simply is not known
                fresh = None
            if fresh is not None:
                found = fresh.get(plugin_id)
                if found is not None:
                    return found
    return None


def plugin_code_allowed(manifest: PluginManifest, settings: Any = None) -> bool:
    """Report whether the deployment lets one plugin's code run.

    The deployment's policy always wins.  The setting
    ``deepspace_plugin_code_enabled`` refuses extension code outright: a
    plugin the workspace holds and a command handler do not run when the
    deployment says so.  Code the deployment shipped or installed itself
    stays allowed, because the deployment already trusts it.

    Args:
        manifest: The plugin's manifest.
        settings: The deployment settings in force, when the caller has them.

    Returns:
        Whether the plugin's code may run.
    """
    if settings is None:
        return True
    if bool(getattr(settings, "deepspace_plugin_code_enabled", True)):
        return True
    if manifest.source == "workspace":
        return False
    return manifest.handler_kind != "command"


class PluginStateService:
    """Which plugins one conversation runs.

    The state is recorded in the typed session log as a
    ``plugins/enabled`` event, so it survives a restart by replay and
    cannot be changed out of band.  The deployment's own policy always
    wins: a plugin the deployment disabled cannot be switched on here.
    """

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.events = SessionEventStore(db)

    def recorded_plugins(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> tuple[str, ...] | None:
        """Read the conversation's last recorded plugin set.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded plugin ids, or None when the conversation never
            recorded a set.
        """
        events = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == PLUGINS_ENABLED,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        if not events:
            return None
        payload: object = events[0].payload
        if not isinstance(payload, dict):
            return None
        enabled = payload.get("enabled")
        if not isinstance(enabled, list):
            return None
        return tuple(str(item) for item in enabled if isinstance(item, str))

    def effective_plugins(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        deployment_enabled: tuple[str, ...] | None = None,
        scope: PluginScope | None = None,
    ) -> tuple[str, ...]:
        """Resolve the plugins one call runs with.

        The deployment's policy wins.  A plugin the deployment did not
        enable is never offered, whatever the conversation recorded.  A
        plugin the conversation's own workspace holds is offered to that
        conversation, so the scope carries it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the call belongs to.
            deployment_enabled: The plugins the deployment permits, when the
                deployment has an opinion.
            scope: The conversation's plugin scope, when the caller has one.

        Returns:
            The plugin ids.
        """
        offered = scope.all() if scope is not None else plugin_registry().all()
        permitted = (
            set(deployment_enabled)
            if deployment_enabled is not None
            else {manifest.id for manifest in offered}
        )
        recorded = self.recorded_plugins(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        defaults = (
            scope.enabled_by_default()
            if scope is not None
            else plugin_registry().enabled_by_default()
        )
        if recorded is None:
            return tuple(plugin_id for plugin_id in defaults if plugin_id in permitted)
        return tuple(plugin_id for plugin_id in recorded if plugin_id in permitted)

    def set_plugins(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        enabled: tuple[str, ...],
        scope: PluginScope | None = None,
    ) -> dict[str, Any]:
        """Record the conversation's plugin set.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            enabled: The plugin ids the conversation runs.
            scope: The conversation's plugin scope, when the caller has one.

        Returns:
            The recorded state.
        """
        offered = scope.all() if scope is not None else plugin_registry().all()
        known = {manifest.id for manifest in offered}
        accepted = tuple(plugin_id for plugin_id in enabled if plugin_id in known)
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=PLUGINS_ENABLED,
            payload={"enabled": list(accepted)},
        )
        self.db.commit()
        return {"enabled": list(accepted)}

    def set_plugin_enabled(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        plugin_id: str,
        enabled: bool,
        scope: PluginScope | None = None,
    ) -> dict[str, Any]:
        """Switch one plugin on or off for the conversation.

        The conversation's set is recorded in the typed session log as a
        ``plugins/enabled`` event, so the change survives a restart by
        replay.  The deployment's policy still wins: a plugin the
        deployment does not offer cannot be switched on here.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            plugin_id: The plugin to switch on or off.
            enabled: Whether the conversation runs the plugin.
            scope: The conversation's plugin scope, when the caller has one.

        Returns:
            The recorded state: the plugin, whether it is on, and the
            conversation's plugin set.

        Raises:
            PluginError: When the plugin is not known, or the deployment
                does not offer it and the caller wants it switched on.
        """
        registry = plugin_registry()
        manifest = scope.get(plugin_id) if scope is not None else registry.get(plugin_id)
        if manifest is None:
            raise PluginError(f"No plugin with the id {plugin_id!r} is installed.")
        if enabled and not self._deployment_offers(manifest, scope=scope):
            raise PluginError(f"The deployment does not offer the plugin {plugin_id!r}.")
        recorded = self.recorded_plugins(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        current = (
            recorded
            if recorded is not None
            else self.effective_plugins(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                scope=scope,
            )
        )
        updated = tuple(item for item in current if item != plugin_id)
        if enabled:
            updated = (*updated, plugin_id)
        state = self.set_plugins(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            enabled=updated,
            scope=scope,
        )
        return {
            "plugin_id": plugin_id,
            "enabled": enabled,
            "plugins": list(state["enabled"]),
        }

    @staticmethod
    def _deployment_offers(manifest: PluginManifest, *, scope: PluginScope | None) -> bool:
        """Report whether the deployment offers one plugin.

        A plugin the conversation's workspace holds belongs to that
        conversation, so the deployment's own on/off state does not apply
        to it.

        Args:
            manifest: The plugin's manifest.
            scope: The conversation's plugin scope, when the caller has one.

        Returns:
            Whether the deployment offers the plugin.
        """
        if scope is not None and manifest.source == "workspace":
            return True
        return plugin_registry().enabled(manifest.id)


def plugin_guidance(plugin_ids: tuple[str, ...], *, scope: PluginScope | None = None) -> str:
    """Build the instruction the enabled plugins add to the system prompt.

    Args:
        plugin_ids: The plugins the conversation runs.
        scope: The conversation's plugin scope, when the caller has one.

    Returns:
        The instruction, empty when no plugin adds guidance.
    """
    parts: list[str] = []
    for plugin_id in plugin_ids:
        manifest = scope.get(plugin_id) if scope is not None else plugin_registry().get(plugin_id)
        if manifest is None or not manifest.guidance:
            continue
        parts.append(f"[{manifest.name}]\n{manifest.guidance}")
    if not parts:
        return ""
    return (
        "The following instructions come from the plugins this conversation runs. "
        "Follow them unless they conflict with the instructions above.\n\n" + "\n\n".join(parts)
    )


def plugin_denied_tools(
    plugin_ids: tuple[str, ...], *, scope: PluginScope | None = None
) -> frozenset[str]:
    """Return the tool names the enabled plugins refuse.

    A plugin's deny list always wins over its allow list and over the
    product's own tool list.

    Args:
        plugin_ids: The plugins the conversation runs.
        scope: The conversation's plugin scope, when the caller has one.

    Returns:
        The refused tool names.
    """
    denied: set[str] = set()
    for plugin_id in plugin_ids:
        manifest = scope.get(plugin_id) if scope is not None else plugin_registry().get(plugin_id)
        if manifest is None:
            continue
        denied.update(manifest.deny_tools)
    return frozenset(denied)


def plugin_allowed_tools(
    plugin_ids: tuple[str, ...], *, scope: PluginScope | None = None
) -> frozenset[str]:
    """Return the tool names the enabled plugins permit beyond the product's.

    Args:
        plugin_ids: The plugins the conversation runs.
        scope: The conversation's plugin scope, when the caller has one.

    Returns:
        The permitted tool names.
    """
    allowed: set[str] = set()
    for plugin_id in plugin_ids:
        manifest = scope.get(plugin_id) if scope is not None else plugin_registry().get(plugin_id)
        if manifest is None:
            continue
        allowed.update(manifest.allow_tools)
    return frozenset(allowed)


def load_python_handler(handler: str) -> Any:
    """Import the Python handler a plugin declares.

    Args:
        handler: The handler, ``module:function``.

    Returns:
        The callable.

    Raises:
        PluginError: When the handler cannot be imported.
    """
    module_name, _, attribute = handler.partition(":")
    if not module_name or not attribute:
        raise PluginError("A plugin handler must be written as 'module:function'.")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"The plugin handler {handler!r} could not be imported.") from exc
    target = getattr(module, attribute, None)
    if target is None or not callable(target):
        raise PluginError(f"The plugin handler {handler!r} is not callable.")
    return target


def plugin_environment(root: Path | None = None) -> dict[str, str]:
    """Build the environment a plugin command receives.

    The environment carries the paths a command needs and none of the
    deployment's secrets.  The home directory is the workspace when the
    caller names one, so the extension never inherits the server user's
    home.

    Args:
        root: The workspace the extension runs in, when the caller knows
            it.

    Returns:
        The environment.
    """
    values: dict[str, str] = {}
    for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            values[name] = value
    if root is not None:
        values["HOME"] = str(root)
    values["DEEPSPACE_PLUGIN"] = "1"
    return values


def _workspace_handler_attribute(manifest: PluginManifest) -> str:
    """Return the name of the function one manifest's handler calls.

    A handler that names its module, ``module:function``, names the
    function after the colon.  A handler that names only the function is
    accepted for a workspace module, because the module the file provides
    is already known.

    Args:
        manifest: The plugin's manifest.

    Returns:
        The function's name.

    Raises:
        PluginError: When the handler names no function.
    """
    _, separator, attribute = str(manifest.handler or "").partition(":")
    name = attribute if separator else str(manifest.handler or "")
    name = name.strip()
    if not name:
        raise PluginError(f"The plugin {manifest.id!r} names no function to call.")
    return name


def _import_workspace_module(manifest: PluginManifest, *, path: Path, root: Path) -> ModuleType:
    """Import the module a workspace plugin's handler lives in.

    The module is loaded from the workspace file itself, so an installed
    package with the same name never answers for it.  The workspace root
    is on the import path while the module loads, so the module's own
    imports resolve inside the workspace first.

    Args:
        manifest: The plugin's manifest.
        path: The module's absolute path inside the workspace.
        root: The workspace root.

    Returns:
        The imported module.

    Raises:
        PluginError: When the module cannot be loaded.
    """
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    module_name = f"deepspace_workspace_plugin_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} could not be loaded."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} failed to load: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(root))
    return module


def load_workspace_handler(manifest: PluginManifest, *, root: Path) -> Any:
    """Import the handler a workspace plugin declares.

    The module is resolved inside the workspace, and a path that leaves the
    workspace is refused.

    Args:
        manifest: The plugin's manifest.
        root: The workspace root that bounds the module.

    Returns:
        The callable.

    Raises:
        PluginError: When the handler cannot be imported.
    """
    if not manifest.handler_module:
        raise PluginError(f"The plugin {manifest.id!r} declares no workspace module.")
    try:
        resolved = resolve_within_root(root, manifest.handler_module)
    except WorkspacePathError as exc:
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} leaves the workspace."
        ) from exc
    if not resolved.absolute.is_file():
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} is not in the workspace."
        )
    module = _import_workspace_module(manifest, path=resolved.absolute, root=root)
    attribute = _workspace_handler_attribute(manifest)
    target = getattr(module, attribute, None)
    if target is None or not callable(target):
        raise PluginError(f"The plugin handler {manifest.handler!r} is not callable.")
    return target


def _command_program() -> tuple[str, str]:
    """Return the program that runs one command line.

    Returns:
        The program and the flag that takes a command line.
    """
    if os.name == "nt":
        return (os.environ.get("COMSPEC") or "cmd.exe", "/c")
    shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return (shell, "-c")


def _command_result(stdout: str, stderr: str, exit_code: int) -> dict[str, Any]:
    """Build the result one command handler answers with.

    A command that prints a JSON object answers with that object, so a
    handler can return a structured result.  Any other output is returned
    as text.

    Args:
        stdout: The command's standard output.
        stderr: The command's standard error.
        exit_code: The command's exit code.

    Returns:
        The result.
    """
    text = (stdout or "").strip()
    payload: dict[str, Any] | None = None
    if text:
        try:
            candidate = json.loads(text)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict):
            payload = candidate
    if payload is not None:
        result = dict(payload)
        result.setdefault("status", "ok" if exit_code == 0 else "failed")
        result["exit_code"] = exit_code
        if stderr:
            result["stderr"] = stderr
        return result
    return {
        "status": "ok" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "output": stdout,
        **({"stderr": stderr} if stderr else {}),
    }


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    """Stop a command that ran past its timeout.

    Args:
        process: The running command.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()


#: The program that runs a plugin's Python handler in its own process.
#:
#: The handler is imported inside the cage, from the workspace, and it
#: answers with the JSON result of one call.  The arguments arrive on
#: stdin and the result leaves on stdout, so the handler never shares a
#: process with the server.
_HANDLER_RUNNER = """
import importlib.util
import inspect
import json
import sys

path, attribute = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("deepspace_extension_handler", path)
if spec is None or spec.loader is None:
    raise SystemExit("the handler module could not be loaded")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
target = getattr(module, attribute, None)
if target is None or not callable(target):
    raise SystemExit("the handler is not callable")
class _Context(dict):
    # The call's context: a mapping that also answers to attribute names.
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


payload = json.loads(sys.stdin.read() or "{}")
arguments = payload.get("arguments", payload)
context = _Context(payload.get("context") or {})
try:
    takes = len(inspect.signature(target).parameters)
except (TypeError, ValueError):
    takes = 1
if takes >= 2:
    result = target(arguments, context)
else:
    result = target(arguments)
sys.stdout.write(json.dumps({"result": result}, default=str))
"""


def _effective_mode(context: Any, settings: Any) -> str:
    """Return the containment mode one extension's code runs under.

    The conversation's own mode wins when it has one, so a user can tighten
    one conversation without changing the deployment.  Otherwise the
    deployment's sandbox policy owns it.

    Args:
        context: The calling request's tool context.
        settings: The deployment settings.

    Returns:
        ``read-only``, ``workspace-write``, or ``danger-full-access``.
    """
    conversation_mode = str(getattr(context, "sandbox_mode", "") or "").strip().lower()
    if conversation_mode:
        return conversation_mode
    return str(
        getattr(settings, "deepspace_sandbox_policy", None)
        or getattr(settings, "deepspace_permission_mode", "workspace-write")
    )


def _cage_argv(
    argv: list[str],
    *,
    root: Path,
    settings: Any,
    context: Any = None,
) -> list[str]:
    """Return the argument vector that runs one command inside the cage.

    The cage is the deployment's own: bubblewrap, Landlock, the macOS
    seatbelt profile, or a container.  The conversation's containment mode
    decides how much the cage gives: a read-only conversation gets a
    read-only workspace, and a conversation that asked for full access runs
    without a cage, exactly as its shell commands do.

    A host that offers no cage has no cage, and the caller refuses the call
    rather than running the code unguarded.

    Args:
        argv: The command to run.
        root: The workspace the command runs in.
        settings: The deployment settings.
        context: The calling request's tool context, when there is one.

    Returns:
        The argument vector, or an empty list when the host offers no cage.
    """
    from app.deepspace.services.sandbox_backend import build_sandbox_command

    mode = _effective_mode(context, settings)
    if mode == "danger-full-access":
        return list(argv)
    try:
        command = build_sandbox_command(
            settings,
            argv,
            workspace=root,
            cwd=root,
            read_only=mode == "read-only",
        )
    except Exception:  # noqa: BLE001 - a cage that cannot be built refuses the call
        return []
    if not command.sandboxed:
        return []
    return list(command.argv)


def run_python_handler(
    manifest: PluginManifest,
    *,
    root: Path,
    arguments: dict[str, Any],
    settings: Any = None,
    context: Any = None,
) -> dict[str, Any]:
    """Run a plugin's Python handler in its own process, inside the cage.

    The handler is imported from the workspace by a small runner program
    that lives in the cage, so the handler cannot read the server's
    memory, and the deployment's cage keeps it inside the workspace.  The
    handler's arguments arrive as JSON and its result leaves as JSON, so a
    handler that hangs, crashes or answers with something unreadable fails
    its call instead of the server.

    Args:
        manifest: The plugin's manifest.
        root: The workspace the handler runs in.
        arguments: The tool's arguments.
        settings: The deployment settings, which choose the cage.
        context: The calling request's tool context, when there is one.  The
            handler receives the workspace, the conversation and the tenant
            as plain data, because it no longer shares a process with the
            server.

    Returns:
        The handler's result.

    Raises:
        PluginError: When the handler cannot run, times out, or answers with
            something unreadable.
    """
    if not manifest.handler_module:
        raise PluginError(f"The plugin {manifest.id!r} declares no workspace module.")
    try:
        resolved = resolve_within_root(root, manifest.handler_module)
    except WorkspacePathError as exc:
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} leaves the workspace."
        ) from exc
    if not resolved.absolute.is_file():
        raise PluginError(
            f"The plugin handler file {manifest.handler_module!r} is not in the workspace."
        )
    attribute = _workspace_handler_attribute(manifest)
    # The system interpreter runs the handler, not the deployment's own: the
    # cage does not mount the deployment's directories, and the handler must
    # not be able to import the application's code.  Windows has no such
    # mount, so the deployment's own interpreter runs the handler there.
    interpreter = sys.executable if os.name == "nt" else (shutil.which("python3") or "python3")
    argv = _cage_argv(
        [interpreter, "-c", _HANDLER_RUNNER, str(resolved.absolute), attribute],
        root=root,
        settings=settings,
        context=context,
    )
    if not argv:
        raise PluginError(
            "This host offers no sandbox, so extension code is not run. Install "
            "bubblewrap, or turn extension code off with deepspace_plugin_code_enabled."
        )
    payload = {
        "arguments": arguments,
        "context": {
            "workspace": str(root),
            "conversation_id": str(getattr(context, "conversation_id", "") or ""),
            "tenant_id": str(getattr(getattr(context, "auth", None), "tenant_id", "") or ""),
        },
    }
    try:
        process = subprocess.Popen(  # noqa: S603 - the program is the product's own
            argv,
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=plugin_environment(root),
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise PluginError(f"The plugin {manifest.id!r} could not start: {exc}") from exc
    try:
        stdout, stderr = process.communicate(
            input=json.dumps(payload, default=str),
            timeout=manifest.handler_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        _stop_process_group(process)
        with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
            process.communicate(timeout=5)
        raise PluginError(
            f"The plugin {manifest.id!r} did not finish within "
            f"{manifest.handler_timeout_seconds:g} seconds."
        ) from exc
    if process.returncode != 0:
        raise PluginError(
            f"The plugin {manifest.id!r} failed: {(stderr or '').strip()[:500] or 'no output'}"
        )
    try:
        answer = json.loads((stdout or "").strip() or "{}")
    except ValueError as exc:
        raise PluginError(
            f"The plugin {manifest.id!r} did not answer with a readable result."
        ) from exc
    result = answer.get("result") if isinstance(answer, dict) else None
    if isinstance(result, dict):
        return result
    return {"status": "ok", "result": result}


def run_command_handler(
    manifest: PluginManifest, *, root: Path, settings: Any = None, context: Any = None
) -> dict[str, Any]:
    """Run the command one plugin declares.

    The command runs in the workspace with the reduced environment the
    plugin system uses, so it never sees a deployment secret, and it is
    bound by the timeout the manifest gives it.  The conversation's
    containment mode decides whether the command runs inside the cage.

    Args:
        manifest: The plugin's manifest.
        root: The workspace the command runs in.
        settings: The deployment settings, which choose the cage.
        context: The calling request's tool context, when there is one.

    Returns:
        The command's result.

    Raises:
        PluginError: When the command cannot run or does not finish in
            time.
    """
    command = str(manifest.handler or "").strip()
    if not command:
        raise PluginError(f"The plugin {manifest.id!r} declares no command to run.")
    program, flag = _command_program()
    argv = _cage_argv([program, flag, command], root=root, settings=settings, context=context)
    if not argv:
        raise PluginError(
            "This host offers no sandbox, so extension code is not run. Install "
            "bubblewrap, or turn extension code off with deepspace_plugin_code_enabled."
        )
    try:
        process = subprocess.Popen(  # noqa: S603 - the command is the plugin's own
            argv,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=plugin_environment(root),
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise PluginError(f"The plugin {manifest.id!r} could not run its command: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=manifest.handler_timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _stop_process_group(process)
        with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
            process.communicate(timeout=5)
        raise PluginError(
            f"The plugin {manifest.id!r} did not finish within "
            f"{manifest.handler_timeout_seconds:g} seconds."
        ) from exc
    return _command_result(stdout or "", stderr or "", process.returncode)


class PluginCommandBridge:
    """A plugin that answers tool calls from another language.

    The plugin ships a command that speaks JSON-RPC over stdio.  The
    product sends one request per call and reads one response.  An AverQel
    plugin uses this shape, so a vendor plugin joins the product without
    the product learning its language.

    The command is started on first use and kept for the process's life.
    It runs in the deployment's cage, in the conversation's workspace, so a
    plugin that speaks another language is confined like every other
    extension.  A command that cannot be started, or that answers with a
    malformed response, fails the call with a typed error rather than
    hanging.
    """

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        root: Path | None = None,
        settings: Any = None,
        context: Any = None,
    ) -> None:
        """Create the bridge.

        Args:
            manifest: The plugin's manifest.
            root: The workspace the plugin's command runs in, when the
                caller knows it.
            settings: The deployment settings, which choose the cage.
            context: The calling request's tool context, when there is one.
        """
        self.manifest = manifest
        self._root = root
        self._settings = settings
        self._context = context
        self._process: subprocess.Popen[str] | None = None

    def _start(self) -> subprocess.Popen[str]:
        """Start the plugin's command.

        Returns:
            The running process.

        Raises:
            PluginError: When the command cannot be started.
        """
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.manifest.command:
            raise PluginError(f"The plugin {self.manifest.id!r} declares no command.")
        executable = shutil.which(self.manifest.command[0]) or self.manifest.command[0]
        argv = [executable, *self.manifest.command[1:]]
        if self._root is not None:
            caged = _cage_argv(
                argv, root=self._root, settings=self._settings, context=self._context
            )
            if not caged:
                raise PluginError(
                    "This host offers no sandbox, so extension code is not run. Install "
                    "bubblewrap, or turn extension code off with deepspace_plugin_code_enabled."
                )
            argv = caged
        try:
            self._process = subprocess.Popen(  # noqa: S603 - the command is the plugin's own
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=plugin_environment(self._root),
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise PluginError(
                f"The plugin {self.manifest.id!r} could not be started: {exc}"
            ) from exc
        return self._process

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call in the plugin.

        Args:
            tool_name: The tool the model called.
            arguments: The tool's arguments.

        Returns:
            The plugin's result.

        Raises:
            PluginError: When the plugin cannot answer.
        """
        process = self._start()
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except (OSError, AssertionError) as exc:
            raise PluginError(
                f"The plugin {self.manifest.id!r} did not answer the call: {exc}"
            ) from exc
        if not line:
            raise PluginError(f"The plugin {self.manifest.id!r} closed before answering.")
        try:
            response = json.loads(line)
        except ValueError as exc:
            raise PluginError(
                f"The plugin {self.manifest.id!r} answered with something that is not JSON."
            ) from exc
        if not isinstance(response, dict):
            raise PluginError(f"The plugin {self.manifest.id!r} answered with an invalid response.")
        if "error" in response:
            error = response.get("error")
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or "The plugin reported an error.")
            raise PluginError(message or f"The plugin {self.manifest.id!r} reported an error.")
        result = response.get("result")
        if not isinstance(result, dict):
            raise PluginError(f"The plugin {self.manifest.id!r} answered with an invalid result.")
        return result

    def close(self) -> None:
        """Stop the plugin's command."""
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def __enter__(self) -> PluginCommandBridge:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def plugin_workspace_root(manifest: PluginManifest, context: Any) -> Path:
    """Return the workspace one plugin's code runs in.

    Args:
        manifest: The plugin's manifest.
        context: The calling request's tool context.

    Returns:
        The workspace root.

    Raises:
        PluginError: When the call has no workspace to run in.
    """
    settings = getattr(context, "settings", None)
    auth = getattr(context, "auth", None)
    tenant_id = getattr(auth, "tenant_id", None)
    user_id = getattr(auth, "user_id", None)
    if settings is None or tenant_id is None or user_id is None:
        raise PluginError(f"The plugin {manifest.id!r} needs the conversation's workspace to run.")
    # The extension runs in the workspace the conversation works in, so the
    # code it runs sees the same files the agent's own tools do.
    chosen = getattr(context, "workspace_root", None)
    if chosen is not None:
        return Path(chosen)
    try:
        return workspace_root_for(settings, tenant_id, user_id)
    except Exception as exc:
        raise PluginError(
            f"The plugin {manifest.id!r} could not find the conversation's workspace."
        ) from exc


def call_plugin_tool(
    manifest: PluginManifest,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    context: Any = None,
    bridge: PluginCommandBridge | None = None,
) -> dict[str, Any]:
    """Run one tool call in a plugin.

    A plugin that declares a workspace Python handler runs in **its own
    process**, inside the deployment's cage, with the reduced environment
    and the timeout the manifest gives it; a plugin that declares a command
    over JSON-RPC runs out of process; a plugin that declares a command
    handler runs its command in the conversation's workspace, in the same
    cage.  A handler that takes the tool context receives the workspace, the
    conversation and the tenant instead, because it no longer shares a
    process with the server.

    The deployment's policy always wins: a plugin whose code the
    deployment refuses does not run.

    Args:
        manifest: The plugin's manifest.
        tool_name: The tool the model called.
        arguments: The tool's arguments.
        context: The calling request's tool context, when there is one.
        bridge: The command bridge to reuse, when the plugin has one.

    Returns:
        The plugin's result.

    Raises:
        PluginError: When the plugin cannot answer.
    """
    if manifest.native_tool is not None:
        raise PluginError(
            f"The native tool {manifest.native_tool!r} must run through the DeepSpace runtime."
        )
    settings = getattr(context, "settings", None)
    if not plugin_code_allowed(manifest, settings):
        raise PluginError("This deployment does not run extension code.")
    if manifest.handler_kind == "command":
        root = plugin_workspace_root(manifest, context)
        return run_command_handler(manifest, root=root, settings=settings, context=context)
    if manifest.command:
        if bridge is not None:
            return bridge.call(tool_name, arguments)
        root = plugin_workspace_root(manifest, context)
        with PluginCommandBridge(manifest, root=root, settings=settings, context=context) as opened:
            return opened.call(tool_name, arguments)
    if manifest.handler:
        if manifest.handler_module:
            # A workspace handler runs in its own process, inside the
            # deployment's cage.
            root = plugin_workspace_root(manifest, context)
            return run_python_handler(
                manifest,
                root=root,
                arguments=arguments,
                settings=settings,
                context=context,
            )
        handler = load_python_handler(manifest.handler)
        result = _invoke_handler(handler, arguments, context)
        if isinstance(result, dict):
            return result
        return {"status": "ok", "result": result}
    raise PluginError(f"The plugin {manifest.id!r} declares no way to run a tool.")


def _invoke_handler(handler: Any, arguments: dict[str, Any], context: Any) -> Any:
    """Call a plugin's Python handler with the arguments it accepts.

    Args:
        handler: The handler.
        arguments: The tool's arguments.
        context: The calling request's tool context, when there is one.

    Returns:
        The handler's result.
    """
    import inspect

    try:
        parameters: Mapping[str, Any] = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "context" in parameters or len(parameters) >= 2:
        return handler(arguments, context)
    return handler(arguments)


def plugin_tool_specs(
    plugin_ids: tuple[str, ...], *, scope: PluginScope | None = None
) -> list[Any]:
    """Build the native tool specs the enabled plugins offer.

    The specs join the product's own tool registry, so a plugin's tool is
    declared, validated and dispatched exactly like a native one.  The
    manifest a spec runs is looked up when the call runs, so a plugin the
    conversation's workspace holds answers with its own manifest.

    Args:
        plugin_ids: The plugins the conversation runs.
        scope: The conversation's plugin scope, when the caller has one.

    Returns:
        The specs, in the order the plugins were enabled.
    """
    from app.deepspace.services.tools._base import NativeToolSpec, define_native_tool

    specs: list[NativeToolSpec] = []
    for plugin_id in plugin_ids:
        found = scope.get(plugin_id) if scope is not None else plugin_registry().get(plugin_id)
        if found is None:
            continue
        manifest: PluginManifest = found
        # Native adapters select an existing registry entry.  The native
        # package already contributes its typed validator and real handler;
        # creating a second plugin spec would bypass that execution path.
        if manifest.native_tool is not None:
            continue
        for tool in manifest.tools:
            arguments_model = _plugin_arguments_model(tool)

            async def handler(
                arguments: dict[str, Any],
                context: Any,
                _plugin_id: str = plugin_id,
                _manifest: PluginManifest = manifest,
                _tool_name: str = tool.name,
            ) -> dict[str, Any]:
                resolved = resolve_plugin_manifest(_plugin_id, context=context, scope=scope)
                try:
                    return call_plugin_tool(
                        resolved or _manifest,
                        tool_name=_tool_name,
                        arguments=arguments,
                        context=context,
                    )
                except PluginError as exc:
                    return {"status": "error", "message": str(exc)}

            specs.append(
                define_native_tool(
                    name=tool.name,
                    description=tool.description,
                    properties=dict(tool.parameters.get("properties") or {}),
                    arguments_model=arguments_model,
                    mode=tool.mode,
                    handler=handler,
                    required=tuple(tool.parameters.get("required") or ()),
                )
            )
    return specs


def workspace_plugin_tool_specs(*, settings: Any, tenant_id: Any, user_id: Any) -> list[Any]:
    """Build the tool specs the conversation's own workspace plugins offer.

    A workspace plugin belongs to one conversation, so its tools are not
    part of the process-wide tool registry.  The caller adds these specs
    to the catalogue it offers the model for that conversation.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The specs the workspace's plugins offer.
    """
    scope = conversation_plugin_scope(settings, tenant_id=tenant_id, user_id=user_id)
    return plugin_tool_specs(
        tuple(manifest.id for manifest in scope.workspace),
        scope=scope,
    )


def plugin_tool_handler(tool_name: str, *, context: Any) -> Any:
    """Return the handler of one plugin tool.

    The handler answers for a tool the conversation's own workspace
    plugin offers, so the dispatcher can run a tool that is not part of
    the process-wide tool registry.

    Args:
        tool_name: The tool the model called.
        context: The calling request's tool context.

    Returns:
        The handler, or None when no plugin offers the tool.
    """
    settings = getattr(context, "settings", None)
    auth = getattr(context, "auth", None)
    if settings is None or auth is None:
        return None
    tenant_id = getattr(auth, "tenant_id", None)
    user_id = getattr(auth, "user_id", None)
    scope = conversation_plugin_scope(settings, tenant_id=tenant_id, user_id=user_id)
    specs = plugin_tool_specs(
        tuple(manifest.id for manifest in scope.workspace),
        scope=scope,
    )
    for spec in specs:
        if spec.name == tool_name:
            return spec.handler
    return None


def _plugin_arguments_model(tool: PluginTool) -> type:
    """Build the argument model one plugin tool validates against.

    The model accepts the tool's declared properties and refuses anything
    else, so a plugin's tool has the same argument discipline as a native
    one.

    Args:
        tool: The tool.

    Returns:
        The model class.
    """
    from pydantic import ConfigDict, Field, create_model

    from app.deepspace.services.tool_registry import ToolArguments

    properties = dict(tool.parameters.get("properties") or {})
    required = tuple(tool.parameters.get("required") or ())
    fields: dict[str, Any] = {}
    for name, schema in properties.items():
        if not isinstance(schema, dict):
            schema = {}
        field_type: Any = str
        json_type = str(schema.get("type") or "string")
        if json_type == "integer":
            field_type = int
        elif json_type == "number":
            field_type = float
        elif json_type == "boolean":
            field_type = bool
        elif json_type == "array":
            field_type = list
        elif json_type == "object":
            field_type = dict
        description = str(schema.get("description") or "")
        if name in required:
            fields[name] = (field_type, Field(description=description))
        else:
            fields[name] = (field_type | None, Field(default=None, description=description))
    model = create_model(
        f"{_snake(tool.name)}__arguments",
        __base__=ToolArguments,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model


def _snake(name: str) -> str:
    """Return one tool name in the form a model class may use.

    Args:
        name: The tool name.

    Returns:
        The snake-cased name.
    """
    return "".join(character if character.isalnum() else "_" for character in name).lower()


def _plugin_runtime_entry(
    manifest: PluginManifest,
    *,
    deployment_enabled: bool,
    available: bool | None = None,
) -> dict[str, Any]:
    """Build the bounded, non-path inventory record for one plugin.

    Args:
        manifest: The plugin to describe.
        deployment_enabled: Whether the deployment offers the plugin.
        available: Whether a preset can expose one of its tools, when this is
            a preset-scoped record.

    Returns:
        The inventory record.
    """
    entry: dict[str, Any] = {
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "source": manifest.source,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "mode": tool.mode,
            }
            for tool in manifest.tools
        ],
        "enabled_by_default": manifest.enabled_by_default,
        "deployment_enabled": deployment_enabled,
        "has_behaviour": bool(manifest.native_tool or manifest.handler or manifest.command),
        "handler_kind": manifest.handler_kind,
        "native_tool": manifest.native_tool,
        "has_guidance": bool(manifest.guidance),
        "settings": sorted(manifest.settings),
    }
    if available is not None:
        entry["available"] = available and deployment_enabled
    return entry


def _plugin_available_for_preset(manifest: PluginManifest, preset: Any) -> bool:
    """Report whether a preset can expose one plugin's tools.

    A preset with no allowlist permits the deployment's complete tool
    catalogue.  A guidance-only plugin is visible in every preset; a plugin
    with tools is session-available when at least one of those tools is in the
    preset's allowlist.

    Args:
        manifest: The plugin to inspect.
        preset: The resolved preset.

    Returns:
        Whether the plugin can contribute to a session using the preset.
    """
    allowlist = getattr(preset, "tool_allowlist", None)
    if allowlist is None or not manifest.tools:
        return True
    allowed = set(allowlist)
    return any(tool.name in allowed for tool in manifest.tools)


def plugin_runtime_summary(
    settings: Any = None,
    custom_presets: list[tuple[Any, dict[str, Any]]] | None = None,
    custom_settings: Mapping[str, Any] | None = None,
    default_preset: str | None = None,
) -> dict[str, Any]:
    """Summarize the core runtime, extensions, tools, and preset availability.

    Args:
        settings: The deployment settings, used to resolve the default
            preset and whether the plugin system is enabled.
        custom_presets: Saved user presets and their persisted settings, when
            the caller is an authenticated user-facing inventory route.
        custom_settings: The authenticated user's saved settings for the
            built-in Custom mode, when available.
        default_preset: The authenticated user's selected default, or None
            to use the deployment default.

    Returns:
        The summary a settings route reports.
    """
    registry = plugin_registry()
    manifests = registry.known()
    deployment_plugins_enabled = bool(getattr(settings, "deepspace_plugins_enabled", True))
    global_entries = [
        _plugin_runtime_entry(
            manifest,
            deployment_enabled=registry.enabled(manifest.id) and deployment_plugins_enabled,
        )
        for manifest in manifests
    ]

    # Keep the dependency local: the preset catalogue imports the native tool
    # package, which imports the plugin catalogue during application startup.
    from app.deepspace.services.presets import (
        CUSTOM_PRESET_ID,
        list_presets,
        resolve_preset,
    )

    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or "standard")
    selected_default = str(default_preset or deployment_default)
    session_presets: list[dict[str, Any]] = []
    available_in: dict[str, list[str]] = {manifest.id: [] for manifest in manifests}
    preset_entries: list[tuple[Any, dict[str, Any] | None]] = [
        (
            preset,
            custom_settings if preset.id == CUSTOM_PRESET_ID else None,
        )
        for preset in list_presets()
    ]
    preset_entries.extend(custom_presets or [])
    for preset, custom_settings in preset_entries:
        resolved = resolve_preset(
            preset.id,
            settings=settings,
            custom_settings=custom_settings,
        )
        rows: list[dict[str, Any]] = []
        for manifest in manifests:
            available = _plugin_available_for_preset(manifest, resolved)
            if not available:
                continue
            available_in[manifest.id].append(preset.id)
            rows.append(
                _plugin_runtime_entry(
                    manifest,
                    deployment_enabled=registry.enabled(manifest.id) and deployment_plugins_enabled,
                    available=available,
                )
            )
        preset_tool_names = sorted(
            {
                tool["name"]
                for row in rows
                if row["deployment_enabled"] and row.get("available", False)
                for tool in row["tools"]
            }
        )
        enabled_plugin_count = sum(1 for row in rows if row["deployment_enabled"])
        session_presets.append(
            {
                "id": preset.id,
                "name": preset.name,
                "description": preset.description,
                "is_default": preset.id == selected_default,
                "plugins": rows,
                "plugin_count": len(rows),
                "enabled_plugin_count": enabled_plugin_count,
                "tools": preset_tool_names,
                "tool_count": len(preset_tool_names),
                "ptc_enabled": resolved.ptc_enabled,
                "sandbox_mode": resolved.sandbox_mode,
                "reasoning_effort": resolved.reasoning_effort,
                "subagent_max_depth": resolved.subagent_max_depth,
                "plan_mode_by_default": resolved.plan_mode_by_default,
                "spill_threshold_chars": resolved.spill_threshold_chars,
            }
        )

    for entry, manifest in zip(global_entries, manifests, strict=True):
        entry["available_in_presets"] = available_in[manifest.id]

    from app.deepspace.services.presets import COMPATIBILITY_TOOL_NAMES, native_tool_names

    registered_tool_names = sorted({tool.name for manifest in manifests for tool in manifest.tools})
    native_names = set(native_tool_names())
    compatibility_names = set(COMPATIBILITY_TOOL_NAMES)
    registered_tools = set(registered_tool_names)
    counts = {
        "extensions": len(global_entries),
        "enabled_extensions": sum(1 for entry in global_entries if entry["deployment_enabled"]),
        "tools": len(registered_tool_names),
        "native_tools": len(registered_tools & native_names),
        "compatibility_tools": len(registered_tools & compatibility_names),
        "extension_tools": len(registered_tools - native_names - compatibility_names),
    }

    return {
        # ``plugins`` remains for existing callers; the named planes are the
        # settings UI's stable discovery surface.
        "plugins": global_entries,
        "global_plugins": global_entries,
        "session_presets": session_presets,
        "core_runtime": [
            {"id": component_id, "name": name, "description": description}
            for component_id, name, description in CORE_RUNTIME_COMPONENTS
        ],
        "counts": counts,
        "default_preset": selected_default,
        "plugins_enabled": deployment_plugins_enabled,
        "errors": registry.errors(),
        "python": sys.version_info[:2] >= (3, 11),
    }


__all__ = [
    "MANIFEST_FILE_NAME",
    "MAX_PLUGIN_COMMAND_TIMEOUT_SECONDS",
    "PLUGINS_ENABLED",
    "PLUGIN_CALL_TIMEOUT_SECONDS",
    "PLUGIN_ENTRY_POINT_GROUP",
    "PLUGIN_HANDLER_KINDS",
    "PLUGIN_TOOL_MODES",
    "WORKSPACE_PLUGIN_DIRECTORY",
    "PluginCommandBridge",
    "PluginError",
    "PluginManifest",
    "PluginRegistry",
    "PluginScope",
    "PluginStateService",
    "PluginTool",
    "builtin_plugin_directory",
    "call_plugin_tool",
    "conversation_plugin_scope",
    "deployment_plugin_directory",
    "discover_directory_plugins",
    "discover_package_plugins",
    "discover_workspace_plugins",
    "load_python_handler",
    "load_workspace_handler",
    "parse_manifest",
    "plugin_allowed_tools",
    "plugin_code_allowed",
    "plugin_denied_tools",
    "plugin_environment",
    "plugin_guidance",
    "plugin_registry",
    "plugin_runtime_summary",
    "plugin_tool_handler",
    "plugin_tool_specs",
    "plugin_workspace_root",
    "read_manifest_file",
    "resolve_plugin_manifest",
    "run_command_handler",
    "workspace_plugin_directory",
    "workspace_plugin_tool_specs",
]
