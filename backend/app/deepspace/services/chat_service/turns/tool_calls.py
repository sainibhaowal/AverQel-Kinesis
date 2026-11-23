"""Turn tool_calls phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

from app.deepspace.services.chat_service.turns.tool_call_validation import _ToolCallValidationMixin
from app.deepspace.services.chat_service.utils import (
    MAX_CONNECTED_TOOL_RECOVERY_RETRIES,
    MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL,
    MAX_EMPTY_PROVIDER_RETRIES,
    MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN,
    MAX_TOOL_RETRIES,
    NATIVE_TOOL_REGISTRY,
    DeepSpaceEmptyResponseError,
    DeepSpaceToolExecutionError,
    _capability_failure_message,
    _TurnState,
    sse,
)
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL_NAMES,
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
)
from app.deepspace.services.runtime_transitions import RuntimePhase, transition_checkpoint
from app.deepspace.services.session_events import (
    STEP_END,
)
from app.deepspace.services.spill_policy import take_recovered_images
from app.providers.services.reasoning_capabilities import (
    is_deepseek_v4_model,
    uses_deepseek_reasoning_protocol,
)
from app.system.services.metrics_service import (
    DEEPSPACE_EXECUTION_BATCHES_TOTAL,
    DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL,
    DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL,
)

logger = logging.getLogger(__name__)


class _TurnToolCallsMixin(_ToolCallValidationMixin):
    def _request_model_final_answer_without_tools(
        self,
        state: _TurnState,
        *,
        reason: str,
    ) -> None:
        """Stop further tool execution and let the model write the final answer."""
        state.disable_tools_for_final_answer = True
        state.terminal_status = "blocked"
        self._set_dynamic_system_instruction(
            state.conversation_messages,
            key="stop_tools_final_answer",
            content=(
                "Tool execution has been stopped for this turn because: "
                f"{reason} "
                "Do not call any more tools. Respond directly to the user using only the "
                "tool results and errors already observed above, explaining what could or "
                "could not be accessed and the exact next step the user can take."
            ),
        )

    async def _handle_tool_calls(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 3 of a turn: validate, police, batch and run the model's tool calls."""
        if state.disable_tools_for_final_answer:
            state.control = "break"
            return
        try:
            workspace_root = state.conversation_workspace_root or self._resolve_workspace_root(
                state
            )
        except Exception:  # noqa: BLE001 - a missing root fails path checks closed.
            workspace_root = None
        self.tool_policy.set_workspace_root(workspace_root)
        state.normalized_calls = [call for _, call in state.normalized_call_items]
        state.provider_continuation = "".join(state.provider_continuation_parts)
        if (
            state.normalized_calls
            and uses_deepseek_reasoning_protocol(
                state.candidate.provider_type, state.candidate.model_name
            )
            and state.provider_continuation
        ):
            await self._persist_provider_continuation(
                reasoning_content=state.provider_continuation,
                auth=state.auth,
                conversation_id=state.conversation_id,
                assistant_message=state.assistant_message,
                turn_index=state.round_index,
            )
        state.round_has_text = (
            len(state.answer_parts) > state.round_answer_start
            or len(state.thinking_parts) > state.round_thinking_start
            or len(state.generated_artifacts) > state.round_artifact_start
        )
        yield sse(
            "metrics",
            {
                "contextUsedTokens": state.last_context_used_tokens,
                "contextRemainingTokens": state.last_context_remaining_tokens,
                "contextUsage": state.last_context_usage,
                "contextUsageSource": "estimated_local",
                "sessionInputTokens": state.session_input_tokens,
                "sessionOutputTokens": state.session_output_tokens,
                "sessionTotalTokens": state.session_input_tokens + state.session_output_tokens,
                "requestInputTokens": state.request_input_tokens,
                "requestOutputTokens": state.request_output_tokens,
                "requestInputTokensSource": "serialized_request_estimate",
                "requestOutputTokensSource": "serialized_response_estimate",
                "maxOutputTokens": state.request_max_tokens,
                "providerUsage": state.last_provider_usage,
                "providerUsageScope": (
                    "latest_reported_round"
                    if state.last_provider_usage.get("usage_source") == "provider"
                    else "unavailable"
                ),
                "providerUsageRoundCount": state.provider_usage_rounds_seen,
                "providerTurnUsage": state.provider_turn_usage_snapshot(),
                "cachedInputTokens": None,
                "uncachedInputTokens": state.last_context_used_tokens,
                "tokenCategorySource": "serialized_request_estimate",
                **self._context_budget_state(
                    used_tokens=state.last_context_used_tokens or 0,
                    context_limit=state.candidate.context_window,
                    reserved_output_tokens=state.last_reserved_output_tokens,
                    compacted=state.last_context_compacted,
                ),
                **(
                    {"contextLimit": state.candidate.context_window}
                    if state.candidate.context_window
                    else {}
                ),
                **(
                    {"contextLimitSource": state.candidate.context_window_source}
                    if state.candidate.context_window_source
                    else {}
                ),
            },
        )
        if not state.normalized_calls and not state.round_has_text:
            if state.empty_provider_retries < MAX_EMPTY_PROVIDER_RETRIES:
                state.empty_provider_retries += 1
                state.control = "continue"
                return
            raise DeepSpaceEmptyResponseError(
                f"{state.candidate.provider_type}/{state.candidate.model_name} returned no answer, reasoning, or tool events."
            )
        if not state.normalized_calls:
            state.prose_answer = "".join(state.answer_parts[state.round_answer_start :]).strip()
            state.round_thinking = "".join(state.thinking_parts[state.round_thinking_start :])
            # Provider reasoning is activity metadata, not a usable
            # answer. Some reasoning-capable models can terminate a
            # turn after emitting only thinking events. Without this
            # guard, the loop falls through to finalization, persists
            # an empty assistant message, and emits `done` as if the
            # turn completed successfully.
            if (
                not state.prose_answer
                and not state.generated_artifacts[state.round_artifact_start :]
            ):
                if state.empty_provider_retries < MAX_EMPTY_PROVIDER_RETRIES:
                    state.empty_provider_retries += 1
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="reasoning_only_recovery",
                        content=(
                            "Your previous response contained only internal reasoning. "
                            "Return a concise user-visible answer now. Do not stop after "
                            "thinking and do not expose internal reasoning as the answer."
                        ),
                    )
                    state.control = "continue"
                    return
                raise DeepSpaceEmptyResponseError(
                    f"{state.candidate.provider_type}/{state.candidate.model_name} returned reasoning "
                    "without a user-visible answer."
                )
            state.fake_tool_output = self._looks_like_fake_tool_output(
                f"{state.prose_answer}\n{state.round_thinking}"
            )
            if state.fake_tool_output or (
                state.connected_service_tool_required
                and not state.connected_service_action_completed
            ):
                del state.answer_parts[state.round_answer_start :]
                del state.thinking_parts[state.round_thinking_start :]
                if state.connected_tool_recovery_retries < MAX_CONNECTED_TOOL_RECOVERY_RETRIES:
                    state.connected_tool_recovery_retries += 1
                    self._record_model_message_step(
                        run_id=state.run_id,
                        auth=state.auth,
                        conversation_id=state.conversation_id,
                        text=state.prose_answer,
                        turn_index=state.round_index,
                    )
                    yield sse(
                        "model_message",
                        {
                            "text": state.prose_answer,
                            "turn_index": state.round_index,
                            "status": "completed",
                        },
                    )
                    yield sse("replace", {"content": "", "replayed": False})
                    state.recovery_instruction = (
                        "Your prior response printed tool-shaped JSON, XML, DSML, or function-call "
                        "syntax as ordinary text. Do not print tool syntax. Reply to the user in "
                        "plain language, or use a real structured tool call only when the task needs one."
                    )
                    if state.connected_service_tool_required:
                        state.recovery_instruction = (
                            "This request requires a real structured call to the attached service. "
                            "Do not print <function-call>, JSON, XML, or internal reasoning as text. "
                            "Call the appropriate provided MCP tool now. If no suitable tool exists, "
                            "say so only after this structured-call attempt."
                        )
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="tool_output_recovery",
                        content=state.recovery_instruction,
                    )
                    state.control = "continue"
                    return
                if (
                    state.connected_service_tool_required
                    and not state.connected_service_action_completed
                ):
                    state.terminal_status = "blocked"
                    state.forced_answer = self._connected_service_failure_message(
                        state.mcp_bindings
                    )
                    state.control = "break"
                    return
                if state.fake_tool_output:
                    state.terminal_status = "blocked"
                    state.forced_answer = (
                        "Hello! How can I help you in DeepSpace today?"
                        if state.defer_task_for_greeting
                        else (
                            "DeepSpace stopped safely because the selected model printed a fake tool "
                            "call instead of using the structured tool interface. No tool action was performed."
                        )
                    )
                    state.control = "break"
                    return
            if state.managed_task_run:
                if state.task_lifecycle_stage == "final":
                    # The persisted task check has completed or recorded
                    # a terminal blocker. The user-facing response is
                    # ordinary assistant text, with no final tool call.
                    state.control = "break"
                    return
                state.task_lifecycle_prompt_retries += 1
                if state.task_lifecycle_prompt_retries <= MAX_EMPTY_PROVIDER_RETRIES + 1:
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="task_lifecycle",
                        content=self._task_lifecycle_instruction(
                            stage=state.task_lifecycle_stage,
                            task_id=state.active_task_id,
                        ),
                    )
                    state.control = "continue"
                    return
                state.terminal_status = "blocked"
                # Do not manufacture a tool call or mark work complete.
                # If the model did provide a useful prose response, keep
                # it visible while the durable plan remains paused; this
                # avoids turning an adapter capability mismatch into a
                # synthetic system fault.
                state.forced_answer = "".join(
                    state.answer_parts[state.round_answer_start :]
                ).strip() or (
                    "DeepSpace paused this task because the selected model did not return the "
                    "required structured tool call. The plan and its unfinished work remain saved."
                )
                state.control = "break"
                return
            if (
                state.tool_call_required
                and not state.tool_requirement_satisfied
                and not state.connected_service_tool_required
            ):
                if state.available_tools and state.tool_call_recovery_retries < MAX_TOOL_RETRIES:
                    state.tool_call_recovery_retries += 1
                    if state.prose_answer:
                        self._record_model_message_step(
                            run_id=state.run_id,
                            auth=state.auth,
                            conversation_id=state.conversation_id,
                            text=state.prose_answer,
                            turn_index=state.round_index,
                        )
                        yield sse(
                            "model_message",
                            {
                                "text": state.prose_answer,
                                "turn_index": state.round_index,
                                "status": "completed",
                            },
                        )
                    del state.answer_parts[state.round_answer_start :]
                    del state.thinking_parts[state.round_thinking_start :]
                    yield sse("replace", {"content": "", "replayed": False})
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="required_live_tool_call",
                        content=(
                            "You answered without using a structured tool. This request still "
                            "requires a real tool result. Call the most relevant supplied tool now. "
                            "Do not repeat a prose answer or claim the check was completed."
                        ),
                    )
                    state.control = "continue"
                    return
                state.terminal_status = "blocked"
                state.forced_answer = (
                    "I could not complete that live check because the model did not call a "
                    "supplied tool. No check was performed. Try again with a tool-capable model "
                    "and a preset that permits the needed tool."
                )
                state.control = "break"
                return
            if state.available_tools and self._looks_like_clarification_request(state.prose_answer):
                if state.clarification_retries < 1:
                    state.clarification_retries += 1
                    # The first response was streamed optimistically.
                    # Replace it in the UI before retrying so users do
                    # not see a duplicate prose question above the
                    # eventual interactive card.
                    del state.answer_parts[state.round_answer_start :]
                    del state.thinking_parts[state.round_thinking_start :]
                    self._record_model_message_step(
                        run_id=state.run_id,
                        auth=state.auth,
                        conversation_id=state.conversation_id,
                        text=state.prose_answer,
                        turn_index=state.round_index,
                    )
                    yield sse(
                        "model_message",
                        {
                            "text": state.prose_answer,
                            "turn_index": state.round_index,
                            "status": "completed",
                        },
                    )
                    yield sse("replace", {"content": "", "replayed": False})
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="clarification_recovery",
                        content=(
                            "Your last response asked the user for clarification in prose. "
                            "This turn requires an interactive clarification: call the ask_user "
                            "tool now with one concise question and optional choices. Do not answer "
                            "in prose and do not call another tool first."
                        ),
                    )
                    state.control = "continue"
                    return
                # A second provider response still asked for missing
                # information without using ask_user. Convert only
                # this narrowly detected case into the same persisted
                # question lifecycle used by a real tool call.
                state.question_id = str(uuid.uuid4())
                state.awaiting_user = {
                    "awaiting_user": True,
                    "question": state.prose_answer[:2000],
                    "options": [],
                    "question_id": state.question_id,
                    "call_id": f"clarification_{state.question_id}",
                    "step_id": f"clarification_{state.round_index}",
                    "tool_name": "ask_user",
                    "tool_input": {
                        "question": state.prose_answer[:2000],
                        "options": [],
                    },
                    "turn_index": state.round_index,
                }
                del state.answer_parts[state.round_answer_start :]
                del state.thinking_parts[state.round_thinking_start :]
                self._record_model_message_step(
                    run_id=state.run_id,
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    text=state.prose_answer,
                    turn_index=state.round_index,
                )
                yield sse(
                    "model_message",
                    {
                        "text": state.prose_answer,
                        "turn_index": state.round_index,
                        "status": "completed",
                    },
                )
                yield sse("replace", {"content": "", "replayed": False})
                yield sse(
                    "ask_user_question",
                    {
                        "tool_name": "ask_user",
                        "tool_id": state.awaiting_user["call_id"],
                        "step_id": state.awaiting_user["step_id"],
                        "message": state.awaiting_user["question"],
                        "options": [],
                        "question_id": state.question_id,
                        "turn_index": state.round_index,
                    },
                )
                state.control = "break"
                return
            state.control = "break"
            return
        # Prose emitted before a real structured tool call is model
        # narration for this turn, not the final user-facing answer.
        # Preserve it as a durable timeline step instead of letting
        # it remain at the bottom and then disappear on completion.
        state.prose_before_tools = "".join(state.answer_parts[state.round_answer_start :]).strip()
        if state.prose_before_tools:
            self._record_model_message_step(
                run_id=state.run_id,
                auth=state.auth,
                conversation_id=state.conversation_id,
                text=state.prose_before_tools,
                turn_index=state.round_index,
            )
            yield sse(
                "model_message",
                {
                    "text": state.prose_before_tools,
                    "turn_index": state.round_index,
                    "status": "completed",
                },
            )
            del state.answer_parts[state.round_answer_start :]
            yield sse("replace", {"content": "", "replayed": False})
        state.tool_exchange_start = len(state.conversation_messages)
        state.assistant_tool_message = {
            "role": "assistant",
            "content": "" if is_deepseek_v4_model(state.candidate.model_name) else None,
            "tool_calls": state.normalized_calls,
        }
        if (
            uses_deepseek_reasoning_protocol(
                state.candidate.provider_type, state.candidate.model_name
            )
            and state.provider_continuation
        ):
            state.assistant_tool_message["reasoning_content"] = state.provider_continuation
        state.conversation_messages.append(state.assistant_tool_message)
        # Record message time for time context
        if hasattr(state, "time_context_service"):
            state.time_context_service.record_message_time(state.conversation_id, time.time())
        async for event in self._validate_tool_calls(state):
            yield event

        state.invalid_tool_error_messages.extend(state.round_invalid_tool_error_messages)
        state.invalid_tool_names.extend(state.round_invalid_tool_names)

        if state.invalid_tool_call_ids:
            state.assistant_tool_message = state.conversation_messages[state.tool_exchange_start]
            state.retained_calls = [
                call
                for call in state.assistant_tool_message.get("tool_calls", [])
                if str(call.get("id") or "") not in state.invalid_tool_call_ids
            ]
            state.retained_messages = [
                message
                for message in state.conversation_messages[state.tool_exchange_start + 1 :]
                if not (
                    message.get("role") == "tool"
                    and str(message.get("tool_call_id") or "") in state.invalid_tool_call_ids
                )
            ]
            del state.conversation_messages[state.tool_exchange_start :]
            if state.retained_calls:
                state.conversation_messages.append(
                    {
                        **state.assistant_tool_message,
                        "tool_calls": state.retained_calls,
                    }
                )
            state.conversation_messages.extend(state.retained_messages)

        if (
            state.invalid_tool_arguments
            and not state.valid_calls
            and state.awaiting_approval is None
        ):
            if state.invalid_tool_argument_retries < MAX_TOOL_RETRIES:
                state.invalid_tool_argument_retries += 1
                state.validation_details = " ".join(state.invalid_tool_error_messages[-3:])[:2_000]
                self._set_dynamic_system_instruction(
                    state.conversation_messages,
                    key="invalid_tool_arguments",
                    content=(
                        "Your previous structured tool arguments failed schema validation. Retry the "
                        "same action using exactly one valid JSON object matching the supplied tool "
                        "schema. Preserve required fields, keep bounded fields within their limits, "
                        "and do not write tool markup or prose. Validation details: "
                        f"{state.validation_details}"
                    ),
                )
                state.control = "continue"
                return
            state.validation_details = " ".join(state.invalid_tool_error_messages[-3:])[:1_500]
            state.validation_tool_name = (
                state.invalid_tool_names[-1] if state.invalid_tool_names else None
            )
            if state.run_id is not None:
                try:
                    self.runtime.update_checkpoint(
                        run_id=state.run_id,
                        status="failed",
                        checkpoint={
                            "status": "failed",
                            "phase": "tool_validation",
                            "turn_index": state.round_index,
                            "tool_name": state.validation_tool_name,
                            "error_category": "validation",
                            "validation_message": state.validation_details,
                            "resume_available": True,
                            "next_action": "retry_tool_arguments",
                            "task_lifecycle_stage": state.task_lifecycle_stage,
                            "active_task_id": state.active_task_id,
                        },
                        last_error="invalid_tool_arguments",
                    )
                except Exception:  # noqa: BLE001
                    # A checkpoint write must not replace the original
                    # validation result with a storage error.
                    logger.warning(
                        "DeepSpace could not persist invalid-tool checkpoint",
                        exc_info=True,
                    )
            state.forced_answer = (
                "DeepSpace could not validate the tool arguments after one correction attempt. "
                "Completed tool results and the current task checkpoint were saved. Retry to "
                "continue from the last successful step."
            )
            state.terminal_status = "failed"
            state.control = "break"
            return

        if state.awaiting_approval is not None:
            state.valid_calls = []
            if state.run_id is not None:
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="awaiting_approval",
                    checkpoint={
                        "status": "awaiting_approval",
                        "turn_index": state.round_index,
                        "phase": "approval",
                        "pending_approval": state.awaiting_approval,
                        "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                    },
                )
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="approval_requested",
                    status="awaiting_approval",
                    tool_name=str(state.awaiting_approval["tool_name"]),
                    tool_call_id=str(state.awaiting_approval["call_id"]),
                    input_json=dict(state.awaiting_approval["tool_input"]),
                    result_json={
                        "approval_id": str(state.awaiting_approval["approval_id"]),
                        "risk_level": str(state.awaiting_approval["risk_level"]),
                    },
                )
            yield sse("permission_request", state.awaiting_approval)
            yield sse("approval_request", state.awaiting_approval)

        if state.run_id is not None and state.valid_calls:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.MODEL_REQUESTED,
                    next_phase=RuntimePhase.ACTIONS_VALIDATED,
                    turn_index=state.round_index,
                    details={"tool_count": len(state.valid_calls)},
                ),
            )
        for state.item in state.valid_calls:  # noqa: B020
            state.tool_name = str(state.item["tool_name"])
            if state.run_id is not None:
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="tool_start",
                    status="running",
                    tool_name=state.tool_name,
                    tool_call_id=str(state.item["call_id"]),
                    input_json=state.item["arguments"],
                    result_json={"turn_index": state.round_index},
                )
            yield sse(
                "tool_start",
                {
                    "tool_name": state.tool_name,
                    "tool_id": state.item["call_id"],
                    "step_id": state.item["step_id"],
                    "tool_input": state.item["arguments"],
                    "permission_level": "auto",
                    "turn_index": state.round_index,
                    "started_at": self._now(),
                },
            )

        state.read_semaphore = asyncio.Semaphore(
            max(
                1,
                min(
                    int(getattr(self.settings, "deepspace_max_concurrent_tools", 8)),
                    32,
                ),
            )
        )
        state.write_lock = asyncio.Lock()
        state.execution_batches = self._plan_tool_execution_batches(
            state.valid_calls,
            mcp_bindings=state.mcp_bindings,
        )
        if state.run_id is not None:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.ACTIONS_VALIDATED,
                    next_phase=RuntimePhase.EXECUTING,
                    turn_index=state.round_index,
                    details={
                        "tool_count": len(state.valid_calls),
                        "batch_count": len(state.execution_batches),
                    },
                ),
            )
            self.runtime.record_step(
                run_id=state.run_id,
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                step_type="execution_batch_plan",
                status="running",
                result_json={
                    "batch_count": len(state.execution_batches),
                    "parallel_read_batches": sum(
                        1 for batch in state.execution_batches if batch.parallel
                    ),
                    "tool_count": len(state.valid_calls),
                },
            )
            DEEPSPACE_EXECUTION_BATCHES_TOTAL.labels(mode="planned").inc(
                len(state.execution_batches)
            )

        state.results_by_call_id = {}
        for state.batch in state.execution_batches:  # noqa: B020
            state.coroutines = [
                self._run_tool_call(
                    tool_name=str(item["tool_name"]),
                    arguments=item["arguments"],
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    web_provider=state.web_provider,
                    web_candidate=state.web_candidate,
                    web_search_options=state.web_search_options,
                    request=state.request,
                    loop_deadline=state.loop_deadline,
                    run_id=state.run_id,
                    turn=state.turn_number,
                    step=state.round_index,
                    read_semaphore=state.read_semaphore,
                    write_lock=state.write_lock,
                    mcp_binding=state.mcp_bindings.get(str(item["tool_name"])),
                    mcp_bindings=state.mcp_bindings,
                    assistant_message_id=state.assistant_message.id,
                    ignore_existing_tasks=(
                        not state.allow_existing_task_state and not state.managed_task_run
                    ),
                    user_prompt=state.prompt,
                    native_tool_names=state.native_tool_names,
                )
                for item in state.batch.calls
            ]
            tool_spans = [
                (
                    str(item["call_id"]),
                    self._open_telemetry_span(
                        state,
                        self.session_telemetry.tool_span(
                            state.conversation_id,
                            state.round_index,
                            int(item.get("step", 0) or 0),
                            str(item["tool_name"]),
                        ),
                    ),
                )
                for item in state.batch.calls
            ]
            state.batch_results = (
                await asyncio.gather(*state.coroutines)
                if state.batch.parallel
                else [await state.coroutines[0]]
            )
            state.tool_call_count = (state.tool_call_count or 0) + len(state.batch.calls)
            # Record repeat tool reminders for native tool calls in this batch
            tracker = self.repeat_tool_reminder(state.conversation_id)
            if tracker is not None:
                for item, _result in zip(state.batch.calls, state.batch_results, strict=True):
                    tool_name = str(item.get("tool_name") or "")
                    if NATIVE_TOOL_REGISTRY.contains(tool_name):
                        arguments = item.get("arguments") or {}
                        reminder = tracker.record(
                            str(state.conversation_id),
                            tool_name,
                            arguments,
                        )
                        if reminder:
                            state.repeat_tool_reminders = getattr(
                                state, "repeat_tool_reminders", []
                            )
                            state.repeat_tool_reminders.append(
                                {"tool_name": tool_name, "message": reminder}
                            )
            for call_id, handle in tool_spans:
                result = next(
                    (item for item in state.batch_results if str(item.get("call_id")) == call_id),
                    None,
                )
                self.session_telemetry.record_tool_result(
                    handle.span,
                    success=bool(result.get("success")) if result else False,
                    duration_ms=int(result.get("duration_ms") or 0) if result else 0,
                    error=(result.get("error") if result else None),
                )
                handle.close()
            state.results_by_call_id.update(
                {
                    str(item["call_id"]): result
                    for item, result in zip(state.batch.calls, state.batch_results, strict=True)
                }
            )
        state.results = [
            state.results_by_call_id[str(item["call_id"])] for item in state.valid_calls
        ]
        if isinstance(state.mcp_budget, dict):
            state.mcp_call_count = max(
                int(state.mcp_call_count or 0), int(state.mcp_budget.get("calls", 0))
            )
            state.mcp_discovery_call_count = max(
                int(state.mcp_discovery_call_count or 0),
                int(state.mcp_budget.get("discovery", 0)),
            )
            state.mcp_result_chars = max(
                int(state.mcp_result_chars or 0),
                int(state.mcp_budget.get("result_chars", 0)),
            )
        if any(
            str(item.get("tool_name") or "") != "ask_user"
            and (
                str(item.get("tool_name") or "") not in {MCP_SEARCH_TOOLS, MCP_GET_TOOL_SCHEMA}
                or (state.tool_call_required and not state.connected_service_tool_required)
            )
            for item in state.valid_calls
        ):
            # A structured action actually reached the executor. A denied or
            # failed result is still useful evidence for the model to explain.
            # Catalog/schema calls satisfy a catalog request, but they do not
            # satisfy a request to act inside the connected service.
            state.tool_requirement_satisfied = True
        if state.run_id is not None and state.valid_calls:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.EXECUTING,
                    next_phase=RuntimePhase.RESULTS_RECORDED,
                    turn_index=state.round_index,
                    details={"tool_count": len(state.valid_calls)},
                ),
            )
        for state.item, state.result in zip(  # noqa: B020
            state.valid_calls, state.results, strict=True
        ):
            state.tool_name = str(state.item["tool_name"])
            state.call_id = str(state.item["call_id"])
            state.step_id = str(state.item["step_id"])
            state.success = bool(state.result.get("success"))
            if not state.success:
                state.result = self._normalize_tool_failure_result(state.tool_name, state.result)
            state.raw_tool_payload = state.result.get("payload")
            state.tool_payload = (
                state.raw_tool_payload if isinstance(state.raw_tool_payload, dict) else {}
            )
            if state.success and self._coordination_read_no_progress(state):
                read_streak = int((state.coordination_read_streaks or {}).get(state.tool_name, 0))
                state.tool_payload = {
                    "status": "ok",
                    "stop_turn": True,
                    "coordination_guard": True,
                    "error_code": "COORDINATION_NO_PROGRESS",
                    "read_streak": read_streak,
                    "message": (
                        "The team state did not change after repeated coordination reads. "
                        "Stop polling and answer from the latest observation; saved progress remains available."
                    ),
                }
            state.guard_refusal = bool(
                state.success
                and (
                    state.tool_payload.get("stop_turn") is True
                    or state.tool_payload.get("duplicate_guard") is True
                )
            )
            if state.success:
                state.is_mcp_payload = (
                    state.tool_name in MCP_BROKER_TOOL_NAMES
                    or state.mcp_bindings.get(state.tool_name) is not None
                )
                if state.is_mcp_payload:
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_result_chars = max(
                            int(state.mcp_result_chars or 0),
                            int(state.mcp_budget.get("result_chars", 0)),
                        )
                    state.payload_chars = len(
                        json.dumps(state.tool_payload, ensure_ascii=False, default=str)
                    )
                    state.remaining_result_chars = max(
                        0, self.mcp_max_result_chars_per_turn - state.mcp_result_chars
                    )
                    if state.payload_chars > state.remaining_result_chars:
                        state.tool_payload = self.mcp_broker.bound_result(
                            state.tool_payload,
                            max_chars=state.remaining_result_chars,
                        )
                    state.mcp_result_chars += len(
                        json.dumps(state.tool_payload, ensure_ascii=False, default=str)
                    )
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_budget["result_chars"] = max(
                            int(state.mcp_budget.get("result_chars", 0)),
                            int(state.mcp_result_chars),
                        )
                if state.mcp_bindings.get(state.tool_name) is not None:
                    # Keep the bounded full result in tenant-scoped,
                    # short-lived server storage. The model receives
                    # only a compact preview plus an opaque reference;
                    # it can request more through the broker when
                    # genuinely needed.
                    state.result_ref = self.mcp_result_store.put(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        value=state.tool_payload,
                    )
                    DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL.inc()
                    DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                        kind="mcp", status="created"
                    ).inc()
                    state.preview = json.dumps(
                        state.tool_payload, ensure_ascii=False, default=str, separators=(",", ":")
                    )
                    state.tool_payload = {
                        "status": "ok",
                        "result_ref": state.result_ref,
                        "preview": state.preview[: state.round_budget.mcp_preview_chars],
                        "preview_truncated": len(state.preview)
                        > state.round_budget.mcp_preview_chars,
                        "preview_chars_limit": state.round_budget.mcp_preview_chars,
                        "message": (
                            "Use mcp_broker with operation=result and this result_ref "
                            "only if more detail is required."
                        ),
                    }
                if state.mcp_bindings.get(state.tool_name) is not None:
                    # A successful MCP result satisfies the connected
                    # service requirement for this turn. The model may
                    # now provide a normal final answer without being
                    # forced through another structured-call retry.
                    state.connected_service_action_completed = True
                if state.tool_name == "write" and state.item["arguments"].get("target") == "memory":
                    state.memory_written_this_turn = True
                if state.tool_name == "spill_read":
                    state.pending_images.extend(take_recovered_images(state.tool_payload))
                state.raw_image = state.tool_payload.get("_image_base64")
                if isinstance(state.raw_image, str) and state.raw_image:
                    state.pending_images.append(state.raw_image)
                elif state.tool_name == "read_image":
                    # A local image is handed to the model as an image part,
                    # and its bytes are kept out of the stored result so the
                    # session log does not carry megabytes of base64.
                    image_data = state.tool_payload.get("data")
                    if isinstance(image_data, str) and image_data:
                        state.pending_images.append(image_data)
                        state.tool_payload = {
                            key: value for key, value in state.tool_payload.items() if key != "data"
                        }
                elif state.tool_name in {"browser_screenshot", "computer_screenshot"}:
                    state.artifact = self._consume_screenshot_tool_result(state)
                    if isinstance(state.artifact, dict) and state.artifact.get("id"):
                        yield sse(
                            "artifact",
                            {"artifact": state.artifact, "turn_index": state.round_index},
                        )
                state.tool_payload = self.tool_policy.after_tool(
                    state.tool_name, state.tool_payload
                )
                if (
                    state.tool_name in {"bash", "pwsh"}
                    and state.tool_payload.get("failure_kind") == "command_not_found"
                ):
                    state.forced_answer = (
                        "The shell could not find a requested command in this runtime's PATH. "
                        "I stopped instead of repeating it; no live result was obtained."
                    )
                    state.terminal_status = "blocked"
                if state.guard_refusal:
                    capability_failure_message = _capability_failure_message(state.tool_payload)
                    if capability_failure_message is not None:
                        state.forced_answer = capability_failure_message
                    elif state.tool_payload.get("coordination_guard") is True:
                        state.forced_answer = (
                            "DeepSpace stopped repeated team-status polling because no new "
                            "coordination state appeared. The latest saved team progress remains available."
                        )
                    elif state.tool_payload.get("limit") == "filesystem_search_calls":
                        state.forced_answer = (
                            "DeepSpace stopped after four filesystem searches to prevent another "
                            "unbounded retry loop. It did not run another shell command. Use the "
                            "completed search results above to report the paths checked, any "
                            "permission-denied or skipped paths, and whether the search was "
                            "truncated. Do not claim the file is absent if the result was incomplete."
                        )
                    else:
                        state.forced_answer = (
                            "DeepSpace stopped this turn at its safety limit. No additional tool "
                            "action was executed; saved progress remains available."
                        )
                    state.terminal_status = "blocked"
                # Native results use the same scoped-reference pattern
                # as MCP once their model-visible category budget is
                # exhausted. The stored value is post-policy and can
                # only be retrieved by this tenant/user/conversation.
                if (
                    not state.is_mcp_payload
                    and state.tool_name != "get_tool_result"
                    and state.round_budget.tool_result_budget_tokens is not None
                ):
                    state.rendered_payload = json.dumps(
                        state.tool_payload,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                    # A result far larger than the context window is spilled
                    # to durable storage first.  The short-lived reference
                    # below is a convenience; the spill is the only remaining
                    # copy, so it must survive the reference's expiry.
                    if (
                        len(state.rendered_payload) > self._spill_threshold_chars()
                        and state.tool_name != "spill_read"
                    ):
                        state.spilled_payload, state.spill_ref = self._spill_tool_result(
                            auth=state.auth,
                            tool_name=state.tool_name,
                            payload=state.tool_payload,
                            preview_budget_tokens=max(
                                64,
                                min(
                                    1_024,
                                    int(state.round_budget.tool_result_budget_tokens or 1_024) // 4,
                                ),
                            ),
                        )
                        if state.spill_ref is not None:
                            state.tool_payload = state.spilled_payload
                            if not any(
                                str(tool.get("function", {}).get("name") or "") == "spill_read"
                                for tool in state.available_tools
                            ):
                                state.available_tools.append(
                                    NATIVE_TOOL_REGISTRY.emit("spill_read")
                                )
                            DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                                kind="spill", status="created"
                            ).inc()
                        elif state.spilled_payload.get("status") == "refused":
                            # The result was too large to keep, so the
                            # refusal is what the model reads and the
                            # preview below carries the refusal rather than
                            # the result that was too large.
                            state.tool_payload = state.spilled_payload
                            state.rendered_payload = json.dumps(
                                state.tool_payload,
                                ensure_ascii=False,
                                default=str,
                                separators=(",", ":"),
                            )
                    state.native_limit_chars = state.round_budget.tool_result_budget_tokens * 4
                    state.remaining_native_chars = max(
                        0, state.native_limit_chars - state.native_result_chars
                    )
                    state.preview_limit = min(
                        state.round_budget.mcp_preview_chars,
                        state.remaining_native_chars,
                    )
                    if len(state.rendered_payload) > state.preview_limit:
                        state.result_ref = self.tool_result_store.put(
                            tenant_id=state.auth.tenant_id,
                            user_id=state.auth.user_id,
                            conversation_id=state.conversation_id,
                            value=state.tool_payload,
                        )
                        if not any(
                            str(tool.get("function", {}).get("name") or "") == "get_tool_result"
                            for tool in state.available_tools
                        ):
                            state.available_tools.append(
                                NATIVE_TOOL_REGISTRY.emit("get_tool_result")
                            )
                        state.tool_payload = {
                            "status": "ok",
                            "result_ref": state.result_ref,
                            "preview": state.rendered_payload[: state.preview_limit],
                            "preview_truncated": True,
                            "preview_chars_limit": state.preview_limit,
                            "message": "Use get_tool_result with this result_ref only if more detail is required.",
                        }
                        DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                            kind="native", status="created"
                        ).inc()
                    state.native_result_chars += min(
                        len(state.rendered_payload), state.preview_limit
                    )
                # Materialize sandbox outputs as private, durable
                # artifacts and expose them through the same timeline
                # used by provider-generated media. Input files remain
                # ephemeral inside the executor and are never copied.
                if state.tool_name == "sandbox_execute":
                    state.persisted = []
                    for state.generated in state.tool_payload.get("files", []):  # noqa: B020
                        if not isinstance(state.generated, dict):
                            continue
                        state.encoded = state.generated.get("data_base64")
                        if not isinstance(state.encoded, str) or not state.encoded:
                            continue
                        try:
                            state.artifact = self.media_artifacts.persist_content_base64(
                                tenant_id=state.auth.tenant_id,
                                user_id=state.auth.user_id,
                                conversation_id=state.conversation_id,
                                message_id=state.assistant_message.id,
                                content_type=str(
                                    state.generated.get("content_type")
                                    or "application/octet-stream"
                                ),
                                data_base64=state.encoded,
                                provider_type="sandbox",
                                model_name=None,
                                title=str(state.generated.get("name") or "Sandbox output"),
                                metadata={
                                    "source": "sandbox_execute",
                                    "turn_index": state.round_index,
                                },
                                requested_kind=(
                                    "chart"
                                    if str(state.generated.get("content_type") or "").startswith(
                                        ("image/", "image/svg")
                                    )
                                    else None
                                ),
                            )
                        except (ValueError, TypeError):
                            logger.warning("Rejected unsafe sandbox output artifact")
                            continue
                        state.persisted.append(state.artifact)
                        state.generated_artifacts.append(state.artifact)
                        yield sse(
                            "artifact",
                            {"artifact": state.artifact, "turn_index": state.round_index},
                        )
                    if state.persisted:
                        state.tool_payload["artifacts"] = state.persisted
                    # Never send binary base64 back through the model
                    # context or browser timeline; artifact metadata is
                    # sufficient for the user to preview/download it.
                    state.tool_payload["files"] = [
                        {
                            "name": artifact.get("title"),
                            "content_type": artifact.get("content_type"),
                            "size_bytes": artifact.get("size_bytes"),
                            "artifact_id": artifact.get("id"),
                        }
                        for artifact in state.persisted
                    ]
                state.panel_artifact = state.tool_payload.get("panel_artifact")
                if isinstance(state.panel_artifact, dict) and state.panel_artifact.get("id"):
                    state.generated_artifacts.append(state.panel_artifact)
                    yield sse(
                        "artifact",
                        {"artifact": state.panel_artifact, "turn_index": state.round_index},
                    )
                if state.tool_name in {
                    "web_search",
                    "web_fetch",
                    "url_read",
                    "image_read",
                } and not state.tool_payload.get("is_error", False):
                    if state.tool_name == "web_search":
                        state.native_research_stats["searches"] += 1
                        state.native_research_stats["results"] += len(
                            [
                                item
                                for item in state.tool_payload.get("citations", [])
                                if isinstance(item, dict)
                            ]
                        )
                    elif state.tool_name in {"web_fetch", "url_read"}:
                        if state.tool_payload.get("retrieval_status") in {
                            "blocked",
                            "unavailable",
                        }:
                            state.native_research_stats["blocked_pages"] += 1
                        else:
                            state.native_research_stats["pages"] += 1
                    else:
                        state.native_research_stats["images"] += 1
                    for state.citation in state.tool_payload.get("citations", []):  # noqa: B020
                        if isinstance(state.citation, dict):
                            state.citation_status = state.citation.get("retrieval_status")
                            if state.tool_name == "web_search" and not state.citation_status:
                                state.citation_status = "search_snippet_only"
                            state.citations.append(
                                {
                                    **state.citation,
                                    "retrieval_status": state.citation_status,
                                    "id": len(state.citations) + 1,
                                }
                            )
                    if state.tool_name == "image_read" and not state.tool_payload.get("citations"):
                        state.image_url = str(state.item["arguments"].get("url") or "").strip()
                        if state.image_url.startswith(("http://", "https://")):
                            state.citations.append(
                                {
                                    "id": len(state.citations) + 1,
                                    "title": "Inspected image",
                                    "url": state.image_url,
                                    "source": "web_fetch",
                                }
                            )
                if state.tool_name == "find" and state.item["arguments"].get("target") == "memory":
                    for state.memory in state.tool_payload.get("memories", []):  # noqa: B020
                        if not isinstance(state.memory, dict) or not state.memory.get("id"):
                            continue
                        state.memory_ref = {
                            "id": str(state.memory["id"]),
                            "key": str(state.memory.get("key") or "memory")[:120],
                            "source": str(state.memory.get("source") or "memory"),
                        }
                        if state.memory_ref not in state.used_memories:
                            state.used_memories.append(state.memory_ref)
                    if state.used_memories:
                        yield sse("memory_used", {"memories": state.used_memories})
                if (
                    not state.managed_task_run
                    and state.tool_name == "todo_write"
                    and str(state.item["arguments"].get("operation") or "") == "replace"
                ):
                    state.task_check = self.task_store.check_tasks(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                    )
                    if state.task_check.get("task_count"):
                        # The model has chosen a real persisted plan.
                        # From the next turn onward the lifecycle guard
                        # protects its order, evidence, and completion.
                        state.managed_task_run = True
                        state.task_lifecycle_stage = "read_plan"
                        state.active_task_id = None
                elif state.managed_task_run:
                    if (
                        state.task_lifecycle_stage == "read_plan"
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "read"
                    ):
                        state.raw_task_check = state.tool_payload.get("task_check")
                        if isinstance(state.raw_task_check, dict):
                            state.task_check = state.raw_task_check
                        else:
                            state.task_check = self.task_store.check_tasks(
                                tenant_id=state.auth.tenant_id,
                                user_id=state.auth.user_id,
                                conversation_id=state.conversation_id,
                            )
                        state.task_lifecycle_stage, state.active_task_id = (
                            self._task_lifecycle_stage(state.task_check)
                        )
                        if state.task_lifecycle_stage == "final" and not state.task_check.get(
                            "complete"
                        ):
                            state.terminal_status = "blocked"
                    elif (
                        state.task_lifecycle_stage == "start_task"
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "update"
                    ):
                        state.task_lifecycle_stage = "work"
                        state.task_has_work_evidence = False
                    elif state.task_lifecycle_stage == "work":
                        if (
                            state.tool_name == "todo_write"
                            and str(state.item["arguments"].get("operation") or "") == "update"
                        ):
                            state.task_lifecycle_stage = "verify_task"
                        elif state.tool_name not in {
                            "get_tool_result",
                            "ask_user",
                            "todo_write",
                            MCP_SEARCH_TOOLS,
                            MCP_GET_TOOL_SCHEMA,
                            MCP_GET_RESULT,
                        }:
                            state.task_has_work_evidence = True
                    elif (
                        state.task_lifecycle_stage in {"verify_task", "verify_final"}
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "check"
                    ):
                        state.task_check = state.tool_payload
                        state.task_lifecycle_stage, state.active_task_id = (
                            self._task_lifecycle_stage(state.task_check)
                        )
                        if state.task_check.get("complete"):
                            state.task_lifecycle_stage = "final"
                        elif state.task_lifecycle_stage == "final":
                            state.terminal_status = "blocked"
                state.output = json.dumps(
                    state.tool_payload, ensure_ascii=False, separators=(",", ":")
                )
                # A successful call breaks any consecutive-failure streak for
                # this tool so a later, unrelated failure does not inherit it.
                if isinstance(state.consecutive_tool_failures, dict):
                    state.consecutive_tool_failures[state.tool_name] = 0
            else:
                state.output = json.dumps(
                    {
                        "error": str(
                            state.result.get("error") or f"{state.tool_name} failed safely."
                        ),
                        "error_category": str(state.result.get("error_category") or "tool"),
                        "recoverable": bool(state.result.get("recoverable")),
                        **(
                            {"recovery": state.result["recovery"]}
                            if state.result.get("recovery")
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                state.tool_error_text = str(
                    state.result.get("error") or f"{state.tool_name} failed safely."
                ).strip()
                state.tool_execution_failure = (
                    f"{state.tool_name} failed: {state.tool_error_text[:1600]}"
                )
                state.tool_execution_failure_names.add(state.tool_name)
                if bool(state.result.get("recoverable")):
                    state.recoverable_tool_failure_count = (
                        int(state.recoverable_tool_failure_count or 0) + 1
                    )
                else:
                    state.tool_execution_failure_fatal = state.tool_execution_failure
                # Track consecutive failures of the same tool regardless of
                # argument variations. A model that keeps retrying a failing
                # read/glob with different paths is stuck, not making progress.
                if isinstance(state.consecutive_tool_failures, dict):
                    state.consecutive_tool_failures[state.tool_name] = (
                        int(state.consecutive_tool_failures.get(state.tool_name, 0) or 0) + 1
                    )
                    if (
                        state.consecutive_tool_failures[state.tool_name]
                        >= MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL
                    ):
                        self._request_model_final_answer_without_tools(
                            state,
                            reason=(
                                f"{state.tool_name} failed "
                                f"{state.consecutive_tool_failures[state.tool_name]} times in a row."
                            ),
                        )
            if state.run_id is not None:
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="tool_result",
                    status=(
                        "blocked"
                        if state.guard_refusal
                        else ("completed" if state.success else "failed")
                    ),
                    tool_name=state.tool_name,
                    tool_call_id=state.call_id,
                    input_json=state.item["arguments"],
                    result_json={
                        "success": state.success,
                        "guard_refusal": state.guard_refusal,
                        "error_category": (
                            state.result.get("error_category") if not state.success else None
                        ),
                        "recoverable": (
                            bool(state.result.get("recoverable")) if not state.success else False
                        ),
                        "output": state.output,
                        "turn_index": state.round_index,
                    },
                )
                # Persist the exact post-tool continuation point. A
                # reconnect or worker retry can resume from this
                # verified result without repeating the tool call.
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="running",
                    checkpoint={
                        "status": "running",
                        "phase": "tool_result",
                        "turn_index": state.round_index,
                        "tool_name": state.tool_name,
                        "tool_call_id": state.call_id,
                        "tool_success": state.success,
                        "next_phase": "model",
                        "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                    },
                )
            if state.success:
                yield sse(
                    "tool_result",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "tool_input": state.item["arguments"],
                        "output": state.output,
                        "success": True,
                        "guard_refusal": state.guard_refusal,
                        "turn_index": state.round_index,
                        "completed_at": self._now(),
                    },
                )
                if state.tool_name == "ask_user" and state.tool_payload.get("awaiting_user"):
                    # Do not re-signal the same pending clarification. A resumed
                    # or replayed turn can reach this executor for a call id that
                    # is already awaiting an answer; emitting again would stack
                    # duplicate "Clarifying Objective" rows in the timeline.
                    already_signaled = bool(
                        state.awaiting_user
                        and str(state.awaiting_user.get("call_id") or "") == str(state.call_id)
                    )
                    if not already_signaled:
                        state.awaiting_user = {
                            **state.tool_payload,
                            "question_id": str(uuid.uuid4()),
                            "call_id": state.call_id,
                            "step_id": state.step_id,
                            "tool_name": state.tool_name,
                            "tool_input": state.item["arguments"],
                            "turn_index": state.round_index,
                        }
                        yield sse(
                            "ask_user_question",
                            {
                                "tool_name": state.tool_name,
                                "tool_id": state.call_id,
                                "step_id": state.step_id,
                                "message": state.tool_payload.get("question"),
                                "options": state.tool_payload.get("options", []),
                                "question_id": state.awaiting_user["question_id"],
                                "turn_index": state.round_index,
                            },
                        )
                if state.tool_name == "plan_submit" and state.tool_payload.get("plan"):
                    # The plan is with the user now.  The turn pauses so the
                    # agent does not start the work before the review.
                    state.plan_review = {
                        "plan_id": str(state.tool_payload["plan"].get("id") or ""),
                        "title": str(state.tool_payload["plan"].get("title") or ""),
                        "content": str(state.tool_payload["plan"].get("content") or ""),
                        "status": str(state.tool_payload["plan"].get("status") or "proposed"),
                        "call_id": state.call_id,
                        "step_id": state.step_id,
                        "turn_index": state.round_index,
                    }
                    yield sse(
                        "plan_submitted",
                        {
                            "plan_id": state.plan_review["plan_id"],
                            "title": state.plan_review["title"],
                            "content": state.plan_review["content"],
                            "status": state.plan_review["status"],
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "turn_index": state.round_index,
                        },
                    )
            else:
                state.error_category = str(state.result.get("error_category") or "tool")
                if state.result.get("requires_approval"):
                    # A hook asked about the call instead of refusing it.  The
                    # call did not run, and the turn hands it to the approval
                    # flow so the user decides.
                    state.tool_execution_failure = None
                    state.tool_execution_failure_fatal = None
                    state.tool_execution_failure_names.discard(state.tool_name)
                    state.awaiting_approval = self._hook_approval_request(state)
                    yield sse("permission_request", state.awaiting_approval)
                    yield sse("approval_request", state.awaiting_approval)
                    break
                if state.mcp_bindings.get(state.tool_name) is not None:
                    state.mcp_error_text = str(state.result.get("error") or "").casefold()
                    state.policy_block_markers = (
                        "risk ceiling",
                        "read-only mode",
                        "blocked by policy",
                        "not in the connection allowlist",
                        "connection policy is not configured",
                    )
                    if any(marker in state.mcp_error_text for marker in state.policy_block_markers):
                        state.forced_answer = (
                            "The connected MCP action was blocked by its configured policy. "
                            "No external action was performed; update that connection's policy "
                            "only if this action is intended."
                        )
                        state.terminal_status = "blocked"
                    state.failure_key = hashlib.sha256(
                        json.dumps(
                            {
                                "tool": state.tool_name,
                                "category": state.error_category,
                                "error": str(state.result.get("error") or "")[:500],
                            },
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    state.repeated_mcp_failures[state.failure_key] = (
                        state.repeated_mcp_failures.get(state.failure_key, 0) + 1
                    )
                    if state.repeated_mcp_failures[state.failure_key] >= 2:
                        state.forced_answer = (
                            "The connected MCP service returned the same failure twice, so "
                            "DeepSpace stopped retrying to avoid a loop. Reconnect the service "
                            "or try again after it is healthy."
                        )
                        state.terminal_status = "blocked"
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.output,
                        "error_category": state.error_category,
                        "recoverable": bool(state.result.get("recoverable")),
                        **(
                            {"recovery": state.result["recovery"]}
                            if state.result.get("recovery")
                            else {}
                        ),
                    },
                )
            state.conversation_messages.append(
                {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
            )
            if state.control == "break" or state.disable_tools_for_final_answer:
                break
        if state.tool_execution_failure_fatal is not None:
            raise DeepSpaceToolExecutionError(state.tool_execution_failure_fatal)
        if (
            not state.disable_tools_for_final_answer
            and int(state.recoverable_tool_failure_count or 0)
            >= MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN
        ):
            self._request_model_final_answer_without_tools(
                state,
                reason="Several tool operations could not complete.",
            )
        self._record_session_event(
            auth=state.auth,
            conversation_id=state.conversation_id,
            run_id=state.run_id,
            event_type=STEP_END,
            turn=state.turn_number,
            step=state.round_index,
            payload={"phase": "model"},
        )

        # Inject repeat tool reminders into the conversation for the next round
        repeat_reminders = getattr(state, "repeat_tool_reminders", [])
        if repeat_reminders:
            for reminder in repeat_reminders:
                self._set_dynamic_system_instruction(
                    state.conversation_messages,
                    key=f"repeat_tool_reminder_{reminder.get('tool_name', 'unknown')}",
                    content=reminder.get("message", ""),
                )

        if (
            state.forced_answer is not None
            or state.awaiting_user is not None
            or state.awaiting_approval is not None
            or state.plan_review is not None
        ):
            state.control = "break"
            return
