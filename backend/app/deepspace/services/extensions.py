"""Extensions for DeepSpace.

An extension changes how the agent works without changing the product.  It
adds guidance to the system prompt, registers an alias for a tool the model
uses often, or tightens the tool policy for one conversation.

Extensions are declarative.  A manifest is a JSON document in the
conversation's workspace, so the agent can install and change one itself:
that is the runtime self-modification the surface offers.  Declarative
manifests are the right shape for that, because a manifest is data the
runtime applies, while an imported module would be code running inside the
API process with the worker's own privileges.

A deployment may install extensions of its own.  They live in the directory
``deepspace_extensions_dir`` names, they apply to every conversation, and a
conversation's own extension replaces a deployment extension of the same
name.

The deployment's own policy always wins.  An extension may narrow what the
agent may do, and it may add guidance, but it cannot widen the deployment's
approval mode, its sandbox policy, or its tool allowlist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.deepspace.services.workspace_root import (
    resolve_within_root,
    workspace_root_for,
)

#: The directory under the workspace root that holds extension manifests.
EXTENSIONS_DIRECTORY = ".deepspace/extensions"

#: The directory under the storage path that holds the deployment's own
#: extension manifests when the deployment names none.
DEPLOYMENT_EXTENSIONS_DIRECTORY = "extensions"

#: The largest manifest one extension may carry, in characters.
MAX_MANIFEST_CHARS = 20_000

#: The largest number of extensions one conversation may hold.
MAX_EXTENSIONS = 40

#: The largest number of aliases one extension may declare.
MAX_ALIASES = 20

#: The largest number of characters one guidance section may carry.
MAX_GUIDANCE_CHARS = 4_000

#: The largest number of guidance characters the merged extension set may
#: carry.  The deployment's extensions and the conversation's own extensions
#: share it, so reading both never grows the system prompt without a bound.
MAX_TOTAL_GUIDANCE_CHARS = MAX_EXTENSIONS * MAX_GUIDANCE_CHARS


class ExtensionError(ValueError):
    """An extension manifest was rejected."""


@dataclass(frozen=True, slots=True)
class ToolAlias:
    """One alias for a tool the model already has.

    Attributes:
        name: The alias the model calls.
        target: The tool the alias runs.
        arguments: The arguments the alias always supplies.
    """

    name: str
    target: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this alias."""
        return {"name": self.name, "target": self.target, "arguments": self.arguments}


@dataclass(frozen=True, slots=True)
class Extension:
    """One loaded extension.

    Attributes:
        name: The extension's name, unique within the workspace.
        description: What the extension changes.
        guidance: The text the extension adds to the system prompt.
        aliases: The tool aliases the extension registers.
        denied_tools: The tools the extension refuses.
        approval_tools: The tools the extension always asks about.
    """

    name: str
    description: str = ""
    guidance: str = ""
    aliases: tuple[ToolAlias, ...] = ()
    denied_tools: tuple[str, ...] = ()
    approval_tools: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this extension."""
        return {
            "name": self.name,
            "description": self.description,
            "guidance": self.guidance,
            "aliases": [alias.as_dict() for alias in self.aliases],
            "denied_tools": list(self.denied_tools),
            "approval_tools": list(self.approval_tools),
        }


@dataclass(frozen=True, slots=True)
class ExtensionSet:
    """The extensions one conversation runs with.

    Attributes:
        extensions: The loaded extensions.
        guidance: The combined guidance, in load order.
        aliases: The aliases the extensions register, by name.
        denied_tools: The tools the extensions refuse.
        approval_tools: The tools the extensions always ask about.
    """

    extensions: tuple[Extension, ...] = ()
    guidance: str = ""
    aliases: dict[str, ToolAlias] = field(default_factory=dict)
    denied_tools: frozenset[str] = frozenset()
    approval_tools: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this extension set."""
        return {
            "extensions": [extension.as_dict() for extension in self.extensions],
            "guidance": self.guidance,
            "aliases": {name: alias.as_dict() for name, alias in sorted(self.aliases.items())},
            "denied_tools": sorted(self.denied_tools),
            "approval_tools": sorted(self.approval_tools),
        }


