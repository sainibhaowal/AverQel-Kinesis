"""Delegation tools: DeepSpace's multi-agent surface.

A root agent can start a child, and a child can be continued, stopped, and
listed.  Two delegation shapes exist and they differ in what the child
inherits:

``subagent``
    A fresh child with its own context.  It answers the task on its own and
    can be continued afterwards with ``send_message``.

``subagent_fork``
    A child that inherits the parent's recent history, so it can act on the
    conversation the parent is already in.  It answers once and ends.

Both honour the deployment's depth limit, so a root agent cannot start a
child that starts another child unless the deployment allows it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import func, select, update

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.services.session_events import (
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_END,
    SUBAGENT_PRESET,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SUBAGENT_START,
    SessionEventStore,
)
from app.deepspace.services.subagent_lifecycle import (
    SubagentResult as LifecycleResult,
)
from app.deepspace.services.subagent_lifecycle import (
    SubagentRunEndInfo,
    SubagentRunInfo,
    create_activation_observer,
    create_lifecycle_emitter,
)
from app.deepspace.services.subagent_providers import (
    SubagentResult as ProviderResult,
)
from app.deepspace.services.subagent_providers import (
    SubagentRunSpec,
    get_provider,
)
from app.deepspace.services.subagent_runner import (
    CHILD_STATUS_COMPLETED,
    build_child_descriptor,
    child_reported_status,
    child_tool_names,
    effective_delegation_depth,
    new_child_record,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    boolean_property,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)
from app.platform.database.session import managed_db_session, set_db_tenant_context

#: The largest number of turns a continuable child may accumulate.
MAX_CHILD_TURNS = 50

#: The largest number of messages one child may have queued.
MAX_PENDING_MESSAGES = 20

logger = logging.getLogger(__name__)

#: The largest number of children one conversation may hold at once.
DEFAULT_MAX_CHILDREN = 8

# ``list_agents.revision`` is a coordination cursor, not a conversation
# cursor. Parent tool-call/result events must not make an unchanged child
# roster look new to a polling model.
DELEGATION_PROGRESS_EVENT_TYPES = frozenset(
    {
        SUBAGENT_DESCRIPTOR,
        SUBAGENT_END,
        SUBAGENT_PRESET,
        SUBAGENT_RESULT,
        SUBAGENT_SPAWN,
        SUBAGENT_START,
    }
)


class SubagentArguments(ToolArguments):
    """Delegate a self-contained task to a child agent."""

    description: str = Field(min_length=1, max_length=255)
    task: str = Field(min_length=1, max_length=20_000)
    mode: Literal["one-shot", "continuable"] = "continuable"
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    persona: str | None = Field(default=None, max_length=4_000)
    agent_preset: str | None = Field(default=None, max_length=100)
    tool_allow: list[str] | None = Field(default=None, max_length=64)
    tool_deny: list[str] | None = Field(default=None, max_length=64)
    # Out-of-process backend selection
    backend: Literal["in-process", "acp", "claude-code", "codex", "dsh-sdk"] | None = None
    backend_config: dict[str, Any] | None = None


class SubagentForkArguments(SubagentArguments):
    """Delegate a task to a child that inherits the parent's history."""

    mode: Literal["one-shot"] = "one-shot"
    history_messages: int | None = Field(default=None, ge=0, le=40)


class SendMessageArguments(ToolArguments):
    """Send a message to a continuable child agent."""

    agent_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=20_000)
    attachment_file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class InterruptAgentArguments(ToolArguments):
    """Stop the current turn of a running child agent."""

    agent_id: str = Field(min_length=1, max_length=64)


class ListAgentsArguments(ToolArguments):
    """List the child agents of the current conversation."""

    include_completed: bool = True


class GetAgentArguments(ToolArguments):
    """Read or wait briefly for the detailed result of one child agent."""

    agent_id: str = Field(min_length=1, max_length=64)
    wait_timeout_seconds: int = Field(default=0, ge=0, le=90)


