"""Bind DeepSpace's existing tools and plugins into Typert contributions.

DeepSpace already declares its native tools twice: once as an
OpenAI-compatible provider definition and once as a typed Pydantic contract.
This module adds the third, protocol-facing view: a Typert contribution with
one invocable descriptor per tool, the argument model as a lazily materialized
schema, and a package reflection model a client can read.

The bridge is additive.  It does not replace the tool registry, the plugin
registry, the policy layer, or the chat executor; it only describes them on
the capability protocol.  A native tool descriptor is invocable in process
through :class:`~app.deepspace.typert.registry.TypertRegistry` when the
caller supplied an authenticated tool context in the invocation metadata.
Plugin manifests are reflected as non-invocable descriptors because a plugin
is enabled per conversation, not process-wide.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.deepspace.typert.codecs import JsonCodec
from app.deepspace.typert.descriptors import (
    RemoteContribution,
    RemoteDescriptor,
    RemoteParameter,
)
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.invocation import current_invocation
from app.deepspace.typert.reflection import (
    TypertDocumentation,
    TypertMemberModel,
    TypertObjectModel,
    TypertPackageModel,
    TypertServiceModel,
)
from app.deepspace.typert.resources import GLOBAL
from app.deepspace.typert.schema import TypertSchemaFactory

#: The package name of the process-wide native tool contribution.
NATIVE_TOOL_PACKAGE = "deepspace.tools"

#: The namespace native tool endpoints use.
NATIVE_TOOL_NAMESPACE = "tool"

#: The package name of the plugin reflection contribution.
PLUGIN_PACKAGE = "deepspace.plugins"


@dataclass(frozen=True, slots=True)
class WireTool:
    """One tool described on the capability protocol.

    Attributes:
        name: The wire method name.
        description: The model-facing description.
        parameters: The JSON Schema of the tool's arguments.
        mode: ``read``, ``write``, or ``interactive``.
        type_symbol: The portable name of the argument type.
    """

    name: str
    description: str = ""
    parameters: Mapping[str, Any] | None = None
    mode: str = "read"
    type_symbol: str = "json"

    def as_member(self, *, namespace: str) -> TypertMemberModel:
        """Return the reflection member for this tool."""
        required = []
        properties: Mapping[str, Any] = self.parameters or {}
        if isinstance(properties.get("required"), list):
            required = [str(item) for item in properties["required"]]
        signature = f"{namespace}/{self.name}({', '.join(required)}) -> json"
        return TypertMemberModel(
            name=self.name,
            kind="method",
            signature=signature,
            type_symbol=self.type_symbol,
            documentation=TypertDocumentation(summary=self.description),
        )


def wire_tools_from_native_specs(specs: Iterable[Any]) -> list[WireTool]:
    """Describe native tool specs as wire tools.

    Args:
        specs: Objects with ``name``, ``definition``, and ``arguments_model``
            attributes, such as ``NativeToolSpec`` or ``ToolSpec``.

    Returns:
        One wire tool per spec.
    """
    tools: list[WireTool] = []
    for spec in specs:
        definition = getattr(spec, "definition", None) or getattr(spec, "provider_definition", None)
        function = definition.get("function") if isinstance(definition, Mapping) else None
        if not isinstance(function, Mapping):
            raise RemoteError(
                "gateway/internal",
                f"Native tool {getattr(spec, 'name', '?')!r} has no provider definition.",
            )
        model = getattr(spec, "arguments_model", None)
        tools.append(
            WireTool(
                name=str(spec.name),
                description=str(function.get("description") or ""),
                parameters=function.get("parameters") or {},
                mode=str(getattr(spec, "mode", "read")),
                type_symbol=(
                    f"{model.__module__}.{model.__qualname__}" if model is not None else "json"
                ),
            )
        )
    return tools


def wire_tools_from_plugin_manifests(manifests: Iterable[Any]) -> list[WireTool]:
    """Describe plugin manifests as wire tools.

    Args:
        manifests: Objects with ``tools`` attributes, such as
            ``PluginManifest``.

    Returns:
        One wire tool per declared plugin tool.
    """
    tools: list[WireTool] = []
    for manifest in manifests:
        for tool in getattr(manifest, "tools", ()):
            tools.append(
                WireTool(
                    name=str(tool.name),
                    description=str(tool.description or ""),
                    parameters=tool.parameters or {},
                    mode=str(tool.mode or "read"),
                    type_symbol="json",
                )
            )
    return tools


def _native_tool_implementation(name: str) -> Callable[[dict[str, Any]], Any]:
    """Build the in-process implementation of one native tool endpoint."""

    def invoke(arguments: dict[str, Any]) -> Any:
        from app.deepspace.services.tools import NATIVE_TOOL_HANDLERS, NATIVE_TOOL_SPECS

        spec_by_name = {spec.name: spec for spec in NATIVE_TOOL_SPECS}
        spec = spec_by_name.get(name)
        if spec is None:
            raise RemoteError(
                "gateway/not-found",
                f"Native tool {name!r} is not registered.",
            )
        handler = NATIVE_TOOL_HANDLERS.get(name)
        if handler is None:
            raise RemoteError(
                "gateway/unavailable",
                f"Native tool {name!r} has no handler.",
            )
        invocation = current_invocation()
        context = None
        if invocation is not None:
            context = invocation.metadata.get("tool_context")
        if context is None:
            raise RemoteError(
                "gateway/unauthorized",
                f"Native tool {name!r} needs an authenticated tool context.",
            )
        validated = spec.arguments_model.model_validate(dict(arguments or {})).model_dump(
            exclude_none=True
        )
        return handler(validated, context)

    return invoke


def contribution_from_wire_tools(
    *,
    package: str,
    tools: Sequence[WireTool],
    namespace: str = NATIVE_TOOL_NAMESPACE,
    service: str = "tools",
    face: str = "host",
    executor: Callable[[str, dict[str, Any]], Any] | None = None,
    schema_models: Mapping[str, type[Any]] | None = None,
) -> RemoteContribution:
    """Build one atomic contribution from wire tools.

    Args:
        package: The owning package name.
        tools: The tools to describe.
        namespace: The endpoint namespace.
        service: The service key the endpoints belong to.
        face: The compiled face, ``host`` for this process.
        executor: An optional callable ``(name, arguments)`` that makes the
            endpoints invocable.  Without one the descriptors are reflection
            only.
        schema_models: Optional argument models keyed by tool name; each one
            becomes a lazy schema in the contribution.

    Returns:
        The validated contribution.
    """
    descriptors: list[RemoteDescriptor] = []
    members: list[TypertMemberModel] = []
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise RemoteError(
                "gateway/conflict",
                f"Tool {tool.name!r} is declared twice in package {package!r}.",
            )
        seen.add(tool.name)
        implementation: Callable[..., Any] | None = None
        if executor is not None:
            executor_fn: Callable[[str, dict[str, Any]], Any] = executor

            def implementation(
                arguments: dict[str, Any],
                *,
                _name: str = tool.name,
                _executor: Callable[[str, dict[str, Any]], Any] = executor_fn,
            ) -> Any:
                return _executor(_name, arguments)

        descriptors.append(
            RemoteDescriptor(
                id=f"{namespace}/{tool.name}",
                service=service,
                namespace=namespace,
                method=tool.name,
                implementation=implementation,
                mode="unary",
                parameters=(
                    RemoteParameter(
                        name="arguments",
                        codec=JsonCodec(type_symbol=tool.type_symbol or "json"),
                        source="json",
                        accepts_undefined=False,
                    ),
                ),
                result_codec=JsonCodec(type_symbol="json"),
                description=tool.description,
                resources=(GLOBAL,),
                source=f"deepspace.tools:{tool.name}",
                invocable=implementation is not None,
            )
        )
        members.append(tool.as_member(namespace=namespace))

    schemas: list[TypertSchemaFactory] = []
    for tool_name, model in (schema_models or {}).items():
        schemas.append(
            TypertSchemaFactory(
                key=f"{package}#{tool_name}Arguments",
                package=package,
                name=f"{tool_name}Arguments",
                factory=_model_factory(model),
                type_symbol=f"{model.__module__}.{model.__qualname__}",
                face=face,
            )
        )

    package_model = TypertPackageModel(
        package=package,
        face=face,
        services=(
            TypertServiceModel(
                name=service,
                documentation=TypertDocumentation(
                    summary=f"The {len(tools)} tools package {package} exposes."
                ),
                members=tuple(members),
            ),
        ),
        objects=tuple(
            TypertObjectModel(
                name=model.__name__,
                documentation=TypertDocumentation(summary=f"The argument contract of {tool_name}."),
            )
            for tool_name, model in (schema_models or {}).items()
        ),
    )
    return RemoteContribution(
        package=package,
        face=face,
        schemas=tuple(schemas),
        descriptors=tuple(descriptors),
        package_model=package_model,
    )


def contribution_from_native_specs(
    *,
    package: str = NATIVE_TOOL_PACKAGE,
    specs: Iterable[Any] | None = None,
    face: str = "host",
    executor: Callable[[str, dict[str, Any]], Any] | None = None,
) -> RemoteContribution:
    """Build the native tool contribution.

    Args:
        package: The owning package name.
        specs: The native specs; the process registry's specs are imported
            lazily when omitted.
        face: The compiled face.
        executor: An optional executor override; the native handler dispatch
            is used when omitted.

    Returns:
        The native tool contribution.
    """
    if specs is None:
        from app.deepspace.services.tools import NATIVE_TOOL_SPECS

        specs = NATIVE_TOOL_SPECS
    resolved_specs = list(specs)
    tools = wire_tools_from_native_specs(resolved_specs)
    models = {
        spec.name: spec.arguments_model
        for spec in resolved_specs
        if getattr(spec, "arguments_model", None) is not None
    }

    def execute(name: str, arguments: dict[str, Any]) -> Any:
        if executor is not None:
            return executor(name, arguments)
        return _native_tool_implementation(name)(arguments)

    return contribution_from_wire_tools(
        package=package,
        tools=tools,
        face=face,
        executor=execute,
        schema_models=models,
    )


def contribution_from_plugin_manifests(
    manifests: Iterable[Any],
    *,
    package: str = PLUGIN_PACKAGE,
    face: str = "host",
) -> list[RemoteContribution]:
    """Reflect plugin manifests as non-invocable contributions.

    A plugin is enabled per conversation, so a process-wide invocable
    descriptor would be misleading.  The reflection still gives a client the
    full tool surface the deployment can offer.  Each plugin with at least
    one tool becomes one contribution so a caller can mount and unmount it
    independently.

    Returns:
        One contribution per plugin that declares a tool.
    """
    contributions: list[RemoteContribution] = []
    for manifest in manifests:
        declared = wire_tools_from_plugin_manifests([manifest])
        if not declared:
            continue
        plugin_id = str(getattr(manifest, "id", "plugin") or "plugin")
        contributions.append(
            contribution_from_wire_tools(
                package=f"{package}.{plugin_id}",
                tools=declared,
                namespace=f"plugin.{plugin_id}",
                service="plugins",
                face=face,
            )
        )
    return contributions


def _model_factory(model: type[Any]) -> Callable[[], Any]:
    def create() -> Any:
        return model

    return create


__all__ = [
    "NATIVE_TOOL_NAMESPACE",
    "NATIVE_TOOL_PACKAGE",
    "PLUGIN_PACKAGE",
    "WireTool",
    "contribution_from_native_specs",
    "contribution_from_plugin_manifests",
    "contribution_from_wire_tools",
    "wire_tools_from_native_specs",
    "wire_tools_from_plugin_manifests",
]