def _normalize_name(raw: str) -> str:
    """Normalize one extension name.

    Args:
        raw: The name the manifest declared.

    Returns:
        The normalized name.

    Raises:
        ExtensionError: When the name is empty, too long, or has characters
            the runtime does not accept in a file name.
    """
    name = str(raw or "").strip().lower()
    if not name:
        raise ExtensionError("An extension needs a name.")
    if len(name) > 64:
        raise ExtensionError("An extension name may not exceed 64 characters.")
    if not all(character.isalnum() or character in {"-", "_"} for character in name):
        raise ExtensionError(
            "An extension name may only contain letters, digits, dashes, and underscores."
        )
    return name


def _normalize_aliases(raw: Any) -> tuple[ToolAlias, ...]:
    """Read the aliases one manifest declares.

    Args:
        raw: The manifest's alias mapping.

    Returns:
        The declared aliases.

    Raises:
        ExtensionError: When an alias is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ExtensionError("An extension's aliases must be an object.")
    aliases: list[ToolAlias] = []
    for name, target in list(raw.items())[:MAX_ALIASES]:
        alias_name = str(name or "").strip()
        if not alias_name:
            raise ExtensionError("An alias needs a name.")
        if isinstance(target, str):
            aliases.append(ToolAlias(name=alias_name, target=target.strip()))
            continue
        if not isinstance(target, dict):
            raise ExtensionError(f"The alias {alias_name!r} must name a tool.")
        tool = str(target.get("tool") or "").strip()
        if not tool:
            raise ExtensionError(f"The alias {alias_name!r} must name a tool.")
        arguments = target.get("arguments")
        aliases.append(
            ToolAlias(
                name=alias_name,
                target=tool,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return tuple(aliases)


def _normalize_tool_names(raw: Any, *, field: str) -> tuple[str, ...]:
    """Read the tool names one manifest field declares.

    Args:
        raw: The manifest's field.
        field: The field's name, used in the error message.

    Returns:
        The declared names.

    Raises:
        ExtensionError: When the field is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ExtensionError(f"An extension's {field} must be an array.")
    return tuple(str(item).strip() for item in raw if str(item).strip())


def parse_extension(document: dict[str, Any]) -> Extension:
    """Read one extension manifest.

    Args:
        document: The manifest's contents.

    Returns:
        The loaded extension.

    Raises:
        ExtensionError: When the manifest is malformed.
    """
    if not isinstance(document, dict):
        raise ExtensionError("An extension manifest must be an object.")
    guidance = str(document.get("guidance") or "").strip()
    if len(guidance) > MAX_GUIDANCE_CHARS:
        raise ExtensionError(
            f"An extension's guidance may not exceed {MAX_GUIDANCE_CHARS} characters."
        )
    return Extension(
        name=_normalize_name(str(document.get("name") or "")),
        description=str(document.get("description") or "").strip()[:1_000],
        guidance=guidance,
        aliases=_normalize_aliases(document.get("aliases")),
        denied_tools=_normalize_tool_names(document.get("deny_tools"), field="deny_tools"),
        approval_tools=_normalize_tool_names(
            document.get("approval_tools"), field="approval_tools"
        ),
    )


