"""Named, reusable configurations of how a DeepSpace agent runs.

A preset is the bundle of settings a conversation starts with: the tools the
agent may call, whether it may delegate, whether it may run programs, and
which sandbox, reasoning, and spill limits apply.  The deployment owns the
defaults; a preset either names a value or leaves it to the deployment.

The catalogue holds the presets the product ships.  ``standard`` is the full
tool set, ``ptc`` reaches the native catalogue through one programmatic
call, ``minimal`` answers a quick question with read-only tools, ``creator``
writes notes, Library files, artifacts, and memory, and ``custom`` is the
preset the user configures.

The ``custom`` preset's settings are recorded with the conversation's preset
in the typed session log, so the settings the user edited are read back and
applied on the next turn.

The module reads the deployment settings through ``getattr``, so it works
before the settings that name a preset exist, and it never imports a setting
that is not there yet.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any

from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL_NAMES


def _native_tool_package() -> ModuleType | None:
    """Return the native tool package, or None when it cannot be imported.

    The package is resolved when the tool list is needed, not when this
    module is imported.  The tool package imports the delegation tools,
    which import the subagent runner, which imports this module, so
    importing it here would leave it half-initialised and the presets would
    fall back to a stale list of tool names.

    Returns:
        The tool package, or None when it cannot be imported.
    """
    try:
        from app.deepspace.services import tools as package
    except ImportError:  # pragma: no cover - the native tool package ships with the app
        return None
    return package


#: The preset a conversation runs when the caller names none.
DEFAULT_PRESET_ID = "standard"

#: The preset the user configures.
CUSTOM_PRESET_ID = "custom"

#: Prefix for durable user-owned custom preset ids.
CUSTOM_PRESET_PREFIX = "custom_"

#: The legacy pseudo-conversation id kept for old local seeds.
USER_DEFAULT_CONVERSATION_ID = uuid_module.UUID(int=0)

#: Namespace used to derive stable, non-visible settings conversation ids.
USER_DEFAULT_CONVERSATION_NAMESPACE = uuid_module.UUID("7f9e5d42-1d54-4a9f-92c9-6fcb3e6e3c50")


def user_default_conversation_id(tenant_id: Any, user_id: Any) -> uuid_module.UUID:
    """Return the hidden conversation id that stores one user's default.

    Args:
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        A stable id suitable for the session-event foreign key.
    """
    return uuid_module.uuid5(
        USER_DEFAULT_CONVERSATION_NAMESPACE,
        f"{tenant_id}:{user_id}",
    )


def is_custom_preset_id(preset_id: str | None) -> bool:
    """Return whether an id names the legacy or a saved custom preset.

    Args:
        preset_id: The preset id to inspect.

    Returns:
        True for the legacy ``custom`` id or a durable ``custom_<uuid>`` id.
    """
    key = str(preset_id or "").strip().lower()
    return key == CUSTOM_PRESET_ID or key.startswith(CUSTOM_PRESET_PREFIX)


#: The sandbox modes a preset may choose.  The deployment owns the names.
SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")

#: The sandbox modes ordered from the strictest to the loosest.  A mode that
#: is earlier in this order keeps a conversation inside a tighter boundary.
SANDBOX_MODE_STRICTNESS: tuple[str, ...] = (
    "read-only",
    "workspace-write",
    "danger-full-access",
)

#: The reasoning efforts a preset may request.
REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: The delegation depth the deployment allows when it names none.
DEFAULT_SUBAGENT_MAX_DEPTH = 3

#: The sandbox mode the deployment runs in when it names none.
DEFAULT_SANDBOX_MODE = "workspace-write"

#: The result size above which the deployment spills a tool result.
DEFAULT_SPILL_THRESHOLD_CHARS = 24_000

#: The deployment settings that may carry the default reasoning effort.  The
#: first one that is set wins, so the deployment may name it either way.
REASONING_EFFORT_SETTINGS: tuple[str, ...] = (
    "deepspace_reasoning_effort",
    "deepspace_default_reasoning_effort",
)

#: The tools the compatibility registry owns, which the native tool package
#: does not declare.  The runtime policy names their modes and the tool
#: registry their schemas, so the preset allowlists name them the same way.
COMPATIBILITY_TOOL_NAMES: tuple[str, ...] = (
    "analyze",
    "artifact_create",
    "ask_user",
    "delete",
    "discover_tools",
    "edit",
    "find",
    "get_tool_result",
    "image_read",
    "read",
    "sandbox_execute",
    "todo_write",
    "url_read",
    "web_search",
    "write",
) + tuple(sorted(MCP_BROKER_TOOL_NAMES))

#: The tools the model uses to find the tools a preset allows.
#:
#: A preset decides which tools the model may call.  The discovery tools
#: are how the model finds out what those are, so a preset that names a
#: tool list keeps them.
DISCOVERY_TOOL_NAMES: tuple[str, ...] = ("discover_tools", "get_tool_result")

#: The read-only tools the compatibility registry owns.
_COMPATIBILITY_READ_TOOL_NAMES: tuple[str, ...] = (
    "analyze",
    "discover_tools",
    "find",
    "get_tool_result",
    "image_read",
    "read",
    "url_read",
    "web_search",
)

#: The tools the native tool package owns, used when that package cannot be
#: imported.
_FALLBACK_NATIVE_TOOL_NAMES: tuple[str, ...] = (
    "bash",
    "extension_list",
    "extension_remove",
    "extension_write",
    "fs_delete",
    "fs_edit",
    "fs_glob",
    "fs_grep",
    "fs_read",
    "fs_str_replace",
    "fs_write",
    "goal_read",
    "goal_set",
    "interrupt_agent",
    "job_kill",
    "job_list",
    "job_status",
    "list_agents",
    "plan_read",
    "plan_submit",
    "pwsh",
    "ralph_run",
    "run_code",
    "send_message",
    "skill_list",
    "skill_load",
    "spill_read",
    "job_output",
    "read_image",
    "present",
    "plugin_manager",
    "averqel_inspect_list",
    "averqel_inspect_query",
    "list_subagent_models",
    "load_workspace_dependencies",
    "session_search",
    "session_event_search",
    "session_event_read",
    "session_trace",
    "session_event_trace",
    "terminal_signal",
    "terminal_job",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
    "terminal_list",
    "terminal_read",
    "workspace_diff",
    "subagent",
    "subagent_fork",
    "web_fetch",
    "workflow_run",
)

#: The native tools whose execution mode is read, used when the native tool
#: package cannot be imported.
_FALLBACK_NATIVE_READ_TOOL_NAMES: tuple[str, ...] = (
    "extension_list",
    "fs_glob",
    "fs_grep",
    "fs_read",
    "goal_read",
    "job_list",
    "job_status",
    "list_agents",
    "plan_read",
    "skill_list",
    "skill_load",
    "spill_read",
    "job_output",
    "read_image",
    "present",
    "plugin_manager",
    "averqel_inspect_list",
    "averqel_inspect_query",
    "list_subagent_models",
    "load_workspace_dependencies",
    "session_search",
    "session_event_search",
    "session_event_read",
    "session_trace",
    "session_event_trace",
    "terminal_signal",
    "terminal_job",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
    "terminal_list",
    "terminal_read",
    "workspace_diff",
    "web_fetch",
)

#: The content tools the creator preset uses: the note, Library, memory, and
#: artifact writers, the readers that go with them, the discovery pair, and
#: the question tool.  There is no shell, no delegation, and no programmatic
#: calling.
CREATOR_TOOL_NAMES: tuple[str, ...] = (
    "analyze",
    "artifact_create",
    "ask_user",
    "delete",
    "discover_tools",
    "edit",
    "find",
    "get_tool_result",
    "image_read",
    "read",
    "todo_write",
    "url_read",
    "web_search",
    "write",
)


def _native_tool_modes() -> dict[str, str]:
    """Return the execution mode of every tool the native tool package owns.

    Returns:
        The modes, keyed by tool name.  The mapping is empty when the native
        tool package is absent.
    """
    package = _native_tool_package()
    if package is None:
        return {}
    modes = getattr(package, "NATIVE_TOOL_MODES", None)
    if not isinstance(modes, dict):
        return {}
    return {str(name): str(mode) for name, mode in modes.items()}


def native_tool_names() -> tuple[str, ...]:
    """Return the names of the tools the native tool package owns.

    Returns:
        The tool names, in the order the package declares them.
    """
    modes = _native_tool_modes()
    if not modes:
        return _FALLBACK_NATIVE_TOOL_NAMES
    return tuple(modes)


def read_only_tool_names() -> tuple[str, ...]:
    """Return the names of the tools that only read.

    The set is the native tools whose execution mode is read, together with
    the read-only tools the compatibility registry owns.

    Returns:
        The read-only tool names, in a stable order.
    """
    modes = _native_tool_modes()
    if not modes:
        native_read = _FALLBACK_NATIVE_READ_TOOL_NAMES
    else:
        native_read = tuple(name for name, mode in modes.items() if mode == "read")
    return tuple(sorted(set(native_read) | set(_COMPATIBILITY_READ_TOOL_NAMES)))


def all_tool_names() -> tuple[str, ...]:
    """Return the names of every tool a preset may name.

    Returns:
        The native tool names together with the compatibility tool names, in
        a stable order.
    """
    return tuple(sorted(set(native_tool_names()) | set(COMPATIBILITY_TOOL_NAMES)))


@dataclass(frozen=True, slots=True)
class AgentPreset:
    """One named configuration of how an agent runs.

    A field set to None means the deployment's own value applies, so a
    preset that names nothing inherits every deployment default.
    """

    #: The identifier the caller and the API use.
    id: str
    #: The human-readable label.
    name: str
    #: The one-sentence description of what the preset is for.
    description: str
    #: The tools the preset permits, or None for every tool the policy allows.
    tool_allowlist: tuple[str, ...] | None
    #: Whether the agent may write and run a program that calls the tools.
    ptc_enabled: bool
    #: The delegation depth the preset allows, or None for the deployment's.
    subagent_max_depth: int | None
    #: Whether a turn with no explicit plan-mode choice starts in plan mode.
    plan_mode_by_default: bool
    #: The sandbox mode the preset runs in, or None for the deployment's.
    sandbox_mode: str | None
    #: The reasoning effort the preset requests, or None for the deployment's.
    reasoning_effort: str | None
    #: The result size above which a tool result spills, or None for the
    #: deployment's.
    spill_threshold_chars: int | None
    #: Whether the user may edit the preset's settings.
    editable: bool


class PresetNotFoundError(ValueError):
    """A preset id was not one the catalogue holds."""


class PresetSettingsError(ValueError):
    """A custom preset's settings were not acceptable."""


