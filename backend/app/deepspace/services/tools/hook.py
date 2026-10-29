"""The hook tool for DeepSpace.

The model can ask the deployment's hooks about a call before it makes one.
The tool runs the hooks that apply to the event and reports their decision,
so a guard the deployment installed is visible to the model instead of
deciding for it in silence.

The tool only reads: it runs a hook command and reports what the command
answered.  It never changes the deployment's own policy.  A hook may
answer in DeepSpace's own dialect or in the Claude Code and Codex dialect,
and the tool reports the same fields either way.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.hook_gate import hook_audit_recorder
from app.deepspace.services.hooks import (
    HookAuditRecorder,
    HookEvent,
    HookRunner,
    hook_registry,
)
from app.deepspace.services.session_events import SessionEventStore
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    object_property,
    string_property,
)


class HookRunArguments(ToolArguments):
    """Ask the deployment's hooks about a call."""

    event: HookEvent
    tool_name: str | None = Field(default=None, min_length=1, max_length=128)
    arguments: dict[str, Any] | None = None


def _audit_for(context: ToolContext) -> HookAuditRecorder | None:
    """Return the recorder that writes the audit trail for this probe.

    Args:
        context: The calling request's tool context.

    Returns:
        The recorder, or None when the audit is off or the runtime has no
        database session.
    """
    database = context.db
    if database is None:
        return None
    return hook_audit_recorder(
        context.settings,
        SessionEventStore(database),
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        run_id=context.run_id,
    )


async def _hook_run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Ask the deployment's hooks about a call."""
    try:
        event = HookEvent(arguments["event"])
    except (KeyError, ValueError):
        return {"status": "error", "message": f"Unknown hook event: {arguments.get('event')!r}"}
    payload = arguments.get("arguments")
    runner = HookRunner(
        hook_registry(),
        recorder=_audit_for(context),
        claude_dialect=bool(getattr(context.settings, "deepspace_hook_claude_dialect", True)),
    )
    try:
        decision = await runner.run_event(
            event,
            tool_name=arguments.get("tool_name"),
            arguments=payload if isinstance(payload, dict) else {},
            conversation_id=str(context.conversation_id),
        )
    except Exception as exc:  # noqa: BLE001 - a broken hook must not break the turn
        return {"status": "error", "message": str(exc)}
    return {
        "status": "ok",
        "decision": decision.action,
        "reason": decision.reason,
        "additional_context": decision.additional_context,
    }


HOOK_RUN_DESCRIPTION = (
    "Ask the deployment's hooks about a tool call before you make it. The hooks are "
    "commands the deployment installed: they allow the call, block it with a reason, "
    "or ask about it, and a post-tool hook can add context for you. Name the tool you "
    "intend to call so the matching hooks see it."
)

HOOK_TOOLS: tuple[NativeToolSpec, ...] = (
    define_native_tool(
        name="hook_run",
        description=HOOK_RUN_DESCRIPTION,
        properties={
            "event": string_property(
                "The moment to ask about.",
                enum=[event.value for event in HookEvent],
            ),
            "tool_name": string_property(
                "The tool the call would make, so the hooks that filter on a tool name apply.",
                min_length=1,
                max_length=128,
            ),
            "arguments": object_property("The arguments the call would carry."),
        },
        required=["event"],
        arguments_model=HookRunArguments,
        mode="read",
        handler=_hook_run,
    ),
)

__all__ = ["HOOK_TOOLS", "HookRunArguments"]