def _read_manifests(directory: Path) -> ExtensionSet:
    """Read the extension manifests one directory holds.

    A manifest that cannot be read is skipped rather than failing the turn,
    because one broken file must not take the conversation down.  The load
    order is the file name's order, so the combined guidance is stable.

    Args:
        directory: The directory that holds the manifests.

    Returns:
        The extensions the directory holds.
    """
    extensions: list[Extension] = []
    aliases: dict[str, ToolAlias] = {}
    denied: set[str] = set()
    approvals: set[str] = set()
    guidance_parts: list[str] = []
    if not directory.is_dir():
        return ExtensionSet()
    for path in sorted(directory.glob("*.json"))[:MAX_EXTENSIONS]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            extension = parse_extension(document)
        except ExtensionError:
            continue
        extensions.append(extension)
        for alias in extension.aliases:
            aliases.setdefault(alias.name, alias)
        denied.update(extension.denied_tools)
        approvals.update(extension.approval_tools)
        if extension.guidance:
            guidance_parts.append(extension.guidance)
    return ExtensionSet(
        extensions=tuple(extensions),
        guidance="\n".join(guidance_parts),
        aliases=aliases,
        denied_tools=frozenset(denied),
        approval_tools=frozenset(approvals),
    )


def load_extensions(root: Path) -> ExtensionSet:
    """Load the extensions one workspace holds.

    Args:
        root: The workspace root.

    Returns:
        The extensions the workspace holds.
    """
    return _read_manifests(root / EXTENSIONS_DIRECTORY)