class PresetUnavailableError(ValueError):
    """A preset the user hid was chosen for a conversation."""


def strictest_sandbox_mode(*modes: str | None) -> str | None:
    """Return the strictest of the given sandbox modes.

    A preset may only narrow what the deployment allows, so the mode a
    conversation runs in is the strictest of the modes that apply to it.

    Args:
        modes: The modes to compare.  A mode the product does not know is
            ignored.

    Returns:
        The strictest known mode, or None when no mode is known.
    """
    known = [str(mode) for mode in modes if mode in SANDBOX_MODE_STRICTNESS]
    if not known:
        return None
    return min(known, key=SANDBOX_MODE_STRICTNESS.index)


def build_custom_preset(
    *,
    name: str = "Custom",
    description: str = "A preset the user configured.",
    tool_allowlist: tuple[str, ...] | None = None,
    ptc_enabled: bool = False,
    subagent_max_depth: int | None = None,
    plan_mode_by_default: bool = False,
    sandbox_mode: str | None = None,
    reasoning_effort: str | None = None,
    spill_threshold_chars: int | None = None,
    editable: bool = True,
) -> AgentPreset:
    """Build a custom preset from the caller's settings.

    The settings the caller does not name keep the standard preset's values,
    so a custom preset without a tool allowlist permits every tool the
    policy allows.

    Args:
        name: The human-readable label.
        description: The one-sentence description.
        tool_allowlist: The tools the preset permits, or None for every tool
            the policy allows.
        ptc_enabled: Whether the agent may write and run a program that calls
            the tools.
        subagent_max_depth: The delegation depth the preset allows, or None
            for the deployment's.
        plan_mode_by_default: Whether a turn with no explicit plan-mode choice
            starts in plan mode.
        sandbox_mode: The sandbox mode the preset runs in, or None for the
            deployment's.
        reasoning_effort: The reasoning effort the preset requests, or None
            for the deployment's.
        spill_threshold_chars: The result size above which a tool result
            spills, or None for the deployment's.
        editable: Whether the user may edit the preset's settings.

    Returns:
        The custom preset.
    """
    return AgentPreset(
        id=CUSTOM_PRESET_ID,
        name=name,
        description=description,
        tool_allowlist=tool_allowlist,
        ptc_enabled=ptc_enabled,
        subagent_max_depth=subagent_max_depth,
        plan_mode_by_default=plan_mode_by_default,
        sandbox_mode=sandbox_mode,
        reasoning_effort=reasoning_effort,
        spill_threshold_chars=spill_threshold_chars,
        editable=editable,
    )


