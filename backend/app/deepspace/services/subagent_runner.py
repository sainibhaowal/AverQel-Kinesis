"""The DeepSpace delegated child agent loop.

A child is a small, self-contained agent that runs inside its parent's tool
call.  It reuses the parent's provider selection, the parent's tool
executor, and the same native tool registry, so a child's tools behave
exactly like the parent's.  What it does not reuse is the parent's streaming
turn: the parent's turn produces browser-facing SSE frames, while a child only
has to produce an answer for its parent.

The loop is deliberately small.  It resolves a model, sends the task, runs
whatever tools the model asks for, and stops when the model answers or the
round budget is spent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from app.core.ids import generate_uuid7_with_fallback
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.execution_batches import plan_execution_batches
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL,
    MCP_BROKER_TOOL_NAMES,
    MCPToolBroker,
)
from app.deepspace.services.reasoning_privacy import redact_reasoning_text
from app.deepspace.services.session_events import (
    SUBAGENT_MODEL_MESSAGE,
    SUBAGENT_THINKING,
    TOOL_CALL,
    TOOL_RESULT,
    SessionEventStore,
)
from app.deepspace.services.tool_requirements import (
    requires_connected_service_action,
    requires_tool_execution,
)
from app.providers.services.base import ProviderRequestError
from app.providers.services.reasoning_capabilities import (
    supports_required_tool_choice,
    uses_deepseek_reasoning_protocol,
)
from app.providers.services.types import ChatGenerateRequest, ChatGenerateResponse

if TYPE_CHECKING:
    # The preset service imports the tool package, which imports this
    # module, so the preset types are read for typing only and the runtime
    # import happens inside the functions that need them.
    from app.deepspace.services.presets import PresetResolution
    from app.deepspace.services.tools._base import ToolContext

logger = logging.getLogger(__name__)

#: The default maximum number of provider rounds one child turn may spend.
#: Configured via ``settings.deepspace_subagent_max_rounds`` (``0`` selects
#: ``ADAPTIVE_CHILD_MAX_ROUNDS``).
MAX_CHILD_ROUNDS = 64
ADAPTIVE_CHILD_MAX_ROUNDS = 200
MAX_IDENTICAL_CHILD_TOOL_REPEATS = 3
FORCE_SUMMARY_CHILD_TOOL_REPEATS = 5
STEERING_POLL_INTERVAL_SECONDS = 0.5

#: The largest task text one delegation may carry.
MAX_TASK_CHARS = 20_000

#: The largest number of history entries a child keeps between turns.
MAX_TURN_HISTORY_EXCHANGES = 40

#: The largest answer a child may return to its parent.
MAX_RESULT_CHARS = 20_000
MAX_CHILD_IMAGE_COUNT = 10
MAX_CHILD_IMAGE_DATA_URL_CHARS = 16 * 1024 * 1024

#: The delegation tools a child never receives, so a child cannot delegate
#: further and the recursion budget stays with the root agent.
DELEGATION_TOOL_NAMES = frozenset(
    {"subagent", "subagent_fork", "send_message", "interrupt_agent", "list_agents", "get_agent"}
)

#: The tools that run a child's turn inside the caller's own call.
CHILD_TURN_TOOL_NAMES = frozenset({"subagent", "subagent_fork"})

#: The team tools a child never receives.  A delegated child is not a
#: teammate, so it neither addresses the team nor reads the team's board.
TEAM_TOOL_NAMES = frozenset(
    {
        "spawn_teammate",
        "team_send_message",
        "list_teammates",
        "team_list_members",
        "team_get_member",
        "wait_agent",
        "team_task_create",
        "team_task_list",
        "team_task_update",
    }
)

#: The tools a child never receives because a child has no channel to the
#: user.  A question asked by a child would never reach them.
CHILD_EXCLUDED_TOOL_NAMES = frozenset({"ask_user"})

#: The system prompt a child runs under.
CHILD_SYSTEM_PROMPT = (
    """You are an autonomous delegated worker inside AverQel's DeepSpace workspace.

You were started by the parent agent with a self-contained goal in your own
isolated context. Work independently, complete the exact deliverables you were
assigned using your tools directly, and report concrete verified results back
to the parent agent. You do not communicate with the user or see sibling agents'
private work.

Execution rules:
- Use your full permitted toolset directly (for example web_search, web_fetch,
  read, write, edit, glob, grep, bash) just like the main agent. When asked to
  create or update a file, actually write/edit it cleanly rather than only
  describing it in text.
- Batch independent read, search, fetch, or inspection calls in the same round
  so they execute in parallel instead of wasting rounds one by one.
- Keep writes ordered and verify key changes or outputs before declaring done.
- Do not repeat the same failing or unchanged tool call; switch strategy or
  finish with what you verified.
- Work strictly from your own tool results. Never invent tool results, file
  paths, command outputs, or URLs. When a tool is unavailable, state it plainly.
- If a wrap-up notice arrives before completion, summarize everything completed,
  exact verified evidence/paths, and any remaining steps cleanly so work can
  continue without losing progress.
""".strip()
)


@dataclass(slots=True)
class SubagentOutcome:
    """The result of one delegated child turn."""

    #: The child's answer.
    text: str
    #: Why the turn ended: ``completed``, ``error``, ``cancelled``, or
    #: ``max_rounds``.
    stop_reason: str
    #: The failure detail, when the turn did not complete cleanly.
    diagnostic: str | None = None
    #: The number of provider rounds the turn used.
    rounds: int = 0
    #: The tools the child called.
    tool_calls: int = 0
    #: The tools the child called, by name.
    called_tools: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this outcome."""
        return {
            "text": self.text,
            "stop_reason": self.stop_reason,
            "diagnostic": self.diagnostic,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "called_tools": self.called_tools,
        }


def tool_filter_for_child(child: DeepSpaceSubagentRun) -> dict[str, list[str]]:
    """Read the tool restriction a child was started with.

    Args:
        child: The durable child record.

    Returns:
        The ``allow`` and ``deny`` name lists, each possibly empty.
    """
    raw = child.tool_filter if isinstance(child.tool_filter, dict) else {}
    allow = [str(item) for item in _name_list(raw.get("allow")) if str(item)]
    deny = [str(item) for item in _name_list(raw.get("deny")) if str(item)]
    return {"allow": allow, "deny": deny}


