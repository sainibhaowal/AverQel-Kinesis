"""Turn settle phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from app.deepspace.memory.memory_service import MemoryService
from app.deepspace.services.chat_service.utils import (
    _PRIVATE_PROVIDER_CONTINUATIONS_KEY,
    _pkg_attr,
    _TurnState,
    sse,
)
from app.deepspace.services.reasoning_privacy import (
    REASONING_REDACTION_VERSION,
)
from app.deepspace.services.session_events import (
    ASSISTANT_MESSAGE,
    TURN_END,
)
from app.query.services.visual_blocks import normalize_visual_markdown

logger = logging.getLogger(__name__)


class _TurnSettleMixin:
    async def _settle_turn(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 4 of a turn: answer, citations, memory and background indexing."""
        if state.aborted:
            return
        try:
            state.final_task_check = self.task_store.check_tasks(
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
            )
        except Exception:  # noqa: BLE001
            state.rollback = getattr(self.db, "rollback", None)
            if callable(state.rollback):
                try:
                    state.rollback()
                except Exception:  # noqa: BLE001
                    logger.warning("DeepSpace final task-check rollback failed", exc_info=True)
            state.terminal_status = "failed"
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="DEEPSPACE_TASK_STATE_FAILED",
                message="DeepSpace could not read the task plan safely. Please retry.",
                candidate=state.candidate,
            )
            logger.exception("DeepSpace final task check failed")
            yield sse(
                "error",
                {
                    "code": "DEEPSPACE_TASK_STATE_FAILED",
                    "message": "DeepSpace could not read the task plan safely. Please retry.",
                },
            )
            return
        if state.native_media_model and not state.generated_artifacts:
            yield sse(
                "media_status",
                {
                    "phase": "failed",
                    "message": "The selected media model did not return usable media.",
                },
            )
        state.raw_answer = (state.forced_answer or "".join(state.answer_parts)).strip()
        if state.forced_answer and not "".join(state.answer_parts):
            yield sse("delta", {"text": state.forced_answer})
        if not state.raw_answer and state.generated_artifacts and state.forced_answer is None:
            state.raw_answer = "Your generated media is ready."
        if state.awaiting_approval is not None:
            state.terminal_status = "awaiting_approval"
            pending_tool = str(state.awaiting_approval.get("tool_name") or "action")
            if state.awaiting_approval.get("mcp_server"):
                pending_tool = (
                    f"{state.awaiting_approval.get('mcp_server')} "
                    f"{state.awaiting_approval.get('mcp_tool') or 'connected action'}"
                )
            state.raw_answer = (
                f"DeepSpace is waiting for your approval before running {pending_tool}."
            )
        elif state.awaiting_user is not None:
            state.terminal_status = "awaiting_user"
            questions = state.awaiting_user.get("questions")
            first_q = (
                str(questions[0].get("question") or "").strip()
                if isinstance(questions, list) and questions and isinstance(questions[0], dict)
                else ""
            )
            state.raw_answer = str(
                state.awaiting_user.get("question")
                or first_q
                or "Please provide the requested information."
            ).strip()
        elif state.plan_review is not None:
            # The plan is with the user.  The work does not start until they
            # approve it, so the turn ends here.
            state.terminal_status = "awaiting_plan_review"
            state.raw_answer = (
                "The plan is ready for your review. Approve it to start the work, "
                "or send feedback to have it revised."
            )
        elif (
            state.managed_task_run
            and state.terminal_status != "cancelled"
            and state.final_task_check["task_count"]
            and not state.final_task_check["complete"]
            and state.forced_answer is None
            and not state.defer_task_for_greeting
        ):
            state.terminal_status = (
                "blocked" if state.final_task_check.get("blocked_count", 0) > 0 else "in_progress"
            )
            pause_notice = (
                "DeepSpace paused because the task list is not complete. "
                "The remaining work is persisted and can continue from your next message."
            )
            if not (state.raw_answer and state.raw_answer.strip()):
                state.raw_answer = pause_notice
            else:
                # Self-healing & non-destructive preservation:
                # Do NOT erase the model's generated diagnostic analysis, blocker details, or questions!
                # Ensure the pause advisory is attached cleanly if not already present.
                if pause_notice not in state.raw_answer:
                    state.raw_answer = (
                        f"{state.raw_answer.rstrip()}\n\n"
                        f"> ⏸️ **DeepSpace Managed Task Status:** {pause_notice}"
                    )
        state.native_summary = self._native_research_summary(state.native_research_stats)
        if state.native_summary:
            state.raw_answer = (
                f"{state.raw_answer.rstrip()}\n\n{state.native_summary}"
                if state.raw_answer
                else state.native_summary
            )
        state.answer = self._append_citations(state.raw_answer, state.citations)
        self._record_session_event(
            auth=state.auth,
            conversation_id=state.conversation_id,
            run_id=state.run_id,
            event_type=ASSISTANT_MESSAGE,
            turn=state.turn_number,
            payload={"text": state.answer, "citations": len(state.citations)},
        )
        state.normalized_answer, state.visual_errors = normalize_visual_markdown(state.answer)
        if state.normalized_answer != state.answer:
            state.answer = state.normalized_answer
            if state.visual_errors:
                logger.warning(
                    "Demoted invalid DeepSpace visual Markdown fences: %s", state.visual_errors[:5]
                )
            # The provider stream may already have shown the raw fence. Replace
            # the completed answer so the visible chat and durable history use
            # the same validated representation.
            yield sse("replace", {"content": state.answer, "format": "markdown"})
        if state.answer != state.raw_answer and state.answer.startswith(state.raw_answer):
            yield sse("delta", {"text": state.answer[len(state.raw_answer) :]})
        state.provider_output_tokens = (
            state.provider_output_tokens_total
            if state.provider_output_usage_rounds > 0 and state.provider_output_usage_complete
            else None
        )
        state.provider_generation_ms = (
            round(state.provider_generation_duration_ms, 3)
            if (
                state.provider_output_usage_rounds > 0
                and state.provider_generation_timing_complete
                and state.provider_generation_duration_ms >= 1.0
            )
            else None
        )
        state.tokens_per_second = (
            round(state.provider_output_tokens / (state.provider_generation_ms / 1000), 1)
            if state.provider_output_tokens is not None and state.provider_generation_ms is not None
            else None
        )
        if state.tokens_per_second is not None:
            state.tokens_per_second_unavailable_reason = None
        elif state.provider_output_tokens is None:
            state.tokens_per_second_unavailable_reason = (
                "usage_missing_for_generation_round"
                if state.provider_output_usage_rounds > 0
                else "exact_usage_unavailable"
            )
        elif state.provider_generation_buffered:
            state.tokens_per_second_unavailable_reason = "buffered_provider_fallback"
        elif not state.provider_generation_timing_complete:
            state.tokens_per_second_unavailable_reason = "insufficient_stream_samples"
        else:
            state.tokens_per_second_unavailable_reason = "stream_interval_too_short"
        state.time_to_first_token_ms = (
            max(0, int((state.first_token_monotonic - state.request_started_monotonic) * 1000))
            if state.first_token_monotonic is not None
            else None
        )
        state.provider_time_to_first_output_ms = (
            max(
                0,
                int(
                    (state.provider_first_output_monotonic - state.first_provider_request_monotonic)
                    * 1000
                ),
            )
            if (
                state.provider_first_output_monotonic is not None
                and state.first_provider_request_monotonic is not None
            )
            else None
        )
        state.request_latency_ms = max(
            0, int((time.monotonic() - state.request_started_monotonic) * 1000)
        )
        state.provider_turn_usage = state.provider_turn_usage_snapshot()
        state.metadata = {
            "status": (
                "cancelled"
                if state.terminal_status == "cancelled"
                else (
                    "awaiting_approval"
                    if state.terminal_status == "awaiting_approval"
                    else (
                        "awaiting_user"
                        if state.terminal_status == "awaiting_user"
                        else ("blocked" if state.terminal_status == "blocked" else "ready")
                    )
                )
            ),
            "surface": "deepspace",
            "provider_type": state.candidate.provider_type,
            "model_name": state.candidate.model_name,
            "task_check": state.final_task_check,
            "context_used_tokens": state.last_context_used_tokens,
            "context_remaining_tokens": state.last_context_remaining_tokens,
            "context_usage": state.last_context_usage,
            "context_usage_source": "estimated_local",
            "session_input_tokens": state.session_input_tokens,
            "session_output_tokens": state.session_output_tokens,
            "session_total_tokens": state.session_input_tokens + state.session_output_tokens,
            "request_input_tokens": state.request_input_tokens,
            "request_output_tokens": state.request_output_tokens,
            "request_input_tokens_source": "serialized_request_estimate",
            "request_output_tokens_source": "serialized_response_estimate",
            "user_visible_input_tokens": state.user_visible_input_tokens,
            "user_visible_output_tokens": (
                self._estimate_visible_tokens([{"role": "assistant", "content": state.answer}])
                if state.answer
                else 0
            ),
            "conversation_visible_tokens": state.visible_conversation_tokens
            + (
                self._estimate_visible_tokens([{"role": "assistant", "content": state.answer}])
                if state.answer
                else 0
            ),
            "reserved_output_tokens": state.last_reserved_output_tokens,
            "context_compacted": state.last_context_compacted,
            "context_epoch": state.last_context_epoch.get("epoch"),
            "context_epoch_reason": state.last_context_epoch.get("updated_reason"),
            "context_source_updates": state.last_context_epoch.get("source_updates", []),
            **state.last_token_categories,
            "cached_input_tokens": None,
            "uncached_input_tokens": state.last_context_used_tokens,
            "token_category_source": "serialized_request_estimate",
            "provider_usage": state.last_provider_usage,
            "provider_usage_scope": (
                "latest_reported_round"
                if state.last_provider_usage.get("usage_source") == "provider"
                else "unavailable"
            ),
            "provider_usage_round_count": state.provider_usage_rounds_seen,
            "provider_turn_usage": state.provider_turn_usage,
            "provider_output_tokens": state.provider_output_tokens,
            "provider_generation_duration_ms": state.provider_generation_ms,
            "tokens_per_second": state.tokens_per_second,
            "tokens_per_second_unavailable_reason": state.tokens_per_second_unavailable_reason,
            "time_to_first_token_ms": state.time_to_first_token_ms,
            "provider_time_to_first_output_ms": state.provider_time_to_first_output_ms,
            "request_latency_ms": state.request_latency_ms,
            "tool_profile": state.tool_profile,
            "tool_call_required": bool(state.tool_call_required),
            "tool_calling_supported": bool(state.tool_calling_supported),
            "tool_requirement_satisfied": bool(state.tool_requirement_satisfied),
            "tool_call_recovery_retries": int(state.tool_call_recovery_retries or 0),
            "tool_schema_names": state.tool_schema_names or [],
            "tool_choice_history": state.tool_choice_history or [],
            "prompt_cache_mode": state.last_prompt_cache_mode,
            "prompt_cache_eligible": state.last_prompt_cache_eligible,
            "prompt_cache_status": state.context_transport.get("cache_status", "unknown"),
            "prompt_cache_prefix_digest": state.context_transport.get("cache_prefix_digest"),
        }
        state.assistant_metadata = getattr(state.assistant_message, "metadata_json", None)
        state.existing_metadata = (
            state.assistant_metadata if isinstance(state.assistant_metadata, dict) else {}
        )
        state.private_continuations = state.existing_metadata.get(
            _PRIVATE_PROVIDER_CONTINUATIONS_KEY
        )
        if isinstance(state.private_continuations, dict):
            state.metadata[_PRIVATE_PROVIDER_CONTINUATIONS_KEY] = state.private_continuations
        # Keep the durable event-log cursor attached to the assistant turn.
        # History uses it to rebuild the ordered thinking/tool timeline after
        # navigation or a full page reload. Dropping it here caused reloads to
        # fall back to one concatenated thinking block and merged tool rows.
        if state.client_request_id:
            state.metadata["client_request_id"] = state.client_request_id
        # Persist the selected model's context metadata with the completed
        # assistant message.  The live ``meta`` event already carries these
        # values, but history/reload can only restore what is stored here.
        # Keeping this provider-agnostic metadata prevents the context meter
        # from losing its denominator after streaming finishes.
        if state.candidate.context_window is not None:
            state.metadata["context_limit"] = state.candidate.context_window
            state.metadata["context_window"] = state.candidate.context_window
        if state.candidate.context_window_source:
            state.metadata["context_limit_source"] = state.candidate.context_window_source
        if state.used_memories:
            state.metadata["memory"] = {"used": state.used_memories[:8]}
        if state.thinking_parts:
            state.metadata["thinking"] = {"content": "".join(state.thinking_parts)}
            state.metadata["thinking_redaction_version"] = REASONING_REDACTION_VERSION
        if state.generated_artifacts:
            state.metadata["artifacts"] = state.generated_artifacts
        if state.awaiting_user is not None:
            # Keep the clarification identity in history so a reload can
            # rehydrate the answer control and resume the same durable run.
            state.metadata["pending_user_question"] = state.awaiting_user
        if state.awaiting_approval is not None:
            # Keep the approval identity in the assistant message as well as
            # the durable run checkpoint. History reloads then retain the
            # actionable approval card instead of only the generic sentence.
            state.metadata["pending_approval"] = state.awaiting_approval
        if state.plan_review is not None:
            # Keep the plan identity in the assistant message so a reload
            # can rehydrate the review card for the same plan.
            state.metadata["pending_plan_review"] = state.plan_review
        if state.run_id is not None:
            state.durable_steps = self.runtime.history_steps_for_message(
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                assistant_message_id=state.assistant_message.id,
            )
            if state.durable_steps:
                state.metadata["agent_steps"] = state.durable_steps
        if getattr(state, "subagent_timelines", None):
            state.metadata["subagent_timelines"] = list(state.subagent_timelines.values())
        state.produced_workspace_files = self._collect_turn_workspace_files(state)
        if state.produced_workspace_files:
            state.metadata["workspace_files"] = state.produced_workspace_files
        # Ensure the conversation has not been deleted or cancelled while generating
        state.active_conversation = self.chat.get_conversation(
            tenant_id=state.auth.tenant_id,
            conversation_id=state.conversation_id,  # gitleaks:allow
        )
        if state.active_conversation is None:
            logger.info(
                "Conversation %s was deleted during processing; aborting turn persistence.",
                state.conversation_id,
            )
            return

        if state.run_id is not None and self.runtime.is_cancel_requested(run_id=state.run_id):
            logger.info(
                "Run %s for conversation %s was cancelled; aborting turn persistence.",
                state.run_id,
                state.conversation_id,
            )
            return

        self.chat.complete_assistant_message(
            tenant_id=state.auth.tenant_id,
            conversation_id=state.conversation_id,
            message_id=state.assistant_message.id,
            user_id=state.auth.user_id,
            content=state.answer,
            metadata_json=state.metadata,
        )
        self.db.commit()
        self._invalidate_context_cache(auth=state.auth, conversation_id=state.conversation_id)
        try:
            self._persist_context_summary(auth=state.auth, conversation_id=state.conversation_id)
            self.db.commit()
        except Exception:  # noqa: BLE001
            # Compaction is an optimization. A completed response must never
            # fail if the optional summary persistence path is unavailable.
            self.db.rollback()
            logger.warning("DeepSpace context summary refresh failed", exc_info=True)
        if state.answer.strip() and state.terminal_status not in {"cancelled", "failed"}:
            # Search indexing is derived, asynchronous work. It must never add
            # latency to the live model stream or make a completed answer fail.
            try:
                from app.platform.worker.celery_app import celery_app

                state.celery_app = celery_app

                state.celery_app.send_task(
                    "deepspace.index_conversation",
                    kwargs={
                        "tenant_id": str(state.auth.tenant_id),
                        "user_id": str(state.auth.user_id),
                        "conversation_id": str(state.conversation_id),
                    },
                    queue="dataset_indexing",
                )
            except Exception:  # noqa: BLE001
                logger.info("DeepSpace conversation indexing was not queued", exc_info=True)
        if (
            state.terminal_status == "running"
            and state.answer.strip()
            and not state.memory_written_this_turn
            and not state.defer_task_for_greeting
        ):
            try:
                state.mem_svc = _pkg_attr("MemoryService", MemoryService)(self.db, self.settings)
                state.prefs = await state.mem_svc.get_preferences(
                    tenant_id=str(state.auth.tenant_id), user_id=str(state.auth.user_id)
                )
                state.candidates = state.mem_svc._candidates_from_prompt(state.prompt)
                state.has_explicit = any(
                    bool(item.get("explicit")) for item in (state.candidates or [])
                )
                if state.has_explicit or state.prefs.get("automatic_capture_enabled", False):
                    state.consolidation = await state.mem_svc.consolidate_turn(
                        tenant_id=str(state.auth.tenant_id),
                        user_id=str(state.auth.user_id),
                        conversation_id=str(state.conversation_id),
                        prompt=state.prompt,
                    )
                    if state.consolidation and state.consolidation.get("status") in {
                        "pending",
                        "saved",
                    }:
                        yield sse("memory_candidate", state.consolidation)
            except Exception:  # noqa: BLE001
                # Memory convenience work must never fail a completed chat response.
                logger.warning("DeepSpace memory consolidation failed", exc_info=True)
        if state.run_id is not None:
            if state.terminal_status == "awaiting_approval":
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="awaiting_approval",
                    checkpoint={
                        "status": "awaiting_approval",
                        "phase": "approval",
                        "pending_approval": state.awaiting_approval or {},
                        "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                    },
                )
            elif state.terminal_status == "awaiting_user":
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="awaiting_user",
                    checkpoint={
                        "status": "awaiting_user",
                        "phase": "question",
                        "pending_user_question": state.awaiting_user or {},
                        **(
                            {"client_request_id": state.client_request_id}
                            if state.client_request_id
                            else {}
                        ),
                    },
                )
            elif state.terminal_status == "awaiting_plan_review":
                # The plan is with the user.  The run stays open so the
                # review decision can resume the same conversation.
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="awaiting_plan_review",
                    checkpoint={
                        "status": "awaiting_plan_review",
                        "phase": "plan_review",
                        "pending_plan_review": state.plan_review or {},
                        **(
                            {"client_request_id": state.client_request_id}
                            if state.client_request_id
                            else {}
                        ),
                    },
                )
            else:
                self.runtime.finish(
                    run_id=state.run_id,
                    status=state.terminal_status if state.terminal_status != "running" else "ready",
                )
        state.metrics = {
            "modelName": state.candidate.model_name,
            "providerType": state.candidate.provider_type,
            "totalTokens": len(state.answer.split()),
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
            "userVisibleInputTokens": state.user_visible_input_tokens,
            "userVisibleOutputTokens": (
                self._estimate_visible_tokens([{"role": "assistant", "content": state.answer}])
                if state.answer
                else 0
            ),
            "conversationVisibleTokens": state.visible_conversation_tokens
            + (
                self._estimate_visible_tokens([{"role": "assistant", "content": state.answer}])
                if state.answer
                else 0
            ),
            "maxOutputTokens": getattr(state.candidate, "max_output_tokens", None),
            "requestLatencyMs": state.request_latency_ms,
            "providerOutputTokens": state.provider_output_tokens,
            "providerGenerationDurationMs": state.provider_generation_ms,
            "tokensPerSecond": state.tokens_per_second,
            "tokensPerSecondUnavailableReason": state.tokens_per_second_unavailable_reason,
            "promptCacheMode": state.last_prompt_cache_mode,
            "promptCacheEligible": state.last_prompt_cache_eligible,
            "promptCacheStatus": state.context_transport.get("cache_status", "unknown"),
            "promptCachePrefixDigest": state.context_transport.get("cache_prefix_digest"),
            "toolCallRequired": bool(state.tool_call_required),
            "toolCallingSupported": bool(state.tool_calling_supported),
            "toolRequirementSatisfied": bool(state.tool_requirement_satisfied),
            "toolCallRecoveryRetries": int(state.tool_call_recovery_retries or 0),
            "toolSchemaNames": state.tool_schema_names or [],
            "toolChoiceHistory": state.tool_choice_history or [],
            "contextEpoch": state.last_context_epoch.get("epoch"),
            "contextEpochReason": state.last_context_epoch.get("updated_reason"),
            "contextSourceUpdates": state.last_context_epoch.get("source_updates", []),
            **{
                "systemContextTokens": state.last_token_categories["system_context_tokens"],
                "toolSchemaTokens": state.last_token_categories["tool_schema_tokens"],
                "toolResultTokens": state.last_token_categories["tool_result_tokens"],
                "cachedInputTokens": None,
                "uncachedInputTokens": state.last_context_used_tokens,
                "tokenCategorySource": "serialized_request_estimate",
                "providerUsage": state.last_provider_usage,
                "providerUsageScope": (
                    "latest_reported_round"
                    if state.last_provider_usage.get("usage_source") == "provider"
                    else "unavailable"
                ),
                "providerUsageRoundCount": state.provider_usage_rounds_seen,
                "providerTurnUsage": state.provider_turn_usage,
            },
            "timeToFirstTokenMs": state.time_to_first_token_ms,
            "providerTimeToFirstOutputMs": state.provider_time_to_first_output_ms,
            **self._context_budget_state(
                used_tokens=state.last_context_used_tokens or 0,
                context_limit=state.candidate.context_window,
                reserved_output_tokens=state.last_reserved_output_tokens,
                compacted=state.last_context_compacted,
            ),
        }
        if state.candidate.context_window is not None:
            state.metrics["contextLimit"] = state.candidate.context_window
        if state.candidate.context_window_source:
            state.metrics["contextLimitSource"] = state.candidate.context_window_source
        self._record_request_metric(
            auth=state.auth,
            conversation_id=state.conversation_id,
            candidate=state.candidate,
            outcome=state.terminal_status if state.terminal_status != "running" else "ready",
            started_monotonic=state.request_started_monotonic,
            first_token_monotonic=state.first_token_monotonic,
            tool_profile=state.tool_profile,
        )
        if state.terminal_status == "running":
            self.provider_circuit.record_success(
                tenant_id=state.auth.tenant_id,
                provider_config_id=getattr(state.candidate, "provider_config_id", None),
                model_name=state.candidate.model_name,
            )
        self._record_turn_changes(state)
        self._record_session_event(
            auth=state.auth,
            conversation_id=state.conversation_id,
            run_id=state.run_id,
            event_type=TURN_END,
            turn=state.turn_number,
            payload={"status": "completed", "turn": state.turn_number},
        )
        # Check for goal continuations
        await self._maybe_continue_goals(state)
        # Update session title if needed
        await self._maybe_update_session_title(state)
        yield sse("lifecycle", {"phase": "finalizing", "message": "Saving the completed turn."})
        if getattr(state, "produced_workspace_files", None):
            yield sse("workspace_files", {"files": state.produced_workspace_files})
        yield sse("metrics", state.metrics)
        async for frame in self._report_invariant_results(state):
            yield frame
        yield sse(
            "done",
            {
                "conversation_id": str(state.conversation_id),
                "message_id": str(state.assistant_message.id),
                "run_id": str(state.run_id) if state.run_id else None,
                "status": state.terminal_status if state.terminal_status != "running" else "ready",
            },
        )
