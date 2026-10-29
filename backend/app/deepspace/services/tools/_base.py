"""Shared plumbing for DeepSpace's native tool modules.

Each tool family lives in its own module under this package and contributes
four things: the provider-facing definition, the typed argument model the
registry validates against, the execution mode the policy reads, and the
handler that runs the call.  The package assembles them into one registry
input and one dispatch table, so a tool name is declared in exactly one place
per concern and cannot drift between the schema, the validator, the policy,
and the executor.

The dispatch returns ``None`` for a name this package does not own, which
lets the caller's existing tool chain continue unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tool_result_store import ToolResultStore

#: The execution mode a tool declares to the policy layer.
ToolMode = Literal["read", "write", "interactive"]

#: The tool's provider-facing definition, ready for a provider request.
ToolDefinition = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a native tool handler needs from its owning request.

    The context is created once per tool call, so a handler never reaches
    back into the chat service for request state.  ``depth`` carries the
    delegation depth of the agent that issued the call, which the delegation
    tools compare against the configured maximum.
    """

    #: The authenticated tenant and user that own the call.
    auth: AuthContext
    #: The conversation the call belongs to.
    conversation_id: uuid.UUID
    #: The deployment settings in force for this request.
    settings: Settings
    #: The request's database session, absent on a partial runtime.
    db: Session | None
    #: The durable task ledger and Library file store.
    task_store: Any
    #: The opaque-reference store for oversized model-visible results.
    tool_result_store: ToolResultStore | None
    #: The provider registry a child agent drives.
    provider_registry: Any
    #: The provider selection service a child agent resolves its model from.
    provider_selection: Any = None
    #: The executor that runs one native tool call on behalf of a caller.
    native_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    #: The deployment's tool policy, used by a tool that calls other tools.
    tool_policy: Any = None
    #: The sandbox mode this conversation runs in, when it differs from the
    #: deployment's configured mode.
    sandbox_mode: str | None = None
    #: The resolved root of the workspace this conversation works in.  A
    #: tool that writes files prefers it over the account's default
    #: workspace.
    workspace_root: Path | None = None
    #: The durable run that owns the call, when the caller has one.
    run_id: uuid.UUID | None = None
    #: The parent agent identity for a delegated call.
    parent_agent_id: uuid.UUID | None = None
    #: The delegation depth of the calling agent; a root agent is depth 0.
    depth: int = 0
    #: The current run's cancellation probe, consulted by long-running tools.
    cancel_check: Callable[[], bool] | None = None


#: A tool handler: validated arguments in, a JSON-serializable result out.
ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class NativeToolSpec:
    """One native tool: its schema, its validator, its mode, and its handler."""

    name: str
    definition: ToolDefinition
    arguments_model: type[ToolArguments]
    mode: ToolMode
    handler: ToolHandler
    #: The wall-clock budget the tool declares for itself, in seconds.  A
    #: tool that declares one runs under that budget instead of the
    #: deployment's default.
    deadline_seconds: float | None = None


def tool_definition(
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
) -> ToolDefinition:
    """Build one OpenAI-compatible function definition.

    Every native tool uses the same envelope, so building it here keeps the
    schema shape uniform across the package and keeps the
    ``additionalProperties`` refusal that stops a model from sending
    undeclared arguments.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": dict(properties),
                "required": list(required),
            },
        },
    }


def define_native_tool(
    *,
    name: str,
    description: str,
    properties: Mapping[str, Any],
    arguments_model: type[ToolArguments],
    mode: ToolMode,
    handler: ToolHandler,
    required: Sequence[str] = (),
    deadline_seconds: float | None = None,
) -> NativeToolSpec:
    """Bind one tool's schema, validator, mode, and handler into a spec.

    Args:
        name: The tool name.
        description: The provider-facing description.
        properties: The provider-facing parameters.
        arguments_model: The typed validator.
        mode: The execution mode the policy reads.
        handler: The tool's implementation.
        required: The parameters the model must send.
        deadline_seconds: The wall-clock budget the tool declares for itself,
            in seconds.  A tool that declares one runs under that budget
            instead of the deployment's default.

    Returns:
        The tool's spec.
    """
    return NativeToolSpec(
        name=name,
        definition=tool_definition(name, description, properties, required),
        arguments_model=arguments_model,
        mode=mode,
        handler=handler,
        deadline_seconds=deadline_seconds,
    )


def require_db(context: ToolContext) -> Session:
    """Return the request's database session.

    Args:
        context: The calling request's tool context.

    Returns:
        The database session.

    Raises:
        RuntimeError: When the runtime has no database session.
    """
    if context.db is None:
        raise RuntimeError("This tool needs a database session.")
    return context.db


def string_property(
    description: str,
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    enum: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build one string property for a tool schema."""
    prop: dict[str, Any] = {"type": "string", "description": description}
    if min_length is not None:
        prop["minLength"] = min_length
    if max_length is not None:
        prop["maxLength"] = max_length
    if enum is not None:
        prop["enum"] = list(enum)
    return prop


def integer_property(
    description: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    """Build one integer property for a tool schema."""
    prop: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    return prop


def number_property(
    description: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> dict[str, Any]:
    """Build one number property for a tool schema."""
    prop: dict[str, Any] = {"type": "number", "description": description}
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    return prop


def boolean_property(description: str) -> dict[str, Any]:
    """Build one boolean property for a tool schema."""
    return {"type": "boolean", "description": description}


def array_property(
    description: str,
    item_type: str = "string",
    *,
    min_items: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Build one array property for a tool schema."""
    prop: dict[str, Any] = {
        "type": "array",
        "description": description,
        "items": {"type": item_type},
    }
    if min_items is not None:
        prop["minItems"] = min_items
    if max_items is not None:
        prop["maxItems"] = max_items
    return prop


def object_property(
    description: str,
    *,
    additional_properties: bool = True,
) -> dict[str, Any]:
    """Build one object property for a tool schema.

    Args:
        description: The property's description.
        additional_properties: Whether the object accepts properties the
            schema does not declare.

    Returns:
        The property.
    """
    return {
        "type": "object",
        "description": description,
        "additionalProperties": additional_properties,
    }