def _name_list(value: object) -> list[object]:
    """Read one tool-name list out of a stored filter.

    Args:
        value: The stored value, which may be absent or malformed.

    Returns:
        The names, or an empty list when the value is not a list.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


#: Workspace/bridge tool names that map directly onto DeepSpace native tools.
_CHILD_TOOL_ALIASES: dict[str, str] = {
    "fs_read": "read",
    "fs_glob": "glob",
    "fs_grep": "grep",
    "fs_write": "write",
    "fs_edit": "edit",
    "fs_str_replace": "edit",
    "fs_delete": "delete",
    "fs_stat": "stat",
}


def _normalize_child_tool_names(raw_names: list[str]) -> set[str]:
    """Expand averqel__/fs_* aliases alongside explicit tool allow/deny names."""
    expanded: set[str] = set()
    for raw in raw_names:
        name = str(raw or "").strip()
        if not name:
            continue
        expanded.add(name)
        unprefixed = name[len("averqel__") :] if name.startswith("averqel__") else name
        expanded.add(unprefixed)
        mapped = _CHILD_TOOL_ALIASES.get(unprefixed) or _CHILD_TOOL_ALIASES.get(name)
        if mapped:
            expanded.add(mapped)
    return expanded


def child_tool_names(
    registry: Any,
    child: DeepSpaceSubagentRun,
    *,
    max_depth: int | None = None,
    allowed_names: set[str] | None = None,
    teammate: bool = False,
) -> list[str]:
    """List the tools one child may call.

    The delegation tools are offered only while the child is above the
    deployment's depth limit, so a root can delegate to a child that
    delegates once more without exceeding the configured recursion cap.
    The team tools are offered only to a teammate, because a delegated child
    is not on the team, and the question tool is never offered, because a
    child has no channel to the user.  The parent's agent mode narrows the
    catalogue the child starts from, and the child's own allow/deny filter
    decides what else is visible.

    Args:
        registry: The native tool registry.
        child: The durable child record.
        max_depth: The delegation depth limit.  ``None`` keeps the
            delegation tools out of every child.
        allowed_names: The tools the parent's agent mode permits, or None
            for every tool.
        teammate: Whether the child works as a named teammate of the team.

    Returns:
        The tool names the child may call, in a stable order.
    """
    restriction = tool_filter_for_child(child)
    allow = _normalize_child_tool_names(restriction["allow"])
    deny = _normalize_child_tool_names(restriction["deny"])
    can_delegate = max_depth is not None and int(child.delegation_depth or 0) < int(max_depth)
    names: list[str] = []
    for name in sorted(registry.names()):
        if name in DELEGATION_TOOL_NAMES and not can_delegate:
            continue
        if name in CHILD_EXCLUDED_TOOL_NAMES:
            continue
        if name in TEAM_TOOL_NAMES and not teammate:
            continue
        if name == "get_tool_result":
            continue
        if name == "discover_tools":
            continue
        if allowed_names is not None and name not in allowed_names:
            continue
        if allow and name not in allow:
            continue
        if name in deny:
            continue
        names.append(name)
    return names


def child_is_teammate(context: ToolContext, child: DeepSpaceSubagentRun) -> bool:
    """Report whether the child works as a named teammate of the team.

    A teammate is a continuable child with a row in the team table, and it
    reads the team's board, so it keeps the team tools.

    Args:
        context: The calling request's tool context.
        child: The durable child record.

    Returns:
        Whether the child is a teammate of the conversation's team.
    """
    db = context.db
    if db is None:
        return False
    from sqlalchemy import select

    from app.deepspace.models.agent_team import TeamMember

    try:
        found = db.execute(
            select(TeamMember.id)
            .where(
                TeamMember.child_id == child.id,
                TeamMember.tenant_id == child.tenant_id,
                TeamMember.user_id == child.user_id,
                TeamMember.conversation_id == child.conversation_id,
            )
            .limit(1)
        ).scalar()
    except Exception:  # noqa: BLE001
        logger.warning("The child's teammate record could not be read", exc_info=True)
        return False
    return found is not None


def effective_preset(context: ToolContext) -> PresetResolution | None:
    """Resolve the agent preset the conversation runs.

    The preset is a per-conversation state, recorded in the typed session
    log, so a child inherits the mode its parent runs in.

    Args:
        context: The calling request's tool context.

    Returns:
        The resolution, or None when it could not be read.
    """
    from app.deepspace.services.presets import (
        PresetNotFoundError,
        effective_preset_resolution,
        resolve_preset,
        strictest_sandbox_mode,
    )

    db = context.db
    if db is None:
        return None
    try:
        parent_resolution = effective_preset_resolution(
            db=db,
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            settings=context.settings,
        )
    except Exception:  # noqa: BLE001
        logger.warning("DeepSpace agent preset could not be read", exc_info=True)
        return None

    child_preset_id = str(context.agent_preset_override or "").strip()
    if not child_preset_id:
        return parent_resolution
    try:
        child_resolution = resolve_preset(child_preset_id, settings=context.settings)
    except (PresetNotFoundError, ValueError) as exc:
        # A malformed persisted override must fail closed instead of silently
        # reverting to the broader conversation preset.
        raise RuntimeError(f"The child agent preset {child_preset_id!r} is unavailable.") from exc

    parent_tools = parent_resolution.tool_allowlist
    child_tools = child_resolution.tool_allowlist
    if parent_tools is None:
        effective_tools = child_tools
    elif child_tools is None:
        effective_tools = parent_tools
    else:
        parent_tool_set = set(parent_tools)
        effective_tools = tuple(name for name in child_tools if name in parent_tool_set)
    spill_values = [
        value
        for value in (
            parent_resolution.spill_threshold_chars,
            child_resolution.spill_threshold_chars,
        )
        if value is not None
    ]
    return replace(
        child_resolution,
        tool_allowlist=effective_tools,
        ptc_enabled=parent_resolution.ptc_enabled and child_resolution.ptc_enabled,
        subagent_max_depth=min(
            parent_resolution.subagent_max_depth,
            child_resolution.subagent_max_depth,
        ),
        sandbox_mode=strictest_sandbox_mode(
            parent_resolution.sandbox_mode,
            child_resolution.sandbox_mode,
        ),
        reasoning_effort=(child_resolution.reasoning_effort or parent_resolution.reasoning_effort),
        spill_threshold_chars=min(spill_values) if spill_values else None,
    )


def effective_delegation_depth(context: ToolContext) -> int:
    """Return the delegation depth one conversation may use.

    The depth is the smaller of the deployment's own limit and the one the
    conversation's agent mode allows, so a mode may only narrow what the
    deployment allows.

    Args:
        context: The calling request's tool context.

    Returns:
        The depth limit.
    """
    deployment_depth = int(getattr(context.settings, "deepspace_subagent_max_depth", 1))
    resolution = effective_preset(context)
    if resolution is None:
        return deployment_depth
    return min(deployment_depth, int(resolution.subagent_max_depth))


def _parent_allowed_tools(context: ToolContext) -> set[str] | None:
    """Return the tools the conversation's agent mode permits.

    Args:
        context: The calling request's tool context.

    Returns:
        The permitted tool names, or None when the mode permits every tool.
    """
    resolution = effective_preset(context)
    if resolution is None or resolution.tool_allowlist is None:
        return None
    return set(resolution.tool_allowlist)


def _history_entries(value: object) -> list[dict[str, str]]:
    """Read a child's stored exchanges as message records.

    Args:
        value: The stored history, which may be absent or malformed.

    Returns:
        The user and assistant records, in order.
    """
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        entries.append({"role": role, "content": str(entry.get("content") or "")})
    return entries


def _effective_child_max_rounds(context: ToolContext) -> int:
    """Resolve the child round ceiling from deployment settings."""
    raw = getattr(getattr(context, "settings", None), "deepspace_subagent_max_rounds", None)
    if raw is None:
        return MAX_CHILD_ROUNDS
    value = int(raw)
    if value <= 0:
        return ADAPTIVE_CHILD_MAX_ROUNDS
    return max(1, value)


def _emit_child_activity(context: ToolContext, payload: dict[str, Any]) -> None:
    """Forward structured child timeline events when a parent sink is attached."""
    cb = getattr(context, "activity_callback", None)
    if cb is None:
        return
    with contextlib.suppress(Exception):
        cb(payload)


def _record_turn_exchange(child: DeepSpaceSubagentRun, *, task: str, answer: str) -> None:
    """Append one exchange to a child's own history.

    The history is what a continued child reads, so it holds the task the
    child was given and the answer it produced.

    Args:
        child: The durable child record.
        task: The task the child was given.
        answer: The answer the child produced.
    """
    history = list(child.turn_history or [])
    history.append({"role": "user", "content": task})
    history.append({"role": "assistant", "content": answer})
    child.turn_history = history[-MAX_TURN_HISTORY_EXCHANGES:]


class _ChildTurnCancelledError(Exception):
    """The durable child was interrupted while its provider stream was open."""


def _accumulate_streamed_tool_calls(accumulator: dict[int, dict[str, Any]], deltas: object) -> None:
    """Join provider tool-call fragments into the response shape used by the loop."""
    if isinstance(deltas, dict):
        deltas = [deltas]
    if not isinstance(deltas, list):
        return
    for position, item in enumerate(deltas):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index", position))
        except (TypeError, ValueError):
            index = position
        call = accumulator.setdefault(
            index,
            {
                "id": f"tool_{index}",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        call_id = item.get("id")
        if isinstance(call_id, str) and call_id.strip():
            call["id"] = call_id.strip()
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name.strip():
            call["function"]["name"] = name.strip()
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if isinstance(arguments, str):
            call["function"]["arguments"] += arguments
        thought_signature = item.get("thought_signature") or item.get("thoughtSignature")
        if isinstance(thought_signature, str) and thought_signature.strip():
            call["thought_signature"] = thought_signature.strip()


async def _close_provider_stream(iterator: Any, pending: asyncio.Task[Any]) -> None:
    """Cancel the current provider read and close its async stream promptly."""
    if not pending.done():
        pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    close = getattr(iterator, "aclose", None)
    if callable(close):
        with contextlib.suppress(Exception):
            await close()


async def _stream_child_provider_turn(
    provider: Any,
    request: ChatGenerateRequest,
    *,
    poll_messages: Callable[[], Awaitable[list[dict[str, Any]]]],
    cancel_check: Callable[[], bool] | None,
) -> tuple[ChatGenerateResponse | None, list[dict[str, Any]], str | None]:
    """Stream one child response, aborting it when new durable input arrives.

    Cancelling the ``anext`` task closes the provider's asynchronous transport,
    so the stale response cannot later trigger a tool call. The caller appends
    the newly claimed inbox messages and starts a fresh provider request.
    """
    stream_factory = getattr(provider, "stream_generate_events", None)
    if not callable(stream_factory):
        response = await asyncio.to_thread(provider.generate, request)
        return response, [], None

    from app.deepspace.services.xml_tool_calls import XmlToolCallStreamBuffer

    iterator = aiter(stream_factory(request))
    pending = asyncio.create_task(anext(iterator))
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    continuation_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    xml_tool_buffer = XmlToolCallStreamBuffer()
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=STEERING_POLL_INTERVAL_SECONDS)
            if not done:
                if cancel_check is not None and cancel_check():
                    await _close_provider_stream(iterator, pending)
                    raise _ChildTurnCancelledError
                incoming = await poll_messages()
                if incoming:
                    await _close_provider_stream(iterator, pending)
                    return None, incoming, None
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                incoming = await poll_messages()
                if incoming:
                    return None, incoming, None
                trailing, extracted = xml_tool_buffer.finalize(
                    start_index=(max(tool_calls.keys(), default=-1) + 1),
                    call_id_prefix="subagent_xml_call",
                )
                merged_calls = [tool_calls[index] for index in sorted(tool_calls)]
                if extracted.tool_calls:
                    merged_calls.extend(extracted.tool_calls)
                    final_content = extracted.cleaned_text
                else:
                    if trailing:
                        text_parts.append(trailing)
                    final_content = "".join(text_parts)
                return (
                    ChatGenerateResponse(
                        content=final_content,
                        thinking_content="".join(thinking_parts) or None,
                        tool_calls=merged_calls or None,
                    ),
                    [],
                    "".join(continuation_parts) or None,
                )

            if isinstance(event, str):
                safe = xml_tool_buffer.feed(event)
                if safe:
                    text_parts.append(safe)
            elif isinstance(event, dict):
                event_type = str(event.get("type") or "")
                if event_type == "provider_continuation":
                    continuation_val = event.get("reasoning_content")
                    if isinstance(continuation_val, str) and continuation_val:
                        continuation_parts.append(continuation_val)
                elif event_type in {"thinking", "reasoning", "reasoning_delta"}:
                    reasoning_val = event.get("text") or event.get("content")
                    if isinstance(reasoning_val, str) and reasoning_val:
                        thinking_parts.append(redact_reasoning_text(reasoning_val))
                else:
                    provider_reasoning = (
                        event.get("reasoning_content")
                        or event.get("reasoning")
                        or event.get("thinking")
                    )
                    if isinstance(provider_reasoning, str) and provider_reasoning:
                        thinking_parts.append(redact_reasoning_text(provider_reasoning))
                    if event_type in {"delta", "text", "content"}:
                        value = event.get("text") or event.get("content")
                        if isinstance(value, str):
                            safe = xml_tool_buffer.feed(value)
                            if safe:
                                text_parts.append(safe)
                    elif event_type in {
                        "tool_calls_delta",
                        "tool_call_delta",
                        "tool_call",
                        "tool_calls",
                        "function_call",
                    }:
                        _accumulate_streamed_tool_calls(
                            tool_calls,
                            event.get("tool_calls")
                            or event.get("tool_call")
                            or event.get("function_call"),
                        )
            pending = asyncio.create_task(anext(iterator))
    finally:
        if not pending.done():
            await _close_provider_stream(iterator, pending)


def build_child_descriptor(
    child: DeepSpaceSubagentRun,
) -> dict[str, Any]:
    """Build the versioned descriptor that makes a child resumable.

    The descriptor stores the composition a later turn needs to rebuild the
    same child: its provider, model, reasoning effort, persona, tool
    restriction, and skill visibility scope.

    Args:
        child: The durable child record.

    Returns:
        The descriptor payload.
    """
    from app.deepspace.services.subagent_descriptor import snapshot_subagent_descriptor

    return snapshot_subagent_descriptor(
        mode=child.mode,  # type: ignore[arg-type]
        provider="deepspace",
        label=child.label or "",
        agent_provider=child.agent_provider,
        agent_model=child.agent_model,
        agent_reasoning_effort=child.reasoning_effort,
        persona=child.persona,
        tool_filter=tool_filter_for_child(child) or None,
        agent_preset=(
            str(child.descriptor.get("agentPreset"))
            if isinstance(child.descriptor, dict) and child.descriptor.get("agentPreset")
            else None
        ),
        skill_scope=(
            cast(dict[str, Any], child.descriptor["skillScope"])
            if isinstance(child.descriptor, dict)
            and isinstance(child.descriptor.get("skillScope"), dict)
            else None
        ),
    )


def _system_messages(child: DeepSpaceSubagentRun) -> list[dict[str, Any]]:
    """Build the child's base system messages.

    Args:
        child: The durable child record.

    Returns:
        The system messages that precede the child's task.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": CHILD_SYSTEM_PROMPT}]
    if child.persona:
        messages.append({"role": "system", "content": child.persona})
    return messages