def _custom_tool_names(value: object) -> tuple[str, ...] | None:
    """Read the tool list a custom preset names.

    Args:
        value: The stored tool list.

    Returns:
        The tool names, or None when the preset names none.

    Raises:
        PresetSettingsError: When the value is not a list of names.
    """
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise PresetSettingsError("A custom preset's tools must be a list of tool names.")
    names = tuple(str(item).strip() for item in value if str(item).strip())
    # ``None`` means the custom preset inherits every deployment-allowed
    # tool; an empty tuple is an explicit choice to allow no optional tools.
    return names


def _custom_flag(value: object, *, field: str, default: bool) -> bool:
    """Read one flag a custom preset carries.

    Args:
        value: The stored flag.
        field: The name of the setting, used in the refusal.
        default: The value the flag takes when the caller names none.

    Returns:
        The flag.

    Raises:
        PresetSettingsError: When the value is not a boolean.
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PresetSettingsError(f"A custom preset's {field} must be true or false.")
    return value


def _custom_number(value: object, *, field: str, minimum: int) -> int | None:
    """Read one number a custom preset carries.

    Args:
        value: The stored number.
        field: The name of the setting, used in the refusal.
        minimum: The smallest number the setting accepts.

    Returns:
        The number, or None when the caller names none.

    Raises:
        PresetSettingsError: When the value is not a number the setting accepts.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresetSettingsError(f"A custom preset's {field} must be a whole number.")
    if value < minimum:
        raise PresetSettingsError(f"A custom preset's {field} must be at least {minimum}.")
    return int(value)


def _custom_choice(value: object, *, field: str, choices: tuple[str, ...]) -> str | None:
    """Read one named choice a custom preset carries.

    Args:
        value: The stored choice.
        field: The name of the setting, used in the refusal.
        choices: The names the setting accepts.

    Returns:
        The choice, or None when the caller names none.

    Raises:
        PresetSettingsError: When the value is not one of the choices.
    """
    if value is None:
        return None
    name = str(value).strip().lower()
    if name not in choices:
        raise PresetSettingsError(f"A custom preset's {field} must be one of {', '.join(choices)}.")
    return name


def custom_preset_from_settings(settings: Mapping[str, Any]) -> AgentPreset:
    """Build the custom preset from the settings a conversation recorded.

    The settings are the ones the user edited, so they are checked before the
    preset is built: a stored custom preset always resolves, and a value the
    product does not accept is refused instead of silently ignored.

    Args:
        settings: The recorded settings.

    Returns:
        The custom preset the settings describe.

    Raises:
        PresetSettingsError: When a setting is not one the product accepts.
    """
    missing = object()
    tools = settings.get("tools", missing)
    if tools is missing:
        tools = settings.get("tool_allowlist", missing)
    tool_allowlist = None if tools is missing else _custom_tool_names(tools)
    name = str(settings.get("name") or "").strip() or CUSTOM_PRESET.name
    description = str(settings.get("description") or "").strip() or CUSTOM_PRESET.description
    return AgentPreset(
        id=str(settings.get("id") or CUSTOM_PRESET_ID).strip().lower(),
        name=name[:120],
        description=description[:400],
        tool_allowlist=tool_allowlist,
        ptc_enabled=_custom_flag(
            settings.get("ptc_enabled"),
            field="programmatic calling",
            default=CUSTOM_PRESET.ptc_enabled,
        ),
        subagent_max_depth=_custom_number(
            settings.get("subagent_max_depth"),
            field="delegation depth",
            minimum=0,
        ),
        plan_mode_by_default=_custom_flag(
            settings.get("plan_mode_by_default"),
            field="plan mode",
            default=CUSTOM_PRESET.plan_mode_by_default,
        ),
        sandbox_mode=_custom_choice(
            settings.get("sandbox_mode"),
            field="sandbox mode",
            choices=SANDBOX_MODES,
        ),
        reasoning_effort=_custom_choice(
            settings.get("reasoning_effort"),
            field="reasoning effort",
            choices=REASONING_EFFORTS,
        ),
        spill_threshold_chars=_custom_number(
            settings.get("spill_threshold_chars"),
            field="spill threshold",
            minimum=1,
        ),
        editable=True,
    )


