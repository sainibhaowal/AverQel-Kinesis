"""The hook gate in DeepSpace's tool pipeline.

A hook may watch every tool call and refuse one. The gate runs before the
call, so a blocked call never reaches the tool.

A broken hook never blocks the agent: a hook that times out, crashes, or
answers with something unreadable is treated as allowing the call.

The gate speaks both hook dialects, so a guard script written for Claude
Code or for Codex runs here unchanged.  A hook that answers with a
permission decision of ``deny`` blocks the call, a hook that exits with
status 2 and writes its reason on standard error blocks the call, and a
hook that answers with ``{"continue": false}`` stops the agent.

The gate is also the seam the chat service uses for the events that do not
belong to a single tool call.  ``after_tool`` runs the ``PostToolUse``
hooks, ``on_stop`` runs the ``Stop`` hooks, and ``on_session_start`` runs
the ``SessionStart`` hooks.  Each returns the hooks' combined decision, so
the caller can put each answer where it belongs: the model sees the
context a ``PostToolUse`` hook added, and the user sees the message a
``Stop`` hook wrote.

Every hook invocation is written to the audit trail when the deployment
turns the audit on.  The trail is best effort: a storage problem never
breaks the tool call.

The chat service wires the gate like this::

    gate = HookParticipant(
        audit=hook_audit_recorder(
            settings,
            session_events,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            run_id=run_id,
        ),
        claude_dialect=bool(settings.deepspace_hook_claude_dialect),
        conversation_id=str(conversation_id),
    )
    pipeline.register_pre("deepspace-hooks", gate)

``gate`` answers the pre-tool question on its own, and the chat service
calls the other three moments where a turn reaches them:

* ``await gate.after_tool(tool_name, payload)`` after a call succeeds.
  The answer's ``additional_context`` is the context for the model, and its
  ``system_message`` is a message for the user.
* ``await gate.on_stop()`` when the agent finishes a turn.  The answer's
  ``system_message`` is a message the user sees.
* ``await gate.on_session_start()`` when a conversation starts.  The
  answer's ``additional_context`` is the context for the model.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.deepspace.services.hooks import (
    HookAction,
    HookAuditRecorder,
    HookDecision,
    HookEvent,
    HookRunner,
    hook_registry,
)
from app.deepspace.services.session_events import (
    HOOK_INVOKED,
    HOOK_RESULT,
    SessionEventStore,
)
from app.deepspace.services.tool_pipeline import PreDecision, PreOutcome

logger = logging.getLogger(__name__)

#: The call the user approved, when one is being run again.
#:
#: A call the user approved is not asked about a second time.  The value is
#: the signature of that call, and the chat service sets it while it runs
#: the approved call again.
APPROVED_CALL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "deepspace_hook_approved_call", default=None
)


def call_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return the signature that names one call.

    Args:
        tool_name: The tool the model called.
        arguments: The validated arguments.

    Returns:
        The signature, which is the same for two calls that ask the same
        tool to do the same thing.
    """
    return (
        tool_name + "\x00" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    )


@dataclass(frozen=True, slots=True)
class HookPreOutcome(PreOutcome):
    """What the gate decided about one call.

    The decision is the pipeline's own vocabulary: ``allow``, ``deny``, or
    ``cancel``.  The fields below carry the hooks' own answer, so a caller
    that asks the gate directly can tell an ask from a block and can apply
    a replaced argument set.

    Attributes:
        action: The action the hooks answered with: ``allow``, ``block``,
            or ``ask``.
        updated_input: The arguments that replace the call's own arguments.
        system_message: The message the hooks showed the user.
        additional_context: The context the hooks added for the model.
    """

    action: HookAction = "allow"
    updated_input: dict[str, Any] | None = None
    system_message: str = ""
    additional_context: str = ""