async def _child_system_messages(
    context: ToolContext,
    child: DeepSpaceSubagentRun,
) -> list[dict[str, Any]]:
    """Build the child's system messages, including its visible skills.

    A child resolves the same workspace registry as its parent, so without a
    scope-filtered catalog it would either not know about the skills it was
    granted or be told about skills it is not allowed to load.

    Args:
        context: The child's own tool context, carrying its skill scope.
        child: The durable child record.

    Returns:
        The system messages that precede the child's task.
    """
    from app.deepspace.services.workspace_instructions import (
        prepare_skill_catalog_message,
    )

    messages = _system_messages(child)
    catalog = prepare_skill_catalog_message(
        context.settings,
        context.auth,
        scope=getattr(context, "skill_scope", None),
        workspace_root=context.workspace_root,
    )
    if catalog is not None:
        messages.append({"role": "system", "content": str(catalog.get("content") or "")})
    return messages


def _tool_result_content(payload: Any) -> str:
    """Render one tool result as the text a provider expects.

    Args:
        payload: The tool's JSON-serializable result.

    Returns:
        The rendered content.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse a tool call's arguments.

    Args:
        raw: The raw argument string from the provider.

    Returns:
        The parsed arguments, or an empty mapping when they do not parse.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_supports_tool_calls(context: ToolContext, candidate: Any) -> bool:
    """Read known model tool capability without probing or changing providers."""
    if str(getattr(candidate, "provider_type", "")).casefold() == "manus":
        return False
    selection_service = context.provider_selection
    model_cache = getattr(selection_service, "model_cache", None)
    provider_config_id = getattr(candidate, "provider_config_id", None)
    if model_cache is not None and provider_config_id is not None:
        try:
            cached = model_cache.get_model(
                tenant_id=context.auth.tenant_id,
                provider_config_id=provider_config_id,
                model_name=candidate.model_name,
                model_kind="chat",
            )
        except Exception:  # noqa: BLE001 - stale capability metadata must not break delegation.
            cached = None
        capabilities = getattr(cached, "capabilities_json", None)
        if isinstance(capabilities, dict):
            for key in ("supports_tools", "supports_tool_use", "tool_use"):
                value = capabilities.get(key)
                if isinstance(value, bool):
                    return value
    return True


def resolve_child_candidate(
    context: ToolContext,
    child: DeepSpaceSubagentRun,
    *,
    requires_tools: bool = False,
) -> Any:
    """Resolve the provider route a child runs under.

    A child that named its own provider and model uses the first candidate
    that matches; otherwise it inherits the conversation's selected model.

    Args:
        context: The calling request's tool context.
        child: The durable child record.

    Returns:
        The selected provider candidate.

    Raises:
        RuntimeError: When no chat model is configured.
    """
    selection_service = context.provider_selection
    if selection_service is None:
        raise RuntimeError("No provider selection service is available for the child agent.")
    selection = selection_service.resolve_chat(
        tenant_id=context.auth.tenant_id,
        workspace_id=None,
        actor_user_id=context.auth.user_id,
        allow_live_model_discovery=False,
    )
    candidates = list(selection.candidates)
    if not candidates:
        raise RuntimeError("No DeepSpace chat model is configured for the child agent.")
    selected = None
    if child.agent_provider and child.agent_model:
        for candidate in candidates:
            if (
                candidate.provider_type == child.agent_provider
                and candidate.model_name == child.agent_model
            ):
                selected = candidate
                break
    if selected is None:
        selected = candidates[0]
    if not requires_tools or _candidate_supports_tool_calls(context, selected):
        return selected
    if child.agent_provider and child.agent_model:
        raise RuntimeError(
            "The child agent's selected model does not support structured tool calls."
        )
    capable = next(
        (
            candidate
            for candidate in candidates
            if _candidate_supports_tool_calls(context, candidate)
        ),
        None,
    )
    if capable is None:
        raise RuntimeError("No configured child model supports structured tool calls.")
    return capable


async def run_child_turn(
    *,
    context: ToolContext,
    child: DeepSpaceSubagentRun,
    task: str,
    inherited_history: list[dict[str, Any]] | None = None,
    tool_names: list[str] | None = None,
    images: list[str] | None = None,
    poll_messages: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
) -> SubagentOutcome:
    """Run one delegated child turn to completion.

    Args:
        context: The calling request's tool context.
        child: The durable child record.
        task: The task the child was given.
        inherited_history: The history prefix a forked child starts from.
        tool_names: The tools the child may call; resolved from the registry
            when omitted.
        images: Validated image data URLs attached to the initial child input.
        poll_messages: Optional durable inbox reader checked between provider
            rounds, allowing a queued message to steer the next round.

    Returns:
        The outcome of the turn.
    """
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY
    from app.deepspace.services.tools import unavailable_tool_names

    trimmed_task = str(task or "").strip()[:MAX_TASK_CHARS]
    if not trimmed_task:
        return SubagentOutcome(
            text="", stop_reason="error", diagnostic="The delegated task was empty."
        )
    if child.mode == "continuable":
        from app.deepspace.services.subagent_cold_resume import (
            apply_cold_resume_composition,
            cold_resume_from_db_row,
        )

        resumed = cold_resume_from_db_row(child)
        if not resumed.success:
            return SubagentOutcome(
                text="",
                stop_reason="error",
                diagnostic=resumed.error or "The child descriptor could not be restored.",
            )
        apply_cold_resume_composition(child, resumed)
    executor = context.native_executor
    if executor is None:
        return SubagentOutcome(
            text="",
            stop_reason="error",
            diagnostic="The delegated child has no tool executor.",
        )
    connected_service_tool_required = requires_connected_service_action(
        trimmed_task, dict(context.mcp_bindings)
    )
    tool_call_required = requires_tool_execution(trimmed_task) or connected_service_tool_required
    names = (
        tool_names
        if tool_names is not None
        else child_tool_names(
            NATIVE_TOOL_REGISTRY,
            child,
            max_depth=effective_delegation_depth(context),
            allowed_names=_parent_allowed_tools(context),
            teammate=child_is_teammate(context, child),
        )
    )
    names = [name for name in names if name not in unavailable_tool_names(context.settings)]
    allowed = _parent_allowed_tools(context)
    child_restriction = tool_filter_for_child(child)
    raw_child_allow = set(child_restriction["allow"])
    child_allow = _normalize_child_tool_names(child_restriction["allow"])
    child_deny = _normalize_child_tool_names(child_restriction["deny"])
    explicit_mcp_allow = any(
        name in context.mcp_bindings or "__" in name for name in raw_child_allow
    )
    can_use_mcp = (
        bool(context.mcp_bindings)
        and (allowed is None or bool(set(allowed).intersection(MCP_BROKER_TOOL_NAMES)))
        and (not child_allow or MCP_BROKER_TOOL in child_allow or explicit_mcp_allow)
        and MCP_BROKER_TOOL not in child_deny
    )
    if tool_names is not None:
        can_use_mcp = can_use_mcp and MCP_BROKER_TOOL in set(tool_names)
    if can_use_mcp and MCP_BROKER_TOOL not in names:
        names.append(MCP_BROKER_TOOL)
    if not context.web_search_options:
        names = [name for name in names if name != "web_search"]
    definitions = [
        NATIVE_TOOL_REGISTRY.emit(name) for name in names if name not in MCP_BROKER_TOOL_NAMES
    ]
    if can_use_mcp:
        definitions.extend(MCPToolBroker.compact_definitions())
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, dict) else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if (
            isinstance(function, dict)
            and function.get("name") == "web_search"
            and isinstance(properties, dict)
            and isinstance(properties.get("provider"), dict)
        ):
            properties["provider"]["enum"] = sorted(context.web_search_options)
    messages: list[dict[str, Any]] = await _child_system_messages(context, child)
    for entry in inherited_history or []:
        if isinstance(entry, dict) and entry.get("role") in {"user", "assistant", "system"}:
            messages.append({"role": entry["role"], "content": str(entry.get("content") or "")})
    # A continued child keeps its own earlier exchanges, so a follow-up turn
    # answers in the context the child already built.
    for entry in _history_entries(child.turn_history):
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": trimmed_task})
    task_parts = [trimmed_task]
    request_images: list[str] = []
    image_chars = 0

    def add_images(values: object) -> None:
        nonlocal image_chars
        if not isinstance(values, list):
            return
        for value in values:
            if not isinstance(value, str) or not value.startswith("data:image/"):
                continue
            if len(request_images) >= MAX_CHILD_IMAGE_COUNT:
                break
            if image_chars + len(value) > MAX_CHILD_IMAGE_DATA_URL_CHARS:
                break
            request_images.append(value)
            image_chars += len(value)

    def append_incoming_messages(incoming_messages: list[dict[str, Any]]) -> None:
        for incoming in incoming_messages:
            content = str(incoming.get("message") or "").strip()
            incoming_images = incoming.get("images")
            if not content and not incoming_images:
                continue
            remaining_chars = max(0, MAX_TASK_CHARS - sum(map(len, task_parts)))
            content = content[:remaining_chars] if remaining_chars else ""
            if not content and incoming_images:
                content = "Please inspect the attached image."
            if not content:
                continue
            messages.append({"role": "user", "content": content})
            task_parts.append(content)
            add_images(incoming_images)

    add_images(images)
    try:
        candidate = resolve_child_candidate(context, child, requires_tools=tool_call_required)
    except RuntimeError as exc:
        return SubagentOutcome(text="", stop_reason="error", diagnostic=str(exc))
    try:
        provider = context.provider_registry.get_chat_provider_from_selection(candidate)
    except Exception as exc:  # noqa: BLE001
        return SubagentOutcome(
            text="",
            stop_reason="error",
            diagnostic=f"The child's chat provider could not be initialized: {exc}",
        )
    provider_supports_tools = bool(getattr(provider, "supports_tool_calling", True))
    if tool_call_required and (not definitions or not provider_supports_tools):
        reason = (
            "The selected child model does not support structured tool calls."
            if not provider_supports_tools
            else "The child has no tools permitted for this task."
        )
        return SubagentOutcome(
            text="",
            stop_reason="error",
            diagnostic=f"{reason} No live check was performed.",
        )
    called_tools: list[str] = []
    repeated_call_counts: dict[str, int] = {}
    effective_max_rounds = _effective_child_max_rounds(context)
    force_final_summary = False
    rounds = 0
    answer = ""
    tool_call_recovery_retries = 0
    tool_requirement_satisfied = False
    _emit_child_activity(
        context,
        {
            "event": "subagent_activity",
            "kind": "start",
            "subagent_id": str(child.id),
            "label": child.label,
            "mode": child.mode,
            "mission": trimmed_task[:500],
            "depth": int(child.delegation_depth or 1),
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    while rounds < effective_max_rounds:
        rounds += 1
        if context.cancel_check is not None and context.cancel_check():
            if answer.strip():
                _record_turn_exchange(
                    child, task="\n\n".join(task_parts), answer=answer[:MAX_RESULT_CHARS]
                )
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "end",
                    "subagent_id": str(child.id),
                    "label": child.label,
                    "mode": child.mode,
                    "status": "cancelled",
                    "stop_reason": "cancelled",
                    "rounds": rounds,
                    "tool_calls": len(called_tools),
                    "summary": answer[:8000],
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
            return SubagentOutcome(
                text=answer[:MAX_RESULT_CHARS],
                stop_reason="cancelled",
                diagnostic="The delegated child was cancelled.",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        if poll_messages is not None:
            append_incoming_messages(await poll_messages())
        supports_live_steering = poll_messages is not None and callable(
            getattr(provider, "stream_generate_events", None)
        )
        allow_tools_this_round = (
            bool(definitions)
            and not force_final_summary
            and (rounds < effective_max_rounds or effective_max_rounds == 1)
        )
        if force_final_summary or (effective_max_rounds > 1 and rounds == effective_max_rounds):
            allow_tools_this_round = False
        raw_effort = str(child.reasoning_effort or "").strip() or None
        child_reasoning_effort = (
            None
            if raw_effort is None or raw_effort.lower() in {"auto", "off", "none"}
            else raw_effort
        )
        child_reasoning_enabled = (raw_effort.lower() != "off" if raw_effort else False) or bool(
            getattr(context, "thinking_enabled", False)
        )
        request = ChatGenerateRequest(
            model=candidate.model_name,
            messages=messages,
            temperature=0.2,
            max_tokens=None,
            base_url=candidate.base_url or "",
            api_key=candidate.api_key,
            stream=supports_live_steering,
            reasoning_enabled=child_reasoning_enabled,
            reasoning_effort=child_reasoning_effort,
            images=list(request_images) or None,
            tools=(definitions if allow_tools_this_round else None),
            tool_choice=(
                "required"
                if allow_tools_this_round
                and tool_call_required
                and not tool_requirement_satisfied
                and supports_required_tool_choice(candidate.provider_type, candidate.model_name)
                else ("auto" if allow_tools_this_round else None)
            ),
        )
        provider_continuation: str | None = None
        try:
            if supports_live_steering:
                assert poll_messages is not None
                response, steered_messages, provider_continuation = (
                    await _stream_child_provider_turn(
                        provider,
                        request,
                        poll_messages=poll_messages,
                        cancel_check=context.cancel_check,
                    )
                )
                if steered_messages:
                    append_incoming_messages(steered_messages)
                    continue
                if response is None:
                    raise RuntimeError("The child provider stream ended without a response.")
            else:
                response = await asyncio.to_thread(provider.generate, request)
        except _ChildTurnCancelledError:
            return SubagentOutcome(
                text=answer,
                stop_reason="cancelled",
                diagnostic="The delegated child was cancelled.",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        except ProviderRequestError as exc:
            return SubagentOutcome(
                text=answer,
                stop_reason="error",
                diagnostic=f"The child's provider request failed: {exc}",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("DeepSpace subagent provider request failed")
            return SubagentOutcome(
                text=answer,
                stop_reason="error",
                diagnostic=f"The child's provider request failed: {exc}",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        if response is None:
            return SubagentOutcome(
                text=answer,
                stop_reason="error",
                diagnostic="The child's provider returned no response.",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        answer = response.content or ""
        raw_thinking = str(getattr(response, "thinking_content", None) or "").strip()
        redacted_thinking = redact_reasoning_text(raw_thinking).strip() if raw_thinking else ""
        if redacted_thinking:
            think_step_id = f"child_{child.id}_think_r{rounds}"
            bounded_thinking = redacted_thinking[:8000]
            _record_child_tool_event(
                context,
                child,
                event_type=SUBAGENT_THINKING,
                tool_name="",
                payload={
                    "step_id": think_step_id,
                    "round": rounds,
                    "text": bounded_thinking,
                },
            )
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "thinking",
                    "subagent_id": str(child.id),
                    "label": child.label,
                    "mode": child.mode,
                    "round": rounds,
                    "step_id": think_step_id,
                    "text": bounded_thinking,
                    "status": "completed",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        tool_calls = response.tool_calls or []
        if not tool_calls and answer:
            from app.deepspace.services.xml_tool_calls import extract_inline_tool_calls

            extracted = extract_inline_tool_calls(answer, call_id_prefix="subagent_xml_call")
            if extracted.tool_calls:
                tool_calls = extracted.tool_calls
                answer = extracted.cleaned_text
        if not tool_calls:
            if tool_call_required and not tool_requirement_satisfied:
                if tool_call_recovery_retries < 1:
                    tool_call_recovery_retries += 1
                    if answer:
                        messages.append({"role": "assistant", "content": answer})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "This delegated task requires an actual structured tool call. "
                                "Use the relevant tool from the supplied schemas now; do not "
                                "claim the check was done without a tool result."
                            ),
                        }
                    )
                    continue
                answer = "No live check was performed because the child model did not call a supplied tool."
                _record_turn_exchange(child, task="\n\n".join(task_parts), answer=answer)
                _emit_child_activity(
                    context,
                    {
                        "event": "subagent_activity",
                        "kind": "end",
                        "subagent_id": str(child.id),
                        "label": child.label,
                        "mode": child.mode,
                        "status": "failed",
                        "stop_reason": "error",
                        "rounds": rounds,
                        "tool_calls": len(called_tools),
                        "summary": answer[:1000],
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return SubagentOutcome(
                    text=answer[:MAX_RESULT_CHARS],
                    stop_reason="error",
                    diagnostic="No tool was called after one correction attempt.",
                    rounds=rounds,
                    tool_calls=len(called_tools),
                    called_tools=called_tools,
                )
            _record_turn_exchange(child, task="\n\n".join(task_parts), answer=answer)
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "end",
                    "subagent_id": str(child.id),
                    "label": child.label,
                    "mode": child.mode,
                    "status": "completed",
                    "stop_reason": "completed",
                    "rounds": rounds,
                    "tool_calls": len(called_tools),
                    "summary": answer[:8000],
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
            return SubagentOutcome(
                text=answer[:MAX_RESULT_CHARS],
                stop_reason="completed",
                rounds=rounds,
                tool_calls=len(called_tools),
                called_tools=called_tools,
            )
        if answer.strip():
            msg_step_id = f"child_{child.id}_msg_r{rounds}"
            bounded_msg = answer.strip()[:8000]
            _record_child_tool_event(
                context,
                child,
                event_type=SUBAGENT_MODEL_MESSAGE,
                tool_name="",
                payload={
                    "step_id": msg_step_id,
                    "round": rounds,
                    "text": bounded_msg,
                },
            )
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "model_message",
                    "subagent_id": str(child.id),
                    "label": child.label,
                    "mode": child.mode,
                    "round": rounds,
                    "step_id": msg_step_id,
                    "text": bounded_msg,
                    "status": "completed",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": answer,
            "tool_calls": tool_calls,
        }
        if uses_deepseek_reasoning_protocol(candidate.provider_type, candidate.model_name) and (
            provider_continuation or raw_thinking
        ):
            assistant_message["reasoning_content"] = provider_continuation or raw_thinking
        messages.append(assistant_message)
        results_by_index: dict[int, dict[str, Any]] = {}
        valid_items: list[dict[str, Any]] = []
        round_max_repeats = 0
        for idx, call in enumerate(tool_calls):
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            call_id = str(call.get("id") or f"call-{uuid.uuid4()}")
            if name not in names:
                results_by_index[idx] = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"The tool '{name}' is not available to this agent.",
                }
                continue
            arguments = _parse_arguments(function.get("arguments"))
            called_tools.append(name)
            call_signature = (
                f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"
            )
            repeat_count = repeated_call_counts.get(call_signature, 0) + 1
            repeated_call_counts[call_signature] = repeat_count
            round_max_repeats = max(round_max_repeats, repeat_count)
            if (
                name != MCP_BROKER_TOOL
                or not connected_service_tool_required
                or str(arguments.get("operation") or "") in {"call", "result"}
            ):
                tool_requirement_satisfied = True
            # The child's own tool calls are recorded in the typed log of
            # the conversation they belong to, so a child's trajectory is as
            # complete as its parent's.
            _record_child_tool_event(
                context,
                child,
                event_type=TOOL_CALL,
                tool_name=name,
                payload={"call_id": call_id, "arguments": _summarize_arguments(arguments)},
            )
            step_id = f"child_{child.id}_r{rounds}_{idx}"
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "tool_start",
                    "subagent_id": str(child.id),
                    "label": child.label,
                    "mode": child.mode,
                    "round": rounds,
                    "step_id": step_id,
                    "tool_id": call_id,
                    "tool_name": name,
                    "tool_input": _summarize_arguments(arguments),
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )
            valid_items.append(
                {
                    "index": idx,
                    "step_id": step_id,
                    "call_id": call_id,
                    "tool_name": name,
                    "arguments": arguments,
                }
            )

        def _is_child_call_parallel_safe(item: dict[str, Any]) -> bool:
            tool_name = str(item.get("tool_name") or "")
            args = item.get("arguments") or {}
            try:
                from app.deepspace.services.host_access import is_known_read_only_command
                from app.deepspace.services.tools import (
                    NATIVE_CONCURRENCY_SAFE_TOOLS,
                    NATIVE_TOOL_MODES,
                )

                mode = NATIVE_TOOL_MODES.get(tool_name)
                if mode is None:
                    return False
                if mode == "read" or tool_name in NATIVE_CONCURRENCY_SAFE_TOOLS:
                    return True
                if tool_name in {"bash", "pwsh"} and isinstance(args, dict):
                    return is_known_read_only_command(str(args.get("command") or ""))
            except Exception:  # noqa: BLE001
                return False
            return False

        async def _run_child_item(
            item: dict[str, Any],
            semaphore: asyncio.Semaphore | None,
            results_map: dict[int, dict[str, Any]] = results_by_index,
            round_num: int = rounds,
            child_id_str: str = str(child.id),
            child_label: str | None = child.label,
            child_mode: str = child.mode,
        ) -> None:
            name = str(item["tool_name"])
            call_id = str(item["call_id"])
            step_id = str(item.get("step_id") or call_id)
            arguments = item["arguments"]
            idx = int(item["index"])

            async def _invoke() -> Any:
                return await executor(name, arguments)

            started = time.monotonic()
            failure: str | None = None
            try:
                if semaphore is not None:
                    async with semaphore:
                        result = await _invoke()
                else:
                    result = await _invoke()
            except Exception as exc:  # noqa: BLE001
                failure = f"The tool '{name}' failed: {exc}"
                result = {"status": "error", "message": failure}
            duration_ms = int((time.monotonic() - started) * 1000)
            serialized_content = _tool_result_content(result)
            _record_child_tool_event(
                context,
                child,
                event_type=TOOL_RESULT,
                tool_name=name,
                payload=(
                    {
                        "call_id": call_id,
                        "status": "failed",
                        "error": failure,
                        "duration_ms": duration_ms,
                        "output_preview": serialized_content[:2000],
                    }
                    if failure is not None
                    else {
                        "call_id": call_id,
                        "status": "completed",
                        "duration_ms": duration_ms,
                        "output_preview": serialized_content[:2000],
                    }
                ),
            )
            if context.progress_callback is not None:
                with contextlib.suppress(Exception):
                    status_label = "failed" if failure is not None else "done"
                    context.progress_callback(
                        f"Round {round_num}: {name} ({status_label}, {duration_ms}ms)"
                    )
            _emit_child_activity(
                context,
                {
                    "event": "subagent_activity",
                    "kind": "tool_result",
                    "subagent_id": child_id_str,
                    "label": child_label,
                    "mode": child_mode,
                    "round": round_num,
                    "step_id": step_id,
                    "tool_id": call_id,
                    "tool_name": name,
                    "status": "failed" if failure is not None else "completed",
                    "duration_ms": duration_ms,
                    "error": failure,
                    "output_preview": serialized_content[:1500],
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
            results_map[idx] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": serialized_content,
            }

        max_parallel = max(
            1,
            min(
                int(
                    getattr(getattr(context, "settings", None), "deepspace_max_concurrent_tools", 8)
                ),
                32,
            ),
        )
        read_semaphore = asyncio.Semaphore(max_parallel)
        for batch in plan_execution_batches(valid_items, is_read=_is_child_call_parallel_safe):
            if batch.parallel and len(batch.calls) > 1:
                if context.progress_callback is not None:
                    with contextlib.suppress(Exception):
                        batch_names = ", ".join(str(item["tool_name"]) for item in batch.calls)
                        context.progress_callback(
                            f"Round {rounds}: running {batch_names} in parallel"
                        )
                await asyncio.gather(
                    *(_run_child_item(item, read_semaphore) for item in batch.calls)
                )
            else:
                if context.progress_callback is not None:
                    with contextlib.suppress(Exception):
                        context.progress_callback(
                            f"Round {rounds}: running {batch.calls[0]['tool_name']}"
                        )
                await _run_child_item(batch.calls[0], None)

        for idx in sorted(results_by_index):
            messages.append(results_by_index[idx])

        if round_max_repeats >= FORCE_SUMMARY_CHILD_TOOL_REPEATS:
            force_final_summary = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Anti-loop guard: identical tool calls were repeated without progress. "
                        "Do not call more tools. Summarize everything completed and verified now."
                    ),
                }
            )
        elif round_max_repeats >= MAX_IDENTICAL_CHILD_TOOL_REPEATS:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Notice: an identical tool call was repeated multiple times. Switch strategy, "
                        "inspect already-gathered outputs, or return your verified final answer."
                    ),
                }
            )
        if effective_max_rounds > 1 and rounds == effective_max_rounds - 1:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Final round budget notice: do not start new work. Deliver a complete summary "
                        "of everything completed, exact file paths/evidence produced, and any remaining "
                        "steps so the parent or a follow-up turn can continue seamlessly."
                    ),
                }
            )

    if answer.strip():
        _record_turn_exchange(child, task="\n\n".join(task_parts), answer=answer[:MAX_RESULT_CHARS])
    _emit_child_activity(
        context,
        {
            "event": "subagent_activity",
            "kind": "end",
            "subagent_id": str(child.id),
            "label": child.label,
            "mode": child.mode,
            "status": "failed",
            "stop_reason": "max_rounds",
            "rounds": rounds,
            "tool_calls": len(called_tools),
            "summary": answer[:1000],
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    return SubagentOutcome(
        text=answer[:MAX_RESULT_CHARS],
        stop_reason="max_rounds",
        diagnostic=f"The child used its {effective_max_rounds} round budget without answering.",
        rounds=rounds,
        tool_calls=len(called_tools),
        called_tools=called_tools,
    )


#: The status a child record carries before it has worked.
#:
#: A child that was delegated runs at once, so it starts as ``running``.  A
#: child that waits for a message starts as ``idle``, because it exists and
#: is not working.
CHILD_STATUS_RUNNING = "running"

#: The status a child record carries while it waits for work.
#:
#: The record carries the child's work state, not the state of the work it
#: was given.  A child that finished its last turn waits, so its record says
#: ``idle``, and the product reports ``completed`` for it.
CHILD_STATUS_IDLE = "idle"

#: The status the product reports for a child whose last turn completed.
CHILD_STATUS_COMPLETED = "completed"


def child_reported_status(child: DeepSpaceSubagentRun) -> str:
    """Return the status the product reports for one child.

    A child whose last turn completed is finished, whatever the work state
    its record carries, so the product reports ``completed`` for it.  A child
    that is working reports ``running``, and a child whose turn ended in a
    failure reports that reason.

    Args:
        child: The durable child record.

    Returns:
        The status the product reports.
    """
    if (
        getattr(child, "status", None) == CHILD_STATUS_IDLE
        and getattr(child, "stop_reason", None) == CHILD_STATUS_COMPLETED
    ):
        return CHILD_STATUS_COMPLETED
    return str(getattr(child, "status", None) or CHILD_STATUS_IDLE)


def _record_child_tool_event(
    context: ToolContext,
    child: DeepSpaceSubagentRun,
    *,
    event_type: str,
    tool_name: str,
    payload: dict[str, Any],
) -> None:
    """Append one event for a child's own tool call to the typed log.

    The typed log records every tool call, whatever the outcome, so a
    child's trajectory is as complete as its parent's.  A failure to append
    must never fail the child's turn, so a storage problem is reported and
    the turn continues.

    Args:
        context: The calling request's tool context.
        child: The durable child record.
        event_type: The event name.
        tool_name: The tool the event concerns.
        payload: The event's data.
    """
    db = context.db
    if db is None:
        return
    try:
        SessionEventStore(db).append(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            event_type=event_type,
            payload=payload,
            run_id=context.run_id,
            subagent_id=child.id,
            turn=child.delegation_depth,
            tool_name=tool_name,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("DeepSpace child tool event rollback failed", exc_info=True)
        logger.warning("DeepSpace child tool event append failed", exc_info=True)


def _summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Reduce a tool call's arguments to a short, storable summary.

    Args:
        arguments: The validated tool arguments.

    Returns:
        The shortened arguments.
    """
    from app.deepspace.services.chat_service import summarize_tool_arguments

    return summarize_tool_arguments(arguments)