STANDARD_PRESET = AgentPreset(
    id="standard",
    name="Standard",
    description="Every tool the policy allows, with delegation available and no programmatic calling.",
    tool_allowlist=None,
    ptc_enabled=False,
    subagent_max_depth=None,
    plan_mode_by_default=False,
    sandbox_mode=None,
    reasoning_effort=None,
    spill_threshold_chars=None,
    editable=False,
)

#: The programmatic tool calling preset.  Its allowlist is the native
#: catalogue, which keeps ``run_code`` in it: the model calls the program
#: instead of the tools one by one, and the program reaches the other native
#: tools as ordinary function calls.  The delegation family stays authorized
#: as well, because the preset allows one level of delegation.
PTC_PRESET = AgentPreset(
    id="ptc",
    name="Programmatic tool calling",
    description="The agent writes one program that calls the native tools as ordinary function calls.",
    tool_allowlist=native_tool_names(),
    ptc_enabled=True,
    subagent_max_depth=1,
    plan_mode_by_default=False,
    sandbox_mode=None,
    reasoning_effort="medium",
    spill_threshold_chars=None,
    editable=False,
)

MINIMAL_PRESET = AgentPreset(
    id="minimal",
    name="Minimal",
    description="Read-only tools for a quick question, with no shell and no delegation.",
    tool_allowlist=read_only_tool_names(),
    ptc_enabled=False,
    subagent_max_depth=0,
    plan_mode_by_default=False,
    sandbox_mode="read-only",
    reasoning_effort=None,
    spill_threshold_chars=None,
    editable=False,
)

CREATOR_PRESET = AgentPreset(
    id="creator",
    name="Creator",
    description="Notes, Library files, artifacts, and memory, with no shell and no delegation.",
    tool_allowlist=CREATOR_TOOL_NAMES,
    ptc_enabled=False,
    subagent_max_depth=0,
    plan_mode_by_default=False,
    sandbox_mode=None,
    reasoning_effort=None,
    spill_threshold_chars=None,
    editable=False,
)

CUSTOM_PRESET = build_custom_preset()

#: Every preset the catalogue holds, keyed by id.
PRESETS: dict[str, AgentPreset] = {
    preset.id: preset
    for preset in (
        STANDARD_PRESET,
        PTC_PRESET,
        MINIMAL_PRESET,
        CREATOR_PRESET,
        CUSTOM_PRESET,
    )
}

#: The preset ids the catalogue holds, in the order the API publishes them.
PRESET_IDS: tuple[str, ...] = tuple(PRESETS)

#: The tools each preset permits, keyed by preset id.  A preset with no
#: allowlist of its own permits every tool the policy allows.
PRESET_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    preset_id: (preset.tool_allowlist if preset.tool_allowlist is not None else all_tool_names())
    for preset_id, preset in PRESETS.items()
}


def _refresh_dynamic_presets() -> None:
    """Refresh presets whose tool lists come from the native registry.

    The native package imports the preset module while the application is
    starting.  Keeping PTC and Minimal dynamic prevents that import order
    from freezing their fallback tool lists before the native definitions
    exist.
    """
    PRESETS["ptc"] = replace(PTC_PRESET, tool_allowlist=native_tool_names())
    PRESETS["minimal"] = replace(MINIMAL_PRESET, tool_allowlist=read_only_tool_names())
    for preset_id, preset in PRESETS.items():
        PRESET_TOOL_NAMES[preset_id] = tuple(
            preset.tool_allowlist if preset.tool_allowlist is not None else all_tool_names()
        )


def get_preset(preset_id: str | None) -> AgentPreset:
    """Return the preset with this id.

    Args:
        preset_id: The preset id, or None for the default preset.  The id is
            matched without regard to case.

    Returns:
        The preset, or the default preset when no id is given.

    Raises:
        PresetNotFoundError: When the id is not one the catalogue holds.
    """
    _refresh_dynamic_presets()
    key = str(preset_id or "").strip().lower()
    if not key:
        return PRESETS[DEFAULT_PRESET_ID]
    try:
        return PRESETS[key]
    except KeyError as exc:
        raise PresetNotFoundError(f"Unknown agent preset: {preset_id!r}") from exc


def list_presets() -> list[AgentPreset]:
    """Return every preset the catalogue holds.

    Returns:
        The presets, in the catalogue's order.
    """
    _refresh_dynamic_presets()
    return [PRESETS[preset_id] for preset_id in PRESET_IDS]


@dataclass(frozen=True, slots=True)
class PresetResolution:
    """The effective settings one conversation runs with.

    The values come from the preset and, where the preset names none, from
    the deployment.
    """

    #: The preset the resolution came from.
    preset: AgentPreset
    #: The tools the conversation may call, or None for every tool the
    #: policy allows.
    tool_allowlist: tuple[str, ...] | None
    #: Whether the agent may write and run a program that calls the tools.
    ptc_enabled: bool
    #: The delegation depth the conversation allows.
    subagent_max_depth: int
    #: The sandbox mode the conversation runs in.
    sandbox_mode: str | None
    #: The reasoning effort the conversation requests.
    reasoning_effort: str | None
    #: The result size above which a tool result spills.
    spill_threshold_chars: int | None
    #: Whether a turn with no explicit plan-mode choice starts in plan mode.
    plan_mode_by_default: bool


def _deployment_subagent_max_depth(settings: Any) -> int:
    """Return the delegation depth the deployment allows."""
    return int(getattr(settings, "deepspace_subagent_max_depth", DEFAULT_SUBAGENT_MAX_DEPTH))


def _subagent_max_depth(preset: AgentPreset, settings: Any) -> int:
    """Return the delegation depth the conversation allows.

    A preset may only narrow what the deployment allows, so the depth is the
    smaller of the two.
    """
    deployment_depth = _deployment_subagent_max_depth(settings)
    if preset.subagent_max_depth is None:
        return deployment_depth
    return min(int(preset.subagent_max_depth), deployment_depth)