def deployment_extensions_root(settings: Any) -> Path | None:
    """Return the directory that holds the deployment's own extensions.

    The directory is the one ``deepspace_extensions_dir`` names.  A
    deployment that names none uses ``<local_storage_path>/extensions``.

    Args:
        settings: The deployment settings in force.

    Returns:
        The directory, or None when the deployment names no storage path and
        no directory of its own.
    """
    configured = str(getattr(settings, "deepspace_extensions_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    local_storage = str(getattr(settings, "local_storage_path", "") or "").strip()
    if not local_storage:
        return None
    return Path(local_storage).expanduser() / DEPLOYMENT_EXTENSIONS_DIRECTORY


def load_deployment_extensions(settings: Any) -> ExtensionSet:
    """Load the extensions the deployment installed for every conversation.

    Args:
        settings: The deployment settings in force.

    Returns:
        The extensions the deployment's own directory holds.
    """
    directory = deployment_extensions_root(settings)
    if directory is None:
        return ExtensionSet()
    return _read_manifests(directory)


def merge_extension_sets(deployment: ExtensionSet, conversation: ExtensionSet) -> ExtensionSet:
    """Merge the deployment's extensions with the conversation's own.

    The deployment's extensions come first.  An extension name that both
    sides declare is taken from the conversation's own copy, because the
    conversation's scope is the narrower one.  An alias that both sides
    declare keeps the deployment's, and the refused tools and the tools that
    always need an answer are the union of both sides.

    The merged guidance is bounded, so reading the deployment's directory
    cannot grow the system prompt without a limit.

    Args:
        deployment: The extensions the deployment installed.
        conversation: The extensions the conversation's workspace holds.

    Returns:
        The extensions the conversation runs with.
    """
    replaced = {extension.name for extension in conversation.extensions}
    extensions = [
        extension for extension in deployment.extensions if extension.name not in replaced
    ]
    extensions.extend(conversation.extensions)
    aliases = dict(deployment.aliases)
    for name, alias in conversation.aliases.items():
        aliases.setdefault(name, alias)
    guidance = "\n".join(extension.guidance for extension in extensions if extension.guidance)
    return ExtensionSet(
        extensions=tuple(extensions),
        guidance=guidance[:MAX_TOTAL_GUIDANCE_CHARS],
        aliases=aliases,
        denied_tools=frozenset(deployment.denied_tools | conversation.denied_tools),
        approval_tools=frozenset(deployment.approval_tools | conversation.approval_tools),
    )


def write_extension(root: Path, document: dict[str, Any]) -> Extension:
    """Write one extension manifest into a workspace.

    Args:
        root: The workspace root.
        document: The manifest's contents.

    Returns:
        The stored extension.

    Raises:
        ExtensionError: When the manifest is malformed.
        WorkspacePathError: When the manifest cannot be placed in the
            workspace.
    """
    extension = parse_extension(document)
    resolved = resolve_within_root(root, f"{EXTENSIONS_DIRECTORY}/{extension.name}.json")
    resolved.absolute.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    if len(payload) > MAX_MANIFEST_CHARS:
        raise ExtensionError(
            f"An extension manifest may not exceed {MAX_MANIFEST_CHARS} characters."
        )
    resolved.absolute.write_text(payload, encoding="utf-8")
    return extension


def remove_extension(root: Path, name: str) -> bool:
    """Remove one extension manifest from a workspace.

    Args:
        root: The workspace root.
        name: The extension's name.

    Returns:
        True when a manifest was removed.

    Raises:
        WorkspacePathError: When the name does not resolve inside the
            workspace.
    """
    normalized = _normalize_name(name)
    resolved = resolve_within_root(root, f"{EXTENSIONS_DIRECTORY}/{normalized}.json")
    if not resolved.absolute.is_file():
        return False
    resolved.absolute.unlink()
    return True


def alias_definition(alias: ToolAlias, target: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-facing definition of one alias.

    The alias exposes the target's parameters except the ones the alias
    fixes, so the model cannot change them.  The fixed arguments are applied
    when the call runs.

    Args:
        alias: The alias to publish.
        target: The target tool's provider-facing definition.

    Returns:
        The alias's definition.
    """
    function = target.get("function") if isinstance(target, dict) else None
    if not isinstance(function, dict):
        return {}
    parameters = function.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    required = parameters.get("required") if isinstance(parameters, dict) else None
    required = [str(item) for item in required] if isinstance(required, list) else []
    fixed = set(alias.arguments)
    exposed_properties = {name: value for name, value in properties.items() if name not in fixed}
    exposed_required = [name for name in required if name not in fixed]
    return {
        "type": "function",
        "function": {
            "name": alias.name,
            "description": (
                f"Alias for the {alias.target!r} tool. " + str(function.get("description") or "")
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": exposed_properties,
                "required": exposed_required,
            },
        },
    }


def resolve_alias(alias: ToolAlias, arguments: dict[str, Any]) -> dict[str, Any]:
    """Merge one call's arguments with the alias's fixed arguments.

    Args:
        alias: The alias the model called.
        arguments: The arguments the model supplied.

    Returns:
        The arguments the target tool runs with.
    """
    return {**arguments, **alias.arguments}


def extensions_for(
    settings: Any,
    *,
    tenant_id: Any,
    user_id: Any,
    root: Path | None = None,
) -> ExtensionSet:
    """Load the extensions one conversation runs with.

    The extensions are the ones the conversation's own workspace holds, so a
    conversation that works in a named workspace runs that workspace's
    extensions.  The extensions the deployment installed are read as well,
    and they apply to every conversation.

    Args:
        settings: The deployment settings.
        tenant_id: The owning tenant.
        user_id: The owning user.
        root: The workspace the extensions are read from, when the caller
            knows it.

    Returns:
        The extensions the conversation runs with.
    """
    deployment = load_deployment_extensions(settings)
    try:
        workspace = root if root is not None else workspace_root_for(settings, tenant_id, user_id)
    except Exception:  # noqa: BLE001
        return deployment
    return merge_extension_sets(deployment, load_extensions(workspace))


__all__ = [
    "DEPLOYMENT_EXTENSIONS_DIRECTORY",
    "EXTENSIONS_DIRECTORY",
    "MAX_EXTENSIONS",
    "MAX_GUIDANCE_CHARS",
    "MAX_MANIFEST_CHARS",
    "MAX_TOTAL_GUIDANCE_CHARS",
    "Extension",
    "ExtensionError",
    "ExtensionSet",
    "ToolAlias",
    "alias_definition",
    "deployment_extensions_root",
    "extensions_for",
    "load_deployment_extensions",
    "load_extensions",
    "merge_extension_sets",
    "parse_extension",
    "remove_extension",
    "resolve_alias",
    "write_extension",
]