def _dispatch_child_turn(
    context: ToolContext,
    *,
    child: DeepSpaceSubagentRun,
) -> bool:
    """Dispatch one turn for a child that has just received a message.

    The turn runs in the worker, so it survives the parent's turn and works
    for a child that was continued in a later process.  A dispatch failure is
    reported and the message stays queued, so the child still runs it on its
    next turn.

    Args:
        context: The calling request's tool context.
        child: The child that received the message.

    Returns:
        Whether the turn was dispatched.
    """
    try:
        from app.deepspace.workers.subagent_tasks import run_subagent_turn_task
        from app.platform.worker.celery_app import celery_app

        # The message is already committed to the child row. Passing a second
        # copy in the broker payload could make duplicate deliveries execute
        # the same work twice after a retry.
        payload = {
            "tenant_id": str(context.auth.tenant_id),
            "user_id": str(context.auth.user_id),
            "conversation_id": str(context.conversation_id),
            "child_id": str(child.id),
            "activation_run_id": str(uuid.uuid4()),
            "roles": sorted(str(role) for role in (getattr(context.auth, "roles", ()) or ())),
            "permissions": sorted(
                str(permission) for permission in (getattr(context.auth, "permissions", ()) or ())
            ),
        }
        if bool(getattr(celery_app.conf, "task_always_eager", False)):
            # Celery eager mode executes apply_async inline. The bounded local
            # executor preserves the desktop's no-external-worker setup while
            # allowing the parent turn to continue immediately.
            from app.deepspace.services.local_subagent_dispatch import (
                submit_local_subagent_task,
            )

            return submit_local_subagent_task(run_subagent_turn_task.run, kwargs=payload)
        run_subagent_turn_task.apply_async(kwargs=payload)
    except Exception:  # noqa: BLE001
        logger.warning("DeepSpace subagent turn dispatch failed", exc_info=True)
        return False
    return True


def _record_delegation_event(
    context: ToolContext,
    *,
    event_type: str,
    child: DeepSpaceSubagentRun,
    payload: dict[str, Any],
) -> None:
    """Append one delegation event to the conversation's typed log.

    A failure to append must never fail the delegation, so a storage problem
    is reported and the call continues.

    Args:
        context: The calling request's tool context.
        event_type: The event name.
        child: The child the event concerns.
        payload: The event's data.
    """
    try:
        SessionEventStore(require_db(context)).append(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            event_type=event_type,
            payload=payload,
            run_id=context.run_id,
            subagent_id=child.id,
            turn=child.delegation_depth,
            tool_name="subagent",
        )
        require_db(context).commit()
    except Exception:  # noqa: BLE001
        try:
            require_db(context).rollback()
        except Exception:  # noqa: BLE001
            pass


def _activation_observer(
    context: ToolContext,
    child: DeepSpaceSubagentRun,
    *,
    provider: str,
    run_id: str,
):
    """Create a lifecycle observer that persists safe child activation edges."""
    emitter = create_lifecycle_emitter(context)

    def persist(name: str, info: Any, _parent: Any) -> None:
        if isinstance(info, SubagentRunInfo):
            payload = {
                "activation_run_id": info.run_id,
                "provider": info.provider,
                "child_id": info.id,
                "local": info.local,
            }
        elif isinstance(info, SubagentRunEndInfo):
            payload = {
                "activation_run_id": info.run_id,
                "provider": provider,
                "child_id": str(child.id),
                "stop_reason": info.stop_reason,
                # Provider diagnostics remain available through get_agent; do
                # not duplicate potentially sensitive stderr in the event log.
                "has_error": info.error is not None,
            }
        else:
            return
        _record_delegation_event(
            context,
            event_type=name,
            child=child,
            payload=payload,
        )

    emitter.register(persist)
    return create_activation_observer(
        emitter,
        provider,
        str(child.id),
        parent=None,
        run_id=run_id,
    )


def _parse_agent_id(raw: str) -> uuid.UUID:
    """Parse a child agent identity.

    Args:
        raw: The identity the model supplied.

    Returns:
        The parsed identity.

    Raises:
        ValueError: When the identity is not a UUID.
    """
    try:
        return uuid.UUID(str(raw).strip())
    except ValueError as exc:
        raise ValueError("The agent id is not a valid identifier.") from exc


def _load_child(context: ToolContext, agent_id: str) -> DeepSpaceSubagentRun:
    """Load one direct child owned by the calling agent.

    Args:
        context: The calling request's tool context.
        agent_id: The identity the model supplied.

    Returns:
        The child record.

    Raises:
        ValueError: When the identity is unknown to this conversation.
    """
    parsed = _parse_agent_id(agent_id)
    child = (
        require_db(context)
        .execute(
            select(DeepSpaceSubagentRun).where(
                DeepSpaceSubagentRun.id == parsed,
                DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                DeepSpaceSubagentRun.user_id == context.auth.user_id,
                DeepSpaceSubagentRun.conversation_id == context.conversation_id,
                DeepSpaceSubagentRun.parent_agent_id == context.parent_agent_id,
            )
        )
        .scalar_one_or_none()
    )
    if child is None:
        raise ValueError("No child agent with that id belongs to this conversation.")
    return child