def _ptc_enabled(preset: AgentPreset, settings: Any) -> bool:
    """Return whether both the preset and deployment allow program calls."""
    return preset.ptc_enabled and bool(getattr(settings, "deepspace_ptc_enabled", True))


def _deployment_sandbox_mode(settings: Any) -> str:
    """Return the sandbox mode the deployment allows."""
    mode = getattr(settings, "deepspace_sandbox_policy", None) or getattr(
        settings, "deepspace_permission_mode", None
    )
    if not mode:
        return DEFAULT_SANDBOX_MODE
    return str(mode)


def _sandbox_mode(preset: AgentPreset, settings: Any) -> str | None:
    """Return the sandbox mode the conversation runs in.

    A preset may only narrow what the deployment allows, so the mode is the
    strictest of the two.
    """
    deployment_mode = _deployment_sandbox_mode(settings)
    if preset.sandbox_mode is None:
        return deployment_mode
    return strictest_sandbox_mode(preset.sandbox_mode, deployment_mode) or deployment_mode


def _reasoning_effort(preset: AgentPreset, settings: Any) -> str | None:
    """Return the reasoning effort the conversation requests."""
    if preset.reasoning_effort is not None:
        return preset.reasoning_effort
    for setting_name in REASONING_EFFORT_SETTINGS:
        value = getattr(settings, setting_name, None)
        if value:
            return str(value)
    return None


def _spill_threshold(preset: AgentPreset, settings: Any) -> int | None:
    """Return the result size above which a tool result spills."""
    if preset.spill_threshold_chars is not None:
        return int(preset.spill_threshold_chars)
    value = getattr(settings, "deepspace_spill_threshold_chars", None)
    if value is None:
        return DEFAULT_SPILL_THRESHOLD_CHARS
    return int(value)