def new_child_record(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    parent_run_id: uuid.UUID | None,
    parent_agent_id: uuid.UUID | None,
    origin: str,
    mode: str,
    label: str,
    delegation_depth: int,
    agent_provider: str | None,
    agent_model: str | None,
    reasoning_effort: str | None,
    persona: str | None,
    tool_filter: dict[str, list[str]],
    inherited_history: list[dict[str, Any]] | None = None,
    status: str = CHILD_STATUS_RUNNING,
) -> DeepSpaceSubagentRun:
    """Build the durable record for a new child.

    The record is what the subagent worker picks a child up from, so it
    carries the parent's link, the child's depth below the root agent, the
    composition the child runs under, and the status that says whether the
    child is working.

    Args:
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation the child belongs to.
        parent_run_id: The run that started the child.
        parent_agent_id: The immediate parent child.
        origin: ``subagent`` or ``fork``.
        mode: ``one-shot`` or ``continuable``.
        label: The short description of the delegation.
        delegation_depth: The child's depth below the root agent.
        agent_provider: The provider route the child runs under.
        agent_model: The model the child runs under.
        reasoning_effort: The reasoning effort the child runs under.
        persona: The per-child persona.
        tool_filter: The tool restriction for the child.
        inherited_history: The history prefix a forked child starts from.
        status: The status the child starts in.

    Returns:
        The unsaved child record.
    """
    child = DeepSpaceSubagentRun(
        id=generate_uuid7_with_fallback(),
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        parent_agent_id=parent_agent_id,
        origin=origin,
        mode=mode,
        label=label[:255],
        delegation_depth=delegation_depth,
        agent_provider=agent_provider,
        agent_model=agent_model,
        reasoning_effort=reasoning_effort,
        persona=persona,
        tool_filter=dict(tool_filter),
        inherited_history=list(inherited_history or []),
        status=status,
    )
    child.descriptor = build_child_descriptor(child)
    return child