def _load_child_snapshot(context: ToolContext, agent_id: str) -> dict[str, Any]:
    """Read a fresh, identity-scoped child snapshot for bounded waits."""
    parsed = _parse_agent_id(agent_id)
    with managed_db_session() as db:
        set_db_tenant_context(db, context.auth.tenant_id)
        child = db.execute(
            select(DeepSpaceSubagentRun).where(
                DeepSpaceSubagentRun.id == parsed,
                DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                DeepSpaceSubagentRun.user_id == context.auth.user_id,
                DeepSpaceSubagentRun.conversation_id == context.conversation_id,
                DeepSpaceSubagentRun.parent_agent_id == context.parent_agent_id,
            )
        ).scalar_one_or_none()
        if child is None:
            raise ValueError("No child agent with that id belongs to this conversation.")
        return _agent_snapshot(child)


def _agent_snapshot(child: DeepSpaceSubagentRun) -> dict[str, Any]:
    """Build the bounded, model-visible details for one child."""
    return {
        "agent_id": str(child.id),
        "label": child.label,
        "mode": child.mode,
        "origin": child.origin,
        "status": child_reported_status(child),
        "depth": child.delegation_depth,
        "turn_count": child.turn_count,
        "stop_reason": child.stop_reason,
        "queued_messages": len(child.pending_messages or []),
        "answer": (child.result_text or "")[:2_000],
        "diagnostic": child.last_error,
    }


def _child_count(context: ToolContext) -> int:
    """Count the children the calling conversation already holds."""
    return int(
        require_db(context)
        .execute(
            select(DeepSpaceSubagentRun.id).where(
                DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                DeepSpaceSubagentRun.user_id == context.auth.user_id,
                DeepSpaceSubagentRun.conversation_id == context.conversation_id,
                DeepSpaceSubagentRun.parent_agent_id == context.parent_agent_id,
            )
        )
        .scalars()
        .all()
        .__len__()
    )


def _max_children(context: ToolContext) -> int:
    """Read the deployment's child cap."""
    return int(getattr(context.settings, "deepspace_subagent_max_children", DEFAULT_MAX_CHILDREN))


def _max_depth(context: ToolContext) -> int:
    """Read the delegation depth limit the conversation runs under.

    The limit is the smaller of the deployment's own limit and the one the
    conversation's agent mode allows, so a mode may only narrow what the
    deployment allows.

    Args:
        context: The calling request's tool context.

    Returns:
        The depth limit.
    """
    return effective_delegation_depth(context)


def _inherited_history(context: ToolContext, message_count: int) -> list[dict[str, Any]]:
    """Collect the parent's recent history for a forked child.

    Args:
        context: The calling request's tool context.
        message_count: The number of recent messages to inherit.

    Returns:
        The inherited messages, oldest first.
    """
    if message_count <= 0:
        return []
    from app.deepspace.models.message import Message

    # A message carries no tenant or user of its own; it belongs to the
    # conversation, and the conversation is the authorized scope.
    rows = (
        require_db(context)
        .execute(
            select(Message)
            .where(Message.conversation_id == context.conversation_id)
            .order_by(Message.created_at.desc())
            .limit(message_count)
        )
        .scalars()
        .all()
    )
    history: list[dict[str, Any]] = []
    for message in reversed(rows):
        if message.role not in {"user", "assistant"}:
            continue
        content = message.active_version.content if message.active_version else message.content
        if content and content.strip():
            history.append({"role": message.role, "content": content})
    return history


