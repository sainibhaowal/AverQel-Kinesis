"""Turn replay phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from app.deepspace.services.chat_service.utils import (
    NATIVE_TOOL_REGISTRY,
    DeepSpaceToolExecutionError,
    _TurnState,
    sse,
)

logger = logging.getLogger(__name__)


class _TurnReplayMixin:
    async def _replay_pending_tool_results(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 1b: replay the tool results a resumed turn already produced."""
        if state.resume_denied:
            denied_pending = state.resumed_pending or {}
            state.pending_tool_name = str(denied_pending.get("tool_name") or "MCP tool")
            state.pending_call_id = str(denied_pending.get("call_id") or "")
            state.pending_arguments = denied_pending.get("tool_input")
            state.pending_step_id = str(
                denied_pending.get("step_id") or f"approval_{state.pending_call_id}"
            )
            if state.pending_call_id and isinstance(state.pending_arguments, dict):
                state.pending_call = {
                    "id": state.pending_call_id,
                    "type": "function",
                    "function": {
                        "name": state.pending_tool_name,
                        "arguments": json.dumps(
                            state.pending_arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                state.conversation_messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [state.pending_call],
                    }
                )
                denial_result = self._normalize_tool_failure_result(
                    state.pending_tool_name,
                    {
                        "success": False,
                        "error": (
                            f"The user denied the requested action ({state.pending_tool_name}); "
                            "nothing was performed."
                        ),
                        "error_category": "permission_denied",
                        "error_code": "USER_DENIED",
                    },
                )
                state.pending_output = json.dumps(
                    denial_result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                state.conversation_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": state.pending_call_id,
                        "content": state.pending_output,
                    }
                )
                state.tool_requirement_satisfied = True
                state.resume_denied = False
                state.terminal_status = "running"
                yield sse(
                    "permission_denied",
                    {
                        "approval_id": state.resume_approval_id,
                        "tool_name": state.pending_tool_name,
                        "tool_id": state.pending_call_id,
                        "step_id": state.pending_step_id,
                        "message": denial_result["error"],
                    },
                )
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.pending_tool_name,
                        "tool_id": state.pending_call_id,
                        "step_id": state.pending_step_id,
                        "tool_input": state.pending_arguments,
                        "error": state.pending_output,
                        "error_category": denial_result["error_category"],
                        "recoverable": True,
                        "recovery": denial_result["recovery"],
                        "turn_index": 0,
                        "completed_at": self._now(),
                    },
                )
                if state.run_id is not None:
                    self.runtime.record_step(
                        run_id=state.run_id,
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        step_type="tool_result",
                        status="failed",
                        tool_name=state.pending_tool_name,
                        tool_call_id=state.pending_call_id,
                        input_json=state.pending_arguments,
                        result_json={
                            "success": False,
                            "error_category": denial_result["error_category"],
                            "recoverable": True,
                            "output": state.pending_output,
                        },
                    )
                    self.runtime.update_checkpoint(
                        run_id=state.run_id,
                        status="running",
                        checkpoint={
                            "status": "running",
                            "phase": "tool_result",
                            "turn_index": 0,
                            "tool_name": state.pending_tool_name,
                            "tool_call_id": state.pending_call_id,
                            "tool_success": False,
                            "next_phase": "model",
                            "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                        },
                    )
                    self.runtime.clear_pending_approval(run_id=state.run_id)
            else:
                state.terminal_status = "blocked"
                state.forced_answer = (
                    f"The requested action ({state.pending_tool_name}) was denied; "
                    "nothing was performed."
                )
                yield sse(
                    "permission_denied",
                    {
                        "approval_id": state.resume_approval_id,
                        "tool_name": state.pending_tool_name,
                        "message": state.forced_answer,
                    },
                )
                if state.run_id is not None:
                    self.runtime.clear_pending_approval(run_id=state.run_id)
        elif state.resumed_pending is not None:
            state.pending_tool_name = str(state.resumed_pending.get("tool_name") or "")
            state.pending_binding = state.mcp_bindings.get(state.pending_tool_name)
            state.pending_call_id = str(state.resumed_pending.get("call_id") or "")
            state.pending_arguments = state.resumed_pending.get("tool_input")
            pending_is_native = NATIVE_TOOL_REGISTRY.contains(state.pending_tool_name)
            if (
                (state.pending_binding is None and not pending_is_native)
                or not isinstance(state.pending_arguments, dict)
                or not state.pending_call_id
            ):
                state.terminal_status = "blocked"
                state.forced_answer = (
                    "The approved action is no longer available because its tool, "
                    "connection, or catalog changed."
                )
                yield sse(
                    "permission_denied",
                    {"approval_id": state.resume_approval_id, "message": state.forced_answer},
                )
                if state.run_id is not None:
                    self.runtime.clear_pending_approval(run_id=state.run_id)
            else:
                state.pending_call = {
                    "id": state.pending_call_id,
                    "type": "function",
                    "function": {
                        "name": state.pending_tool_name,
                        "arguments": json.dumps(
                            state.pending_arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                state.approved_assistant_tool_call = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [state.pending_call],
                }
                state.approval_turn_index = state.resumed_pending.get("turn_index")
                if isinstance(state.approval_turn_index, int):
                    state.approved_assistant_tool_call[
                        "__deepspace_provider_continuation_turn_index"
                    ] = state.approval_turn_index
                state.conversation_messages.append(state.approved_assistant_tool_call)
                state.pending_step_id = str(
                    state.resumed_pending.get("step_id") or f"approval_{state.pending_call_id}"
                )
                yield sse(
                    "permission_granted",
                    {
                        "approval_id": state.resume_approval_id,
                        "tool_name": state.pending_tool_name,
                        "tool_id": state.pending_call_id,
                        "step_id": state.pending_step_id,
                    },
                )
                yield sse(
                    "tool_start",
                    {
                        "tool_name": state.pending_tool_name,
                        "tool_id": state.pending_call_id,
                        "step_id": state.pending_step_id,
                        "tool_input": state.pending_arguments,
                        "permission_level": "approved",
                        "turn_index": 0,
                        "started_at": self._now(),
                    },
                )
                state.pending_result = await self._run_approved_tool_call(
                    state=state,
                    tool_name=state.pending_tool_name,
                    arguments=state.pending_arguments,
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    web_provider=state.web_provider,
                    web_candidate=state.web_candidate,
                    web_search_options=state.web_search_options,
                    request=state.request,
                    loop_deadline=state.loop_deadline,
                    run_id=state.run_id,
                    mcp_binding=state.pending_binding,
                    mcp_bindings=state.mcp_bindings,
                    assistant_message_id=state.assistant_message.id,
                    user_prompt=state.prompt,
                    native_tool_names=state.native_tool_names,
                    mcp_budget=state.mcp_budget,
                )
                if not bool(state.pending_result.get("success")):
                    state.pending_result = self._normalize_tool_failure_result(
                        state.pending_tool_name,
                        state.pending_result,
                    )
                state.pending_success = bool(state.pending_result.get("success"))
                state.pending_payload = state.pending_result.get("payload")
                state.pending_output = (
                    json.dumps(state.pending_payload, ensure_ascii=False, separators=(",", ":"))
                    if state.pending_success
                    else json.dumps(
                        state.pending_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                state.conversation_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": state.pending_call_id,
                        "content": state.pending_output,
                    }
                )
                if state.run_id is not None:
                    self.runtime.record_step(
                        run_id=state.run_id,
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        step_type="mcp_tool_result",
                        status="completed" if state.pending_success else "failed",
                        tool_name=state.pending_tool_name,
                        tool_call_id=state.pending_call_id,
                        input_json=state.pending_arguments,
                        result_json={
                            "success": state.pending_success,
                            "error_category": (
                                state.pending_result.get("error_category")
                                if not state.pending_success
                                else None
                            ),
                            "recoverable": (
                                bool(state.pending_result.get("recoverable"))
                                if not state.pending_success
                                else False
                            ),
                            "output": state.pending_output,
                            **(
                                {"diff": state.pending_payload["diff"]}
                                if state.pending_success
                                and isinstance(state.pending_payload, dict)
                                and isinstance(state.pending_payload.get("diff"), dict)
                                else {}
                            ),
                        },
                    )
                    self.runtime.update_checkpoint(
                        run_id=state.run_id,
                        status="running",
                        checkpoint={
                            "status": "running",
                            "phase": "tool_result",
                            "turn_index": 0,
                            "tool_name": state.pending_tool_name,
                            "tool_call_id": state.pending_call_id,
                            "tool_success": state.pending_success,
                            "next_phase": "model",
                            "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                        },
                    )
                    self.runtime.clear_pending_approval(run_id=state.run_id)
                yield sse(
                    "tool_result" if state.pending_success else "tool_error",
                    {
                        "tool_name": state.pending_tool_name,
                        "tool_id": state.pending_call_id,
                        "step_id": state.pending_step_id,
                        "tool_input": state.pending_arguments,
                        "output" if state.pending_success else "error": state.pending_output,
                        **(
                            {"diff": state.pending_payload["diff"]}
                            if state.pending_success
                            and isinstance(state.pending_payload, dict)
                            and isinstance(state.pending_payload.get("diff"), dict)
                            else {}
                        ),
                        "success": state.pending_success,
                        "error_category": (
                            state.pending_result.get("error_category")
                            if not state.pending_success
                            else None
                        ),
                        "recoverable": (
                            bool(state.pending_result.get("recoverable"))
                            if not state.pending_success
                            else False
                        ),
                        "turn_index": 0,
                        "completed_at": self._now(),
                    },
                )
                if state.pending_success and isinstance(state.pending_payload, dict):
                    for frame in self._emit_queued_subagent_start(
                        state,
                        tool_name=state.pending_tool_name,
                        arguments=state.pending_arguments,
                        payload=state.pending_payload,
                        turn_index=0,
                    ):
                        yield frame
                    for bg_activity in self._poll_background_subagent_events(state):
                        self._record_subagent_activity(state, bg_activity)
                        yield sse("subagent_activity", bg_activity)
                if not state.pending_success and not bool(state.pending_result.get("recoverable")):
                    raise DeepSpaceToolExecutionError(
                        f"{state.pending_tool_name} failed: "
                        f"{state.pending_result.get('error') or 'tool failed safely.'}"
                    )