def _with_discovery_tools(allowlist: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Return a preset's tool list with the discovery tools in it.

    A preset that names no tools permits every tool the policy allows, so
    the discovery tools are already there.

    Args:
        allowlist: The tools the preset names, or None for every tool.

    Returns:
        The tool list, or None when the preset names none.
    """
    if allowlist is None:
        return None
    missing = [name for name in DISCOVERY_TOOL_NAMES if name not in allowlist]
    if not missing:
        return allowlist
    return tuple(allowlist) + tuple(missing)


def resolve_preset(
    preset_id: str | None,
    *,
    settings: Any = None,
    custom_settings: Mapping[str, Any] | None = None,
) -> PresetResolution:
    """Resolve a preset against the deployment settings.

    A preset that names no value takes the deployment's value, so the
    deployment keeps the last word on its own limits.  A custom preset that a
    conversation recorded carries the settings the user edited, so the
    resolution is built from those settings rather than from the catalogue.

    Args:
        preset_id: The preset id, or None for the default preset.
        settings: The deployment settings in force.  The resolution reads
            them through ``getattr``, so the settings may name none of these
            values yet.
        custom_settings: The settings the conversation recorded for its
            custom preset.  They apply to the custom preset only.

    Returns:
        The effective settings for one conversation.

    Raises:
        PresetNotFoundError: When the id is not one the catalogue holds.
        PresetSettingsError: When the recorded settings are not acceptable.
    """
    normalized_id = str(preset_id or "").strip().lower()
    if is_custom_preset_id(normalized_id) and normalized_id != CUSTOM_PRESET_ID:
        if custom_settings is None:
            raise PresetSettingsError("A saved custom preset must provide its persisted settings.")
        custom_payload = dict(custom_settings)
        custom_payload["id"] = normalized_id
        preset = custom_preset_from_settings(custom_payload)
    else:
        preset = get_preset(normalized_id)
        if custom_settings and preset.id == CUSTOM_PRESET_ID:
            custom_payload = dict(custom_settings)
            custom_payload["id"] = CUSTOM_PRESET_ID
            preset = custom_preset_from_settings(custom_payload)
    return PresetResolution(
        preset=preset,
        tool_allowlist=_with_discovery_tools(preset.tool_allowlist),
        ptc_enabled=_ptc_enabled(preset, settings),
        subagent_max_depth=_subagent_max_depth(preset, settings),
        sandbox_mode=_sandbox_mode(preset, settings),
        reasoning_effort=_reasoning_effort(preset, settings),
        spill_threshold_chars=_spill_threshold(preset, settings),
        plan_mode_by_default=preset.plan_mode_by_default,
    )


def tool_allowed(preset_id: str | None, tool_name: str, *, settings: Any = None) -> bool:
    """Report whether a preset permits a tool.

    A preset without an allowlist of its own permits every tool the policy
    allows.  The deployment's own gated tools stay with the runtime that
    owns them, so the settings do not change this answer.

    Args:
        preset_id: The preset id, or None for the default preset.
        tool_name: The tool name the model called.
        settings: The deployment settings in force.

    Returns:
        True when the preset permits the tool.

    Raises:
        PresetNotFoundError: When the id is not one the catalogue holds.
    """
    allowlist = resolve_preset(preset_id, settings=settings).tool_allowlist
    if allowlist is None:
        return True
    return str(tool_name) in allowlist


def preset_defaults(preset_id: str | None, *, settings: Any = None) -> dict[str, Any]:
    """Return the settings a new conversation inherits from a preset.

    Args:
        preset_id: The preset id, or None for the default preset.
        settings: The deployment settings in force.

    Returns:
        The sandbox mode, plan mode, reasoning effort, spill threshold,
        programmatic calling, and delegation depth of a new conversation.

    Raises:
        PresetNotFoundError: When the id is not one the catalogue holds.
    """
    resolution = resolve_preset(preset_id, settings=settings)
    return {
        "sandbox_mode": resolution.sandbox_mode,
        "plan_mode_by_default": resolution.plan_mode_by_default,
        "reasoning_effort": resolution.reasoning_effort,
        "spill_threshold_chars": resolution.spill_threshold_chars,
        "ptc_enabled": resolution.ptc_enabled,
        "subagent_max_depth": resolution.subagent_max_depth,
    }


class PresetStateService:
    """The agent preset one conversation runs.

    The preset is recorded in the typed session log as a ``preset/mode``
    event, so it survives a restart by replay and cannot be changed out of
    band.  The deployment's default preset is only the default: the
    conversation's own preset decides.
    """

    def __init__(self, db: Any) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        from sqlalchemy import select

        from app.deepspace.models.session_event import DeepSpaceSessionEvent
        from app.deepspace.services.session_events import PRESET_MODE, SessionEventStore

        self.db = db
        self.events = SessionEventStore(db)
        self._select = select
        self._event_model = DeepSpaceSessionEvent
        self._event_type = PRESET_MODE

    def _last_preset_payload(
        self, *, tenant_id: Any, user_id: Any, conversation_id: Any
    ) -> dict[str, Any] | None:
        """Read the payload of the conversation's last preset event.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The payload, or None when the conversation never recorded one.
        """
        events = (
            self.db.execute(
                self._select(self._event_model)
                .where(
                    self._event_model.tenant_id == tenant_id,
                    self._event_model.user_id == user_id,
                    self._event_model.conversation_id == conversation_id,
                    self._event_model.event_type == self._event_type,
                )
                .order_by(self._event_model.seq.desc())
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
        return payload

    def recorded_preset(self, *, tenant_id: Any, user_id: Any, conversation_id: Any) -> str | None:
        """Read the conversation's last recorded preset.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded preset id, or None when the conversation never
            recorded one.
        """
        payload = self._last_preset_payload(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if payload is None:
            return None
        preset_id = str(payload.get("preset") or "")
        return preset_id or None

    def recorded_preset_settings(
        self, *, tenant_id: Any, user_id: Any, conversation_id: Any
    ) -> dict[str, Any] | None:
        """Read the settings the conversation recorded for its custom preset.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded settings, or None when the conversation recorded
            none.
        """
        payload = self._last_preset_payload(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if payload is None:
            return None
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            return None
        return dict(settings)

    def user_default_custom_settings(
        self, *, tenant_id: Any, user_id: Any
    ) -> dict[str, Any] | None:
        """Read the user's saved settings for the built-in Custom mode.

        The user may choose another preset as the default without losing the
        last Custom configuration. Older events that predate the dedicated
        ``custom_settings`` field are also read when they still carry a
        built-in Custom settings payload.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The saved Custom settings, or None when the user has never saved
            them.
        """
        events = (
            self.db.execute(
                self._select(self._event_model)
                .where(
                    self._event_model.tenant_id == tenant_id,
                    self._event_model.user_id == user_id,
                    self._event_model.conversation_id
                    == user_default_conversation_id(tenant_id, user_id),
                    self._event_model.event_type == self._event_type,
                )
                .order_by(self._event_model.seq.desc())
            )
            .scalars()
            .all()
        )
        for event in events:
            payload: object = event.payload
            if not isinstance(payload, dict):
                continue
            preserved = payload.get("custom_settings")
            if isinstance(preserved, dict):
                return dict(preserved)
            if str(payload.get("preset") or "").strip().lower() != CUSTOM_PRESET_ID:
                continue
            settings = payload.get("settings")
            if isinstance(settings, dict):
                return dict(settings)
        return None

    def user_default_preset(self, *, tenant_id: Any, user_id: Any) -> str | None:
        """Read the default preset the user chose for new conversations.

        The choice is recorded on the user-default pseudo-conversation, so
        it survives a restart with the rest of the typed log.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The recorded preset id, or None when the user recorded none.
        """
        payload = self.last_user_default_payload(tenant_id=tenant_id, user_id=user_id)
        if payload is None:
            return None
        preset_id = str(payload.get("preset") or "")
        return preset_id or None

    def last_user_default_payload(self, *, tenant_id: Any, user_id: Any) -> dict[str, Any] | None:
        """Read the payload the user recorded for new conversations.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The payload, or None when the user never recorded one.
        """
        return self._last_preset_payload(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=user_default_conversation_id(tenant_id, user_id),
        )

    def _ensure_user_default_conversation(self, *, tenant_id: Any, user_id: Any) -> None:
        """Create the hidden conversation required by the session-event FK."""
        from sqlalchemy import select

        from app.deepspace.models.conversation import Conversation

        conversation_id = user_default_conversation_id(tenant_id, user_id)
        existing = self.db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        ).scalar_one_or_none()
        if existing is not None:
            return
        self.db.add(
            Conversation(
                id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="__user_default_preset__",
                kind="agent_preset_default",
            )
        )
        self.db.flush()

    def effective_preset_entry(
        self,
        *,
        tenant_id: Any,
        user_id: Any,
        conversation_id: Any,
        deployment_default: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Resolve the preset one conversation runs with, and its settings.

        The conversation's own record decides first.  A conversation that
        never recorded one inherits the default the user chose for new
        conversations, then the deployment's configured preset.  The custom
        settings travel with the record that decided, so a user's default
        custom preset applies to every new conversation that inherits it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the call belongs to.
            deployment_default: The deployment's configured preset.

        Returns:
            The preset id and the custom settings the deciding record
            carries, or None when it carries none.
        """
        payload = self._last_preset_payload(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if payload is None:
            payload = self.last_user_default_payload(tenant_id=tenant_id, user_id=user_id)
        if payload is None:
            preset_id = deployment_default or DEFAULT_PRESET_ID
            settings: dict[str, Any] | None = None
        else:
            preset_id = str(payload.get("preset") or "") or (
                deployment_default or DEFAULT_PRESET_ID
            )
            raw_settings = payload.get("settings")
            settings = dict(raw_settings) if isinstance(raw_settings, dict) else None
            # Older conversation-level Custom selections recorded only the
            # preset id.  Use the user's saved Custom settings in that case
            # rather than treating the missing list as permission for every
            # tool.  New selections persist the settings themselves.
            if settings is None and str(preset_id).strip().lower() == CUSTOM_PRESET_ID:
                settings = self.user_default_custom_settings(
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
        return preset_id, settings

    def effective_preset(
        self,
        *,
        tenant_id: Any,
        user_id: Any,
        conversation_id: Any,
        deployment_default: str | None = None,
    ) -> str:
        """Resolve the preset one conversation runs with.

        The conversation's own record decides, then the user's default for
        new conversations, then the deployment's configured preset.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the call belongs to.
            deployment_default: The deployment's configured preset.

        Returns:
            The preset id.
        """
        entry = self.effective_preset_entry(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            deployment_default=deployment_default,
        )
        return entry[0]

    def set_preset(
        self,
        *,
        tenant_id: Any,
        user_id: Any,
        conversation_id: Any,
        preset_id: str,
        custom_settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the conversation's preset.

        The custom preset's settings are recorded with the preset, so the
        settings the user edited survive a restart and are read back by the
        next turn.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            preset_id: The preset to record.
            custom_settings: The settings of the custom preset, when the
                conversation records one.

        Returns:
            The recorded state.

        Raises:
            PresetNotFoundError: When the id is not one the catalogue holds.
            PresetSettingsError: When the recorded settings are not
                acceptable.
        """
        normalized_id = str(preset_id or "").strip().lower()
        if is_custom_preset_id(normalized_id) and normalized_id != CUSTOM_PRESET_ID:
            if custom_settings is None:
                raise PresetSettingsError(
                    "A saved custom preset must provide its persisted settings."
                )
            custom_payload = dict(custom_settings)
            custom_payload["id"] = normalized_id
            preset = custom_preset_from_settings(custom_payload)
        else:
            preset = get_preset(normalized_id)
        if custom_settings is not None and not is_custom_preset_id(preset.id):
            raise PresetSettingsError(
                "Preset settings can only be supplied for the custom agent mode."
            )
        is_user_default = conversation_id == user_default_conversation_id(tenant_id, user_id)
        previous_custom_settings = (
            self.user_default_custom_settings(tenant_id=tenant_id, user_id=user_id)
            if is_user_default
            else None
        )
        payload: dict[str, Any] = {"preset": preset.id}
        if custom_settings is not None and is_custom_preset_id(preset.id):
            # The settings are checked before they are stored, so a stored
            # custom preset always resolves.
            custom_payload = dict(custom_settings)
            custom_payload["id"] = preset.id
            custom_preset_from_settings(custom_payload)
            payload["settings"] = dict(custom_settings)
            if is_user_default and preset.id == CUSTOM_PRESET_ID:
                payload["custom_settings"] = dict(custom_settings)
        elif is_user_default and previous_custom_settings is not None:
            if preset.id == CUSTOM_PRESET_ID:
                # Selecting Custom again without opening the editor must use
                # the last saved Custom configuration for new conversations.
                payload["settings"] = previous_custom_settings
            payload["custom_settings"] = previous_custom_settings
        if is_user_default:
            self._ensure_user_default_conversation(tenant_id=tenant_id, user_id=user_id)
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=self._event_type,
            payload=payload,
        )
        self.db.commit()
        return {"preset": preset.id}


class PresetEnablementService:
    """The agent modes one user may choose.

    A row named for a preset records whether the user's mode selector lists
    it.  An absent row keeps the preset available, so the catalogue the
    deployment ships stays available before the user touches a toggle.  The
    deployment's default preset cannot be hidden, because new conversations
    run with it.
    """

    def __init__(self, db: Any) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        from sqlalchemy import select

        from app.deepspace.models.preset_enablement import DeepSpacePresetEnablement

        self.db = db
        self._select = select
        self._model = DeepSpacePresetEnablement

    def enabled_map(self, *, tenant_id: Any, user_id: Any) -> dict[str, bool]:
        """Read the enablement state of every preset the catalogue holds.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The catalogue's preset ids, each naming whether the user's mode
            selector lists it.
        """
        tenant = str(tenant_id)
        user = str(user_id)
        from app.deepspace.models.custom_preset import DeepSpaceCustomPreset

        rows = (
            self.db.execute(
                self._select(self._model).where(
                    self._model.tenant_id == tenant,
                    self._model.user_id == user,
                )
            )
            .scalars()
            .all()
        )
        recorded = {str(row.preset_id): bool(row.enabled) for row in rows}
        custom_rows = (
            self.db.execute(
                self._select(DeepSpaceCustomPreset).where(
                    DeepSpaceCustomPreset.tenant_id == tenant,
                    DeepSpaceCustomPreset.user_id == user,
                )
            )
            .scalars()
            .all()
        )
        preset_ids = [preset.id for preset in list_presets()]
        preset_ids.extend(f"{CUSTOM_PRESET_PREFIX}{row.id}" for row in custom_rows)
        return {preset_id: recorded.get(preset_id, True) for preset_id in preset_ids}

    def set_enabled(
        self,
        *,
        tenant_id: Any,
        user_id: Any,
        preset_id: str,
        enabled: bool,
        deployment_default: str | None = None,
    ) -> dict[str, bool]:
        """Record whether the user's mode selector lists one preset.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            preset_id: The preset the toggle names.
            enabled: Whether the mode may be chosen.
            deployment_default: The deployment's configured preset.

        Returns:
            The enablement state of every catalogue preset after the change.

        Raises:
            PresetNotFoundError: When the id is not one the catalogue holds.
            PresetSettingsError: When the deployment's default preset is
                hidden.
        """
        normalized_id = str(preset_id or "").strip().lower()
        if is_custom_preset_id(normalized_id) and normalized_id != CUSTOM_PRESET_ID:
            from app.deepspace.models.custom_preset import DeepSpaceCustomPreset

            custom_exists = self.db.execute(
                self._select(DeepSpaceCustomPreset).where(
                    DeepSpaceCustomPreset.id == normalized_id.removeprefix(CUSTOM_PRESET_PREFIX),
                    DeepSpaceCustomPreset.tenant_id == str(tenant_id),
                    DeepSpaceCustomPreset.user_id == str(user_id),
                )
            ).scalar_one_or_none()
            if custom_exists is None:
                raise PresetNotFoundError(f"Unknown agent preset: {preset_id!r}")
            preset_id = normalized_id
        else:
            preset = get_preset(normalized_id)
            preset_id = preset.id
        if not enabled and preset_id == str(deployment_default or DEFAULT_PRESET_ID):
            raise PresetSettingsError("The deployment's default agent mode cannot be hidden.")
        tenant = str(tenant_id)
        user = str(user_id)
        self.db.commit()
        row = (
            self.db.execute(
                self._select(self._model).where(
                    self._model.tenant_id == tenant,
                    self._model.user_id == user,
                    self._model.preset_id == preset_id,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = self._model(tenant_id=tenant, user_id=user, preset_id=preset_id, enabled=enabled)
            self.db.add(row)
        else:
            row.enabled = enabled
        self.db.commit()
        return self.enabled_map(tenant_id=tenant, user_id=user)

    def unavailable_preset_ids(self, *, tenant_id: Any, user_id: Any) -> frozenset[str]:
        """Read the preset ids the user's mode selector does not list.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The hidden preset ids.
        """
        states = self.enabled_map(tenant_id=tenant_id, user_id=user_id)
        return frozenset(preset_id for preset_id, on in states.items() if not on)

    def assert_preset_available(
        self,
        *,
        tenant_id: Any,
        user_id: Any,
        preset_id: str,
        deployment_default: str | None = None,
    ) -> None:
        """Check a preset may be recorded on a conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            preset_id: The preset the conversation records.
            deployment_default: The deployment's configured preset.

        Raises:
            PresetNotFoundError: When the id is not one the catalogue holds.
            PresetUnavailableError: When the user hid the preset.
        """
        normalized_id = str(preset_id or "").strip().lower()
        if is_custom_preset_id(normalized_id) and normalized_id != CUSTOM_PRESET_ID:
            from app.deepspace.models.custom_preset import DeepSpaceCustomPreset

            custom_row = self.db.execute(
                self._select(DeepSpaceCustomPreset).where(
                    DeepSpaceCustomPreset.id == normalized_id.removeprefix(CUSTOM_PRESET_PREFIX),
                    DeepSpaceCustomPreset.tenant_id == str(tenant_id),
                    DeepSpaceCustomPreset.user_id == str(user_id),
                )
            ).scalar_one_or_none()
            if custom_row is None:
                raise PresetNotFoundError(f"Unknown agent preset: {preset_id!r}")
            preset_name = custom_row.name
            resolved_id = normalized_id
        else:
            preset = get_preset(normalized_id)
            preset_name = preset.name
            resolved_id = preset.id
        hidden = self.unavailable_preset_ids(tenant_id=tenant_id, user_id=user_id)
        if resolved_id in hidden:
            raise PresetUnavailableError(
                f"The agent mode '{preset_name}' is hidden for this account."
            )


def effective_preset_resolution(
    *,
    db: Any,
    tenant_id: Any,
    user_id: Any,
    conversation_id: Any,
    settings: Any = None,
    deployment_default: str | None = None,
) -> PresetResolution:
    """Resolve the preset one conversation runs with.

    The conversation's own preset decides, and the settings it recorded for a
    custom preset are part of that decision.  A conversation that never
    recorded one inherits the default the user chose for new conversations,
    then the deployment's configured preset, so the settings the user edited
    on the preset page apply to every new conversation.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation the call belongs to.
        settings: The deployment settings in force.
        deployment_default: The deployment's configured preset.

    Returns:
        The effective settings for the conversation.
    """
    service = PresetStateService(db)
    preset_id, custom_settings = service.effective_preset_entry(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        deployment_default=deployment_default,
    )
    return resolve_preset(preset_id, settings=settings, custom_settings=custom_settings)


__all__ = [
    "COMPATIBILITY_TOOL_NAMES",
    "CREATOR_TOOL_NAMES",
    "CUSTOM_PRESET",
    "CUSTOM_PRESET_ID",
    "CUSTOM_PRESET_PREFIX",
    "DEFAULT_PRESET_ID",
    "DEFAULT_SANDBOX_MODE",
    "DEFAULT_SPILL_THRESHOLD_CHARS",
    "DEFAULT_SUBAGENT_MAX_DEPTH",
    "MINIMAL_PRESET",
    "PRESETS",
    "PRESET_IDS",
    "PRESET_TOOL_NAMES",
    "PresetEnablementService",
    "PresetResolution",
    "PresetSettingsError",
    "PresetStateService",
    "PresetUnavailableError",
    "PTC_PRESET",
    "REASONING_EFFORTS",
    "REASONING_EFFORT_SETTINGS",
    "SANDBOX_MODES",
    "SANDBOX_MODE_STRICTNESS",
    "STANDARD_PRESET",
    "USER_DEFAULT_CONVERSATION_ID",
    "user_default_conversation_id",
    "AgentPreset",
    "PresetNotFoundError",
    "PresetSettingsError",
    "PresetStateService",
    "PresetResolution",
    "all_tool_names",
    "build_custom_preset",
    "custom_preset_from_settings",
    "effective_preset_resolution",
    "get_preset",
    "is_custom_preset_id",
    "list_presets",
    "native_tool_names",
    "preset_defaults",
    "read_only_tool_names",
    "resolve_preset",
    "strictest_sandbox_mode",
    "tool_allowed",
]