async def _start_child(
    *,
    context: ToolContext,
    arguments: dict[str, Any],
    origin: str,
    mode: str,
    inherited_history: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Start one child agent and run its first turn."""
    depth = context.depth + 1
    max_depth = _max_depth(context)
    if depth > max_depth:
        return {
            "status": "denied",
            "message": (
                f"This deployment allows {max_depth} level(s) of delegation, and this call "
                f"would start level {depth}."
            ),
            "max_depth": max_depth,
        }
    if _child_count(context) >= _max_children(context):
        return {
            "status": "denied",
            "message": (
                f"This conversation already holds the maximum of {_max_children(context)} "
                "child agents."
            ),
        }

    # Get backend selection
    backend_name = str(
        arguments.get("backend")
        or getattr(context.settings, "deepspace_subagent_default_backend", "in-process")
        or "in-process"
    )
    backend_config = arguments.get("backend_config") or {}

    if backend_name not in {"in-process", "acp", "claude-code", "codex", "dsh-sdk"}:
        return {
            "status": "denied",
            "message": f"Subagent backend '{backend_name}' is not supported by DeepSpace.",
        }
    if backend_name != "in-process" and mode != "one-shot":
        return {
            "status": "denied",
            "message": (
                "External subagent backends currently support one-shot runs only. "
                "Use mode='one-shot' or the in-process backend for continuable children."
            ),
        }

    explicit_agent_preset = str(arguments.get("agent_preset") or "").strip() or None
    agent_preset = explicit_agent_preset or getattr(context, "agent_preset_override", None)
    if explicit_agent_preset:
        from app.deepspace.services.presets import PresetNotFoundError, get_preset

        try:
            agent_preset = get_preset(explicit_agent_preset).id
        except PresetNotFoundError as exc:
            return {"status": "denied", "message": str(exc)}

    if agent_preset and backend_name != "in-process":
        return {
            "status": "denied",
            "message": (
                "This external backend cannot enforce DeepSpace child agent presets. "
                "Use the in-process backend for preset-scoped children."
            ),
        }

    tool_filter = {
        "allow": [str(name) for name in (arguments.get("tool_allow") or [])],
        "deny": [str(name) for name in (arguments.get("tool_deny") or [])],
    }
    if backend_name != "in-process" and (tool_filter["allow"] or tool_filter["deny"]):
        return {
            "status": "denied",
            "message": (
                "This external backend cannot enforce DeepSpace per-child tool filters. "
                "Use the in-process backend to apply tool_allow/tool_deny safely."
            ),
        }

    # For in-process backend, create a durable child and dispatch its first
    # turn to the background worker rather than waiting for the child model.
    if backend_name == "in-process":
        from app.deepspace.services.durable_subagent_manager import (
            get_durable_subagent_manager,
        )

        child = new_child_record(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            parent_run_id=context.run_id,
            parent_agent_id=context.parent_agent_id,
            origin=origin,
            mode=mode,
            label=str(arguments["description"]),
            delegation_depth=depth,
            agent_provider=arguments.get("provider"),
            agent_model=arguments.get("model"),
            reasoning_effort=None,
            persona=arguments.get("persona"),
            tool_filter=tool_filter,
            inherited_history=inherited_history,
        )
        child.status = "queued"
        if agent_preset:
            child.descriptor = {**dict(child.descriptor or {}), "agentPreset": agent_preset}
        if not get_durable_subagent_manager().admit(
            require_db(context),
            child,
            max_children=_max_children(context),
        ):
            return {
                "status": "denied",
                "message": "The child could not be admitted within this conversation's ownership and child limit.",
            }
        queued_messages = _pending_messages(context, child)
        queued_messages.append({"id": str(uuid.uuid4()), "message": str(arguments["task"])})
        _store_pending_messages(context, child, queued_messages)
        require_db(context).commit()
        _record_delegation_event(
            context,
            event_type=SUBAGENT_SPAWN,
            child=child,
            payload={
                "mode": child.mode,
                "origin": child.origin,
                "depth": child.delegation_depth,
                "label": child.label,
            },
        )
        if explicit_agent_preset and agent_preset:
            _record_delegation_event(
                context,
                event_type=SUBAGENT_PRESET,
                child=child,
                payload={"agentPreset": agent_preset},
            )
        _record_delegation_event(
            context,
            event_type=SUBAGENT_DESCRIPTOR,
            child=child,
            payload=dict(build_child_descriptor(child)),
        )
        dispatched = _dispatch_child_turn(
            context,
            child=child,
        )
        return {
            "status": "queued",
            "agent_id": str(child.id),
            "mode": child.mode,
            "origin": child.origin,
            "depth": child.delegation_depth,
            "label": child.label,
            "task_queued": True,
            "dispatched": dispatched,
            "message": (
                "The child task is queued for background execution. "
                + (
                    "The background runtime accepted it; read the result with get_agent."
                    if dispatched
                    else "It remains saved in the child queue because the background runtime did not accept it."
                )
            ),
        }

    # For out-of-process backends, use provider
    provider = get_provider(backend_name)
    if provider is None:
        return {
            "status": "error",
            "message": f"Subagent backend '{backend_name}' is not configured.",
        }

    # Create a child record for tracking
    child = new_child_record(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        parent_run_id=context.run_id,
        parent_agent_id=context.parent_agent_id,
        origin=origin,
        mode=mode,
        label=str(arguments["description"]),
        delegation_depth=depth,
        agent_provider=backend_name,
        agent_model=arguments.get("model"),
        reasoning_effort=None,
        persona=arguments.get("persona"),
        tool_filter=tool_filter,
        inherited_history=inherited_history,
    )
    if agent_preset:
        child.descriptor = {**dict(child.descriptor or {}), "agentPreset": agent_preset}
    from app.deepspace.services.durable_subagent_manager import get_durable_subagent_manager

    if not get_durable_subagent_manager().admit(
        require_db(context),
        child,
        max_children=_max_children(context),
    ):
        return {
            "status": "denied",
            "message": "The child could not be admitted within this conversation's ownership and child limit.",
        }

    _record_delegation_event(
        context,
        event_type=SUBAGENT_SPAWN,
        child=child,
        payload={
            "mode": child.mode,
            "origin": child.origin,
            "depth": child.delegation_depth,
            "label": child.label,
            "backend": backend_name,
        },
    )
    if explicit_agent_preset and agent_preset:
        _record_delegation_event(
            context,
            event_type=SUBAGENT_PRESET,
            child=child,
            payload={"agentPreset": agent_preset},
        )
    _record_delegation_event(
        context,
        event_type=SUBAGENT_DESCRIPTOR,
        child=child,
        payload=dict(child.descriptor or build_child_descriptor(child)),
    )

    # Build run spec for out-of-process provider
    run_spec = SubagentRunSpec(
        task=str(arguments["task"]),
        persona=arguments.get("persona"),
        # The DSH runtime owns its route; DeepSpace provider ids are not DSH
        # provider ids. Its provider/model are set by the operator config.
        model=None if backend_name == "dsh-sdk" else arguments.get("model"),
        provider=None if backend_name == "dsh-sdk" else arguments.get("provider"),
        tool_allow=arguments.get("tool_allow"),
        tool_deny=arguments.get("tool_deny"),
        cwd=backend_config.get("cwd")
        or (context.workspace_root.as_posix() if context.workspace_root else "."),
        env=backend_config.get("env") or {},
        depth=depth,
        parent_run_id=context.run_id,
        conversation_id=context.conversation_id,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
    )

    # Start/end observations bracket the actual external process activation,
    # not merely the durable child-row admission above.
    activation_id = str(uuid.uuid4())
    observer = _activation_observer(
        context,
        child,
        provider=backend_name,
        run_id=activation_id,
    )
    observer.start()
    cancel_check = context.cancel_check or (lambda: False)
    try:
        result = await provider.run(run_spec, cancel_check)
    except Exception as exc:  # noqa: BLE001 - keep the durable run and event trail coherent
        logger.warning("DeepSpace external subagent provider failed", exc_info=True)
        result = ProviderResult(status="failed", answer="", error=str(exc))

    # Update child record
    child.result_text = result.answer
    child.stop_reason = result.status
    child.turn_count = 1
    child.status = "idle" if result.status == "completed" else result.status
    child.last_error = result.error
    child.descriptor = build_child_descriptor(child)
    require_db(context).commit()

    observer.settle(
        LifecycleResult(
            output=[result.answer] if result.answer else [],
            stop_reason=str(getattr(result, "stop_reason", result.status)),
            error=RuntimeError(str(result.error)) if result.error else None,
        )
    )

    _record_delegation_event(
        context,
        event_type=SUBAGENT_RESULT,
        child=child,
        payload={
            "stop_reason": result.status,
            "rounds": 1,
            "tools_used": [tc.get("name") for tc in result.tool_calls],
            "backend": backend_name,
        },
    )

    return {
        "status": "ok" if result.status == "completed" else "failed",
        "agent_id": str(child.id),
        "mode": child.mode,
        "origin": child.origin,
        "depth": child.delegation_depth,
        "label": child.label,
        "answer": result.answer,
        "stop_reason": result.status,
        "diagnostic": result.error,
        "rounds": 1,
        "tools_used": [tc.get("name") for tc in result.tool_calls],
        "backend": backend_name,
    }


async def _subagent(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delegate a self-contained task to a fresh child agent."""
    mode = str(arguments.get("mode") or "continuable")
    return await _start_child(
        context=context,
        arguments=arguments,
        origin="subagent",
        mode=mode,
        inherited_history=None,
    )


async def _subagent_fork(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Delegate a task to a child that inherits the parent's history."""
    history = _inherited_history(context, int(arguments.get("history_messages") or 10))
    return await _start_child(
        context=context,
        arguments=arguments,
        origin="fork",
        mode="one-shot",
        inherited_history=history,
    )


def _pending_messages(context: ToolContext, child: DeepSpaceSubagentRun) -> list[dict[str, Any]]:
    """Read the messages that wait for one child's turn.

    The row is read under a lock, so a message that is queued while the
    child's turn claims the queue is neither lost nor answered twice.

    Args:
        context: The calling request's tool context.
        child: The child to read the queue of.

    Returns:
        The queued messages, oldest first.
    """
    raw = (
        require_db(context)
        .execute(
            select(DeepSpaceSubagentRun.pending_messages)
            .where(
                DeepSpaceSubagentRun.id == child.id,
                DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                DeepSpaceSubagentRun.user_id == context.auth.user_id,
                DeepSpaceSubagentRun.conversation_id == context.conversation_id,
            )
            .with_for_update()
        )
        .scalar()
    )
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _store_pending_messages(
    context: ToolContext,
    child: DeepSpaceSubagentRun,
    pending: list[dict[str, Any]],
) -> None:
    """Write the messages that wait for one child's turn.

    Args:
        context: The calling request's tool context.
        child: The child whose queue is written.
        pending: The queued messages.
    """
    require_db(context).execute(
        update(DeepSpaceSubagentRun)
        .where(
            DeepSpaceSubagentRun.id == child.id,
            DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
            DeepSpaceSubagentRun.user_id == context.auth.user_id,
            DeepSpaceSubagentRun.conversation_id == context.conversation_id,
        )
        .values(pending_messages=pending)
    )


async def _send_message(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Deliver a message to a continuable child agent."""
    child = _load_child(context, str(arguments["agent_id"]))
    if child.mode != "continuable":
        return {
            "status": "rejected",
            "agent_id": str(child.id),
            "message": "This child is one-shot; start a continuable child to send it messages.",
        }
    try:
        attachment_ids = list(
            dict.fromkeys(
                uuid.UUID(str(value)) for value in (arguments.get("attachment_file_ids") or [])
            )
        )
    except (TypeError, ValueError, AttributeError):
        return {
            "status": "rejected",
            "agent_id": str(child.id),
            "message": "One or more image attachment references are invalid.",
        }
    if attachment_ids:
        from app.deepspace.services.chat_service import ATTACHMENT_VISION_MIME_TYPES

        files = (
            require_db(context)
            .execute(
                select(DeepSpaceWorkspaceFile).where(
                    DeepSpaceWorkspaceFile.id.in_(attachment_ids),
                    DeepSpaceWorkspaceFile.tenant_id == context.auth.tenant_id,
                    DeepSpaceWorkspaceFile.user_id == context.auth.user_id,
                    DeepSpaceWorkspaceFile.conversation_id == context.conversation_id,
                )
            )
            .scalars()
            .all()
        )
        by_id = {file.id: file for file in files}
        if len(by_id) != len(attachment_ids):
            return {
                "status": "rejected",
                "agent_id": str(child.id),
                "message": "One or more image attachments are unavailable in this conversation.",
            }
        if any(
            str(file.content_type or "").casefold() not in ATTACHMENT_VISION_MIME_TYPES
            for file in files
        ):
            return {
                "status": "rejected",
                "agent_id": str(child.id),
                "message": "Child-message attachments must be supported image files.",
            }
    pending = _pending_messages(context, child)
    if len(pending) >= MAX_PENDING_MESSAGES:
        return {
            "status": "rejected",
            "agent_id": str(child.id),
            "message": f"This child already has {len(pending)} queued messages.",
        }
    message_id = str(uuid.uuid4())
    pending.append(
        {
            "id": message_id,
            "message": str(arguments["message"]),
            "attachment_file_ids": [str(file_id) for file_id in attachment_ids],
        }
    )
    _store_pending_messages(context, child, pending)
    require_db(context).commit()
    dispatched = _dispatch_child_turn(
        context,
        child=child,
    )
    return {
        "status": "delivered",
        "agent_id": str(child.id),
        "message_id": message_id,
        "queued_messages": len(pending),
        "dispatched": dispatched,
        "message": (
            "The message was delivered to the child. If its current model round is still active, "
            "the child can pick it up before a later round; otherwise it runs on the next turn. "
            "Read the answer with get_agent."
        ),
    }


async def _interrupt_agent(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Stop the current turn of a running child agent."""
    import uuid

    from app.deepspace.services.durable_subagent_manager import (
        get_durable_subagent_manager,
    )

    child = _load_child(context, str(arguments["agent_id"]))
    cancelled_runs = get_durable_subagent_manager().request_cancel(
        require_db(context),
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        child_id=uuid.UUID(str(child.id)),
    )
    return {
        "status": "accepted",
        "agent_id": str(child.id),
        "message": (
            f"A stop was requested for {cancelled_runs} child run(s) in this owned subtree. "
            "Queued messages stay queued and continuable children remain available for follow-ups."
        ),
    }


async def _list_agents(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the calling agent's owned child tree without result text."""
    include_completed = bool(arguments.get("include_completed", True))
    rows = (
        require_db(context)
        .execute(
            select(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                DeepSpaceSubagentRun.user_id == context.auth.user_id,
                DeepSpaceSubagentRun.conversation_id == context.conversation_id,
            )
            .order_by(DeepSpaceSubagentRun.created_at.asc())
        )
        .scalars()
        .all()
    )
    by_parent: dict[str | None, list[DeepSpaceSubagentRun]] = {}
    for child in rows:
        parent_id = str(child.parent_agent_id) if child.parent_agent_id else None
        by_parent.setdefault(parent_id, []).append(child)

    def render(child: DeepSpaceSubagentRun, ancestry: frozenset[str]) -> dict[str, Any] | None:
        child_id = str(child.id)
        if child_id in ancestry:
            # Corrupt/cyclic ancestry must not recurse forever or escape the
            # tenant/conversation-scoped result.
            return None
        descendants = [
            rendered
            for nested in by_parent.get(child_id, [])
            if (rendered := render(nested, ancestry | {child_id})) is not None
        ]
        status = child_reported_status(child)
        if not include_completed and status == CHILD_STATUS_COMPLETED and not descendants:
            return None
        return {
            "agent_id": child_id,
            "parent_agent_id": str(child.parent_agent_id) if child.parent_agent_id else None,
            "label": child.label,
            "mode": child.mode,
            "origin": child.origin,
            "status": status,
            "depth": child.delegation_depth,
            "turn_count": child.turn_count,
            "queued_messages": len(child.pending_messages or []),
            "result_available": bool(child.result_text),
            "children": descendants,
        }

    owner_id = str(context.parent_agent_id) if context.parent_agent_id else None
    agents = [
        rendered
        for child in by_parent.get(owner_id, [])
        if (rendered := render(child, frozenset())) is not None
    ]
    visible_agent_ids: set[uuid.UUID] = set()

    def collect_ids(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            visible_agent_ids.add(uuid.UUID(entry["agent_id"]))
            collect_ids(entry["children"])

    collect_ids(agents)
    revision = int(
        require_db(context)
        .execute(
            select(func.max(DeepSpaceSessionEvent.seq)).where(
                DeepSpaceSessionEvent.tenant_id == context.auth.tenant_id,
                DeepSpaceSessionEvent.user_id == context.auth.user_id,
                DeepSpaceSessionEvent.conversation_id == context.conversation_id,
                DeepSpaceSessionEvent.event_type.in_(DELEGATION_PROGRESS_EVENT_TYPES),
                (
                    DeepSpaceSessionEvent.subagent_id.in_(visible_agent_ids)
                    if visible_agent_ids
                    else DeepSpaceSessionEvent.subagent_id.is_(None)
                ),
            )
        )
        .scalar()
        or 0
    )
    return {
        "status": "ok",
        "agent_count": len(visible_agent_ids),
        "revision": revision,
        "agents": agents,
    }


async def _get_agent(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a child result, optionally waiting a bounded time for completion."""
    agent_id = str(arguments["agent_id"])
    wait_seconds = max(0, min(90, int(arguments.get("wait_timeout_seconds") or 0)))
    if wait_seconds == 0:
        snapshot = _agent_snapshot(_load_child(context, agent_id))
        return {"status": "ok", "agent": snapshot}

    deadline = time.monotonic() + wait_seconds
    while True:
        snapshot = await asyncio.to_thread(_load_child_snapshot, context, agent_id)
        if snapshot["status"] not in {"queued", "running"}:
            return {"status": "ok", "agent": snapshot, "wait_timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "ok", "agent": snapshot, "wait_timed_out": True}
        await asyncio.sleep(min(0.25, remaining))


SUBAGENT_DESCRIPTION = (
    "Delegate a self-contained task to a child agent and receive its answer. "
    "The child works on its own context and may use the workspace tools. Give "
    "it everything it needs in the task text; you will not see its working "
    "steps. Use mode 'continuable' when you want to send it further messages."
)
SUBAGENT_FORK_DESCRIPTION = (
    "Delegate a task to a child agent that inherits this conversation's recent "
    "history, so it can act on the context you already have. The child answers "
    "once and ends."
)
SEND_MESSAGE_DESCRIPTION = (
    "Send a message to a continuable child agent by its id. Optionally attach "
    "up to 10 supported image file IDs from this conversation. A running child "
    "can pick up the message between model rounds; otherwise it runs on the "
    "next turn. This call does not return the child's answer."
)
INTERRUPT_AGENT_DESCRIPTION = (
    "Request cancellation of a child agent's current turn by its id. Only the "
    "current turn stops; queued messages stay queued and the child stays "
    "available for follow-ups."
)
LIST_AGENTS_DESCRIPTION = (
    "List a compact child-agent roster with stable ids, status, depth, and a result_available flag. "
    "Do not poll repeatedly. Use get_agent with wait_timeout_seconds (1-90) to wait once for a "
    "running child, or omit it to read the current status immediately."
)
GET_AGENT_DESCRIPTION = (
    "Read one child agent's detailed status and answer by the id returned from list_agents. "
    "Optionally set wait_timeout_seconds to 1-90 to wait once for queued/running work; the "
    "result says whether the wait timed out."
)

DELEGATION_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="subagent",
        description=SUBAGENT_DESCRIPTION,
        properties={
            "description": string_property(
                "A short label naming what the child is for.", min_length=1, max_length=255
            ),
            "task": string_property(
                "The complete task the child should carry out.",
                min_length=1,
                max_length=20_000,
            ),
            "mode": string_property(
                "Whether the child ends with its task (one-shot) or stays available "
                "for further messages (continuable, the default).",
                enum=["one-shot", "continuable"],
            ),
            "provider": string_property(
                "The provider route the child runs under. Omit to inherit this agent's route.",
                min_length=1,
                max_length=100,
            ),
            "model": string_property(
                "The model the child runs under. Omit to inherit this agent's model.",
                min_length=1,
                max_length=200,
            ),
            "persona": string_property(
                "A persona that shapes how the child works.", max_length=4_000
            ),
            "agent_preset": string_property(
                "Optional narrower DeepSpace preset for this child. Parent and deployment "
                "security limits still apply.",
                min_length=1,
                max_length=100,
            ),
            "backend": string_property(
                "Optional configured runtime: in-process (default), ACP, Claude Code, Codex, "
                "or DSH SDK. External runtimes support one-shot children only.",
                enum=["in-process", "acp", "claude-code", "codex", "dsh-sdk"],
            ),
            "tool_allow": array_property(
                "The only tools the child may use. Omit to give it the full set.",
                max_items=64,
            ),
            "tool_deny": array_property("The tools the child may not use.", max_items=64),
        },
        required=["description", "task"],
        arguments_model=SubagentArguments,
        mode="write",
        handler=_subagent,
    ),
    define_native_tool(
        name="subagent_fork",
        description=SUBAGENT_FORK_DESCRIPTION,
        properties={
            "description": string_property(
                "A short label naming what the child is for.", min_length=1, max_length=255
            ),
            "task": string_property(
                "The complete task the child should carry out.",
                min_length=1,
                max_length=20_000,
            ),
            "provider": string_property(
                "The provider route the child runs under. Omit to inherit this agent's route.",
                min_length=1,
                max_length=100,
            ),
            "model": string_property(
                "The model the child runs under. Omit to inherit this agent's model.",
                min_length=1,
                max_length=200,
            ),
            "persona": string_property(
                "A persona that shapes how the child works.", max_length=4_000
            ),
            "agent_preset": string_property(
                "Optional narrower DeepSpace preset for this child. Parent and deployment "
                "security limits still apply.",
                min_length=1,
                max_length=100,
            ),
            "backend": string_property(
                "Optional configured runtime: in-process (default), ACP, Claude Code, Codex, "
                "or DSH SDK. External forked children run once.",
                enum=["in-process", "acp", "claude-code", "codex", "dsh-sdk"],
            ),
            "tool_allow": array_property(
                "The only tools the child may use. Omit to give it the full set.",
                max_items=64,
            ),
            "tool_deny": array_property("The tools the child may not use.", max_items=64),
            "history_messages": integer_property(
                "How many recent messages the child inherits.", minimum=0, maximum=40
            ),
        },
        required=["description", "task"],
        arguments_model=SubagentForkArguments,
        mode="write",
        handler=_subagent_fork,
    ),
    define_native_tool(
        name="send_message",
        description=SEND_MESSAGE_DESCRIPTION,
        properties={
            "agent_id": string_property(
                "The id of the child agent to message.", min_length=1, max_length=64
            ),
            "message": string_property("The message to deliver.", min_length=1, max_length=20_000),
            "attachment_file_ids": array_property(
                "Optional image file IDs already attached to this conversation.", max_items=10
            ),
        },
        required=["agent_id", "message"],
        arguments_model=SendMessageArguments,
        mode="write",
        handler=_send_message,
    ),
    define_native_tool(
        name="list_agents",
        description=LIST_AGENTS_DESCRIPTION,
        properties={
            "include_completed": boolean_property(
                "Include children whose last turn already completed."
            )
        },
        arguments_model=ListAgentsArguments,
        mode="read",
        handler=_list_agents,
    ),
    define_native_tool(
        name="get_agent",
        description=GET_AGENT_DESCRIPTION,
        properties={
            "agent_id": string_property(
                "The child agent id returned by list_agents.", min_length=1, max_length=64
            ),
            "wait_timeout_seconds": integer_property(
                "Optional bounded wait for queued/running work; 0 returns immediately.",
                minimum=0,
                maximum=90,
            ),
        },
        required=["agent_id"],
        arguments_model=GetAgentArguments,
        mode="read",
        handler=_get_agent,
    ),
]

__all__ = [
    "DELEGATION_TOOLS",
    "GetAgentArguments",
    "InterruptAgentArguments",
    "ListAgentsArguments",
    "SendMessageArguments",
    "SubagentArguments",
    "SubagentForkArguments",
    "child_tool_names",
]