class SessionEventHookAudit:
    """Writes the hook audit trail to a conversation's session log.

    Every hook invocation writes two typed events: one that says the hook
    ran, and one that says what it decided.  The record is best effort, so
    a storage problem never breaks the tool call.
    """

    def __init__(
        self,
        store: SessionEventStore,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        turn: int | None = None,
        step: int | None = None,
    ) -> None:
        """Create the recorder.

        Args:
            store: The conversation's session event store.
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the events belong to.
            run_id: The run the events belong to.
            turn: The turn the events belong to.
            step: The step the events belong to.
        """
        self._store = store
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._conversation_id = conversation_id
        self._run_id = run_id
        self._turn = turn
        self._step = step

    def record_invoked(
        self,
        *,
        hook_id: str,
        event: str,
        tool_name: str,
        duration_ms: int,
    ) -> None:
        """Record that one hook ran.

        Args:
            hook_id: The hook's identifier.
            event: The moment the hook ran for.
            tool_name: The tool the call concerned.
            duration_ms: How long the hook took.
        """
        self._append(
            HOOK_INVOKED,
            {
                "hook_id": hook_id,
                "event": event,
                "tool_name": tool_name,
                "duration_ms": duration_ms,
            },
        )

    def record_result(
        self,
        *,
        hook_id: str,
        event: str,
        tool_name: str,
        decision: str,
        reason: str,
    ) -> None:
        """Record what one hook decided.

        Args:
            hook_id: The hook's identifier.
            event: The moment the hook ran for.
            tool_name: The tool the call concerned.
            decision: The action the hook answered with.
            reason: The reason the hook gave.
        """
        self._append(
            HOOK_RESULT,
            {
                "hook_id": hook_id,
                "event": event,
                "tool_name": tool_name,
                "decision": decision,
                "reason": reason,
            },
        )

    def _append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one audit event to the session log.

        A failed write is reported and swallowed: the audit trail never
        breaks the tool call it describes.

        Args:
            event_type: The event name.
            payload: The event's data.
        """
        database = getattr(self._store, "db", None)
        try:
            self._store.append(
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                conversation_id=self._conversation_id,
                event_type=event_type,
                payload=payload,
                run_id=self._run_id,
                turn=self._turn,
                step=self._step,
                tool_name=str(payload.get("tool_name") or "") or None,
            )
            if database is not None:
                database.commit()
        except Exception:
            if database is not None:
                try:
                    database.rollback()
                except Exception:
                    logger.warning("The hook audit rollback failed", exc_info=True)
            logger.warning("The hook audit record could not be written", exc_info=True)


def hook_audit_recorder(
    settings: Settings,
    store: SessionEventStore,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
) -> SessionEventHookAudit | None:
    """Return the audit recorder for one conversation.

    The deployment's ``deepspace_hook_audit_enabled`` setting decides
    whether the trail is written.  A deployment that turns the audit off
    gets no recorder, and the hooks run without one.

    Args:
        settings: The deployment settings.
        store: The conversation's session event store.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation the events belong to.
        run_id: The run the events belong to.

    Returns:
        The recorder, or None when the audit is off.
    """
    if not bool(getattr(settings, "deepspace_hook_audit_enabled", True)):
        return None
    return SessionEventHookAudit(
        store,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=run_id,
    )


class HookParticipant:
    """Runs the deployment's hooks around a tool call.

    The participant is the seam between the hook protocol and the product's
    tool pipeline. A hook that blocks a call stops it, and the reason the
    hook gave is the reason the model sees.

    A hook that asks about a call is not dropped.  The pipeline's
    vocabulary has no ask, so the gate reports the ask as a cancellation
    and carries ``requires_approval`` on the outcome, so a caller that
    asks the gate directly can hand the ask to the product's approval
    flow.
    """

    def __init__(
        self,
        runner: HookRunner | None = None,
        *,
        audit: HookAuditRecorder | None = None,
        audit_factory: Callable[[], HookAuditRecorder | None] | None = None,
        claude_dialect: bool = True,
        conversation_id: str | Callable[[], str] = "",
    ) -> None:
        """Create the participant.

        Args:
            runner: The runner that asks the hooks. The deployment's hooks
                are used when it is not given.  A runner that was given
                keeps its own recorder and its own dialect setting.
            audit: The recorder that writes the audit trail.  It is used
                when the participant builds its own runner.
            claude_dialect: Whether the Claude Code and Codex dialect is
                accepted.  It is used when the participant builds its own
                runner.
            conversation_id: The conversation the calls belong to, or a
                function that returns it.  The pipeline's pre-dispatch
                signature does not carry it, so a participant that serves
                one conversation is told which conversation it serves.  A
                function is used when the participant outlives one
                conversation.
        """
        self._runner = runner
        self._audit = audit
        self._audit_factory = audit_factory
        self._claude_dialect = claude_dialect
        self._conversation_id = conversation_id

    def _audit_for(self) -> HookAuditRecorder | None:
        """Return the audit recorder for the current turn.

        Returns:
            The recorder, or None when the deployment turned the audit off.
        """
        if self._audit_factory is None:
            return None
        try:
            return self._audit_factory()
        except Exception:  # noqa: BLE001 - an audit problem never breaks a call
            return None

    def _resolve_conversation_id(self) -> str:
        """Return the conversation the calls belong to.

        Returns:
            The conversation id, empty when the participant was not told.
        """
        resolver = self._conversation_id
        if callable(resolver):
            return str(resolver() or "")
        return str(resolver or "")

    def _runner_for(self) -> HookRunner:
        """Return the runner this participant asks.

        Returns:
            The runner.
        """
        if self._runner is not None:
            return self._runner
        return HookRunner(
            registry=hook_registry(),
            recorder=self._audit if self._audit is not None else self._audit_for(),
            claude_dialect=self._claude_dialect,
        )

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> PreOutcome:
        """Ask the hooks whether one call may run.

        A call the user already approved is not asked about again, so an
        approval never becomes a loop of approvals.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.

        Returns:
            The decision. A blocked call is denied with the hook's reason,
            and an asked call is cancelled with the hook's question.
        """
        approved = APPROVED_CALL.get()
        if approved is not None and approved == call_signature(tool_name, arguments):
            return PreOutcome("allow")
        return await self.evaluate(tool_name, arguments)

    async def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        conversation_id: str = "",
    ) -> HookPreOutcome:
        """Ask the hooks about one call and report what they decided.

        A hook that answers with replacement arguments replaces the call's
        arguments in place, so the tool the pipeline runs sees the
        replacement.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.
            conversation_id: The conversation the call belongs to.

        Returns:
            The decision in the pipeline's vocabulary, with the hooks' own
            answer beside it.
        """
        runner = self._runner_for()
        registry = runner.registry
        if not registry.hooks_for(HookEvent.PRE_TOOL_USE, tool_name):
            return HookPreOutcome("allow")
        try:
            decision = await runner.run_pre_tool(
                tool_name,
                arguments,
                conversation_id=conversation_id or self._resolve_conversation_id(),
            )
        except Exception:  # noqa: BLE001
            logger.warning("A tool hook could not be run", exc_info=True)
            return HookPreOutcome("allow")
        return self._outcome(decision, arguments)

    def _outcome(self, decision: HookDecision, arguments: dict[str, Any]) -> HookPreOutcome:
        """Report one decision in the pipeline's vocabulary.

        Args:
            decision: The hooks' combined decision.
            arguments: The call's arguments, replaced when a hook named
                replacement arguments.

        Returns:
            The outcome.
        """
        if decision.updated_input is not None:
            arguments.clear()
            arguments.update(decision.updated_input)
        requires_approval = decision.action == "ask"
        pre: PreDecision
        reason: str | None
        if decision.action == "block":
            pre = "deny"
            reason = decision.reason or "A hook blocked this tool call."
        elif decision.stop:
            pre = "cancel"
            reason = decision.stop_reason or decision.reason or "A hook stopped this tool call."
        elif requires_approval:
            pre = "cancel"
            reason = (
                "A hook asked to confirm this tool call: "
                f"{decision.reason or 'the hook gave no reason'}."
            )
        else:
            pre = "allow"
            reason = decision.reason or None
        return HookPreOutcome(
            pre,
            reason,
            action=decision.action,
            requires_approval=requires_approval,
            updated_input=decision.updated_input,
            system_message=decision.system_message,
            additional_context=decision.additional_context,
        )

    async def after_tool(
        self,
        tool_name: str,
        result: Any,
        *,
        conversation_id: str = "",
    ) -> HookDecision:
        """Run the ``PostToolUse`` hooks and report what they added.

        Args:
            tool_name: The tool that ran.
            result: The tool's result.
            conversation_id: The conversation the call belongs to.

        Returns:
            The hooks' combined decision.  Its ``additional_context`` is
            the context for the model, and its ``system_message`` is a
            message for the user.
        """
        return await self._run_event(
            HookEvent.POST_TOOL_USE,
            tool_name=tool_name,
            result=result,
            conversation_id=conversation_id,
        )

    async def on_stop(
        self,
        *,
        conversation_id: str = "",
        tool_name: str = "",
    ) -> HookDecision:
        """Run the ``Stop`` hooks and report what they said.

        Args:
            conversation_id: The conversation that finished its turn.
            tool_name: The tool the turn ended on, when it ended on one.

        Returns:
            The hooks' combined decision.  Its ``system_message`` is a
            message the user sees, and its ``stop_reason`` says why the
            agent stopped.
        """
        return await self._run_event(
            HookEvent.STOP,
            tool_name=tool_name,
            conversation_id=conversation_id,
        )

    async def on_session_start(
        self,
        *,
        conversation_id: str = "",
        tool_name: str = "",
    ) -> HookDecision:
        """Run the ``SessionStart`` hooks and report what they said.

        Args:
            conversation_id: The conversation that started.
            tool_name: The tool the session starts with, when it names one.

        Returns:
            The hooks' combined decision.  Its ``additional_context`` is
            the context for the model.
        """
        return await self._run_event(
            HookEvent.SESSION_START,
            tool_name=tool_name,
            conversation_id=conversation_id,
        )

    async def _run_event(
        self,
        event: HookEvent,
        *,
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
        result: Any = None,
        conversation_id: str = "",
    ) -> HookDecision:
        """Run the hooks for one event and report their combined answer.

        Args:
            event: The moment the call reaches.
            tool_name: The tool the model called.
            arguments: The tool's arguments.
            result: The tool's result.
            conversation_id: The conversation the call belongs to.

        Returns:
            The combined decision, allowing the call when the hooks failed.
        """
        runner = self._runner_for()
        try:
            return await runner.run_event(
                event,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                conversation_id=conversation_id or self._resolve_conversation_id(),
            )
        except Exception:  # noqa: BLE001
            logger.warning("A %s hook could not be run", event.value, exc_info=True)
            return HookDecision()


__all__ = [
    "HookParticipant",
    "HookPreOutcome",
    "SessionEventHookAudit",
    "hook_audit_recorder",
]
