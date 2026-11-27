"""Turn round phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast

from app.deepspace.models.context_epoch import DeepSpaceContextEpoch
from app.deepspace.services.chat_service.utils import (
    DEEPSPACE_AGENT_POLICY,
    DEEPSPACE_COMPACT_POLICY,
    MAX_DEADLINE_CONTINUATIONS,
    NATIVE_TOOL_REGISTRY,
    _mark_provider_output_event,
    _normalize_reasoning_effort,
    _reasoning_mode_for_turn,
    _TimedProviderItem,
    _TurnState,
    sse,
)
from app.deepspace.services.extensions import (
    ExtensionSet,
)
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL_NAMES,
)
from app.deepspace.services.plan_mode import (
    PLAN_MODE_INSTRUCTION,
)
from app.deepspace.services.reasoning_privacy import (
    redact_reasoning_text,
)
from app.deepspace.services.runtime_transitions import RuntimePhase, transition_checkpoint
from app.deepspace.services.session_events import (
    STEP_START,
)
from app.deepspace.services.xml_tool_calls import XmlToolCallStreamBuffer
from app.providers.services import ChatGenerateRequest
from app.providers.services.openai_codex_config import codex_supported_reasoning_efforts
from app.providers.services.reasoning_capabilities import (
    is_deepseek_v4_model,
    merge_reasoning_capabilities,
    normalize_reasoning_effort_value,
    reasoning_capabilities,
    supports_required_tool_choice,
    uses_deepseek_reasoning_protocol,
)
from app.providers.services.tool_definitions import unique_tool_definitions
from app.providers.services.usage_normalizer import normalize_chat_usage
from app.system.services.metrics_service import (
    DEEPSPACE_BUDGET_ALLOCATIONS_TOTAL,
    DEEPSPACE_CACHE_USAGE_TOTAL,
    DEEPSPACE_CANARY_MISMATCHES_TOTAL,
    DEEPSPACE_CONTEXT_COMPACTIONS_TOTAL,
    DEEPSPACE_CONTEXT_OVERFLOW_PREVENTED_TOTAL,
    _safe_label,
)

logger = logging.getLogger(__name__)


class _TurnRoundMixin:
    async def _stream_model_round(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 2 of a turn: one provider request and its streamed output."""
        if (
            state.resume_denied
            or state.forced_answer is not None
            or state.awaiting_user is not None
        ):
            state.control = "break"
            return
        state.round_index += 1
        self._record_session_event(
            auth=state.auth,
            conversation_id=state.conversation_id,
            run_id=state.run_id,
            event_type=STEP_START,
            turn=state.turn_number,
            step=state.round_index,
            payload={"phase": "model"},
        )
        # The model ends the loop by returning its final text. Model rounds do
        # not have a separate fixed ceiling; tool-loop, runtime, cancellation,
        # provider, and resource safeguards remain authoritative below.
        if state.run_id is not None:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint={
                    "turn_index": state.round_index,
                    "phase": "model",
                    "continuation_count": state.deadline_continuations,
                    **transition_checkpoint(
                        previous=None,
                        next_phase=RuntimePhase.MODEL_REQUESTED,
                        turn_index=state.round_index,
                    ),
                },
            )
        provider_telemetry = self._open_telemetry_span(
            state,
            self.session_telemetry.provider_span(
                state.conversation_id,
                state.candidate.provider_type,
                str(state.candidate.model_name),
                state.round_index,
            ),
        )
        if time.monotonic() >= state.loop_deadline:
            if state.deadline_continuations < MAX_DEADLINE_CONTINUATIONS:
                state.deadline_continuations += 1
                if state.run_id is not None:
                    self.runtime.update_checkpoint(
                        run_id=state.run_id,
                        status="running",
                        checkpoint={
                            "status": "running",
                            "phase": "deadline_continuation",
                            "turn_index": state.round_index,
                            "continuation_count": state.deadline_continuations,
                            "resume_available": True,
                            "next_action": "continue_from_checkpoint",
                        },
                        last_error="runtime_deadline_continuing",
                    )
                yield sse(
                    "run_checkpoint",
                    {
                        "phase": "deadline_continuation",
                        "continuation": state.deadline_continuations,
                        "max_continuations": MAX_DEADLINE_CONTINUATIONS,
                        "message": "DeepSpace saved its checkpoint and is continuing the same task.",
                    },
                )
                state.loop_deadline = time.monotonic() + state.max_runtime_seconds
                state.control = "continue"
                return
            state.terminal_status = "blocked"
            if state.run_id is not None:
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="blocked",
                    checkpoint={
                        "status": "blocked",
                        "phase": "deadline",
                        "turn_index": state.round_index,
                        "continuation_count": state.deadline_continuations,
                        "resume_available": True,
                        "next_action": "resume_from_checkpoint",
                    },
                    last_error="runtime_timeout",
                )
                self.runtime.finish(run_id=state.run_id, status="blocked", error="runtime_timeout")
            state.control = "break"
            return
        if await self._request_disconnected(state.request):
            state.terminal_status = "cancelled"
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id,
                    status="cancelled",
                    error="client_disconnected",
                )
            state.control = "break"
            return
        if state.run_id is not None and self.runtime.is_cancel_requested(run_id=state.run_id):
            state.terminal_status = "cancelled"
            self.runtime.finish(run_id=state.run_id, status="cancelled", error="user_cancelled")
            state.control = "break"
            return
        state.tool_calls = {}
        state.provider_continuation_parts = []
        # Providers can split a function call across many SSE chunks.
        # Track exactly what has already been sent to the UI so the
        # activity timeline can render real argument deltas without
        # inventing progress or displaying an unnamed duplicate tool.
        state.emitted_tool_argument_lengths = {}
        state.round_answer_start = len(state.answer_parts)
        state.round_thinking_start = len(state.thinking_parts)
        state.round_artifact_start = len(state.generated_artifacts)
        state.request_images = [*state.attachment_vision_images, *state.pending_images]
        state.pending_images.clear()
        state.lifecycle_instruction = None
        configured_max_steps = getattr(self.settings, "deepspace_agent_max_steps", None)
        if (
            not state.disable_tools_for_final_answer
            and configured_max_steps is not None
            and int(configured_max_steps) > 0
            and state.round_index >= int(configured_max_steps)
        ):
            self._request_model_final_answer_without_tools(
                state,
                reason=f"Configured step limit ({int(configured_max_steps)}) reached.",
                mark_blocked=False,
            )
        if state.disable_tools_for_final_answer:
            # Keep tool definitions attached (matching OpenCode) so the provider's
            # prompt cache stays warm, and enforce text-only output via tool_choice="none".
            state.tools_for_round = list(state.available_tools or [])
        elif state.plan_mode_active:
            # A planning turn reads and designs.  It does not change
            # anything, so the write tools are withheld and the plan tools
            # are offered instead.
            state.tools_for_round = self._tools_for_plan_mode(
                all_tools=state.available_tools,
            )
            state.lifecycle_instruction = PLAN_MODE_INSTRUCTION
        elif state.managed_task_run:
            state.tools_for_round = self._tools_for_task_lifecycle(
                stage=state.task_lifecycle_stage,
                all_tools=state.available_tools,
                has_work_evidence=state.task_has_work_evidence,
            )
            state.lifecycle_instruction = self._task_lifecycle_instruction(
                stage=state.task_lifecycle_stage,
                task_id=state.active_task_id,
            )
            if state.task_lifecycle_stage == "work" and "spawn_teammate" not in (
                state.native_tool_names or set()
            ):
                state.lifecycle_instruction += (
                    " If this task planned subagents or teammates that are unavailable, "
                    "degrade gracefully to in-process sequential execution and perform the work directly."
                )
        else:
            state.tools_for_round = state.available_tools
        # Provider cache prefixes must be byte-stable. Tool order is
        # semantic-free, so normalize it before every request.
        state.tools_for_round = sorted(
            unique_tool_definitions(state.tools_for_round),
            key=lambda tool: str((tool.get("function") or {}).get("name") or ""),
        )
        # Do not impose an AverQel output cap. Provider metadata attaches an
        # explicit model limit when the provider advertises one;
        # otherwise ``None`` lets the provider apply its own default.
        # The application setting is used only as a local context
        # reservation below, never sent as max_tokens to the model.
        state.request_max_tokens = getattr(state.candidate, "max_output_tokens", None)
        state.context_output_reservation = max(
            512, int(getattr(self.settings, "llm_max_tokens_per_request", 1024))
        )
        state.provider_read_timeout = max(
            15,
            min(
                300,
                int(
                    getattr(
                        self.settings,
                        "deepspace_provider_read_timeout_seconds",
                        300,
                    )
                ),
            ),
        )
        state.request_messages = list(state.conversation_messages)
        if state.lifecycle_instruction:
            state.request_messages.append(
                {"role": "system", "content": state.lifecycle_instruction}
            )
        deepseek_tool_protocol_active = bool(state.tools_for_round) or any(
            message.get("role") == "tool" or message.get("tool_calls")
            for message in state.request_messages
        )
        state.request_messages = await self._restore_provider_continuations(
            state.request_messages,
            provider_type=state.candidate.provider_type,
            model_name=state.candidate.model_name,
            continuation_required=(
                deepseek_tool_protocol_active
                and uses_deepseek_reasoning_protocol(
                    state.candidate.provider_type, state.candidate.model_name
                )
                and (
                    state.thinking_enabled
                    or bool(state.reasoning_effort)
                    or is_deepseek_v4_model(state.candidate.model_name)
                )
            ),
            auth=state.auth,
            conversation_id=state.conversation_id,
            assistant_message=state.assistant_message,
        )
        # Calculate before fitting so optional result and history
        # pressure is visible even when the request later compacts.
        # The existing context fitter remains the final hard safety
        # boundary and keeps the user message/policy intact.
        state.round_budget = self.context_budget.allocate(
            context_window=state.candidate.context_window,
            system_tokens=self._estimate_context_tokens(
                [message for message in state.request_messages if message.get("role") == "system"]
            ),
            tool_schema_tokens=self._estimate_context_tokens([], state.tools_for_round),
            user_tokens=self._estimate_context_tokens([{"role": "user", "content": state.prompt}]),
            prior_tool_result_tokens=self._estimate_context_tokens(
                [message for message in state.request_messages if message.get("role") == "tool"]
            ),
            reserved_output_tokens=(state.request_max_tokens or state.context_output_reservation),
            workload=self._workload_for_prompt(
                prompt=state.effective_routing_prompt,
                research_requested=state.research_requested,
            ),
        )
        state.context_tier = (
            "unknown"
            if not state.candidate.context_window
            else "small" if state.candidate.context_window <= 16_384 else "large"
        )
        DEEPSPACE_BUDGET_ALLOCATIONS_TOTAL.labels(
            workload=state.round_budget.workload,
            context_tier=state.context_tier,
        ).inc()
        state.selected_tool_names = self._tool_names(state.tools_for_round)
        # An extension's alias is offered under its own name, so it is an
        # authorized capability even though it is not in the native registry.
        state.canary_mismatch = any(
            name not in MCP_BROKER_TOOL_NAMES
            and not NATIVE_TOOL_REGISTRY.contains(name)
            and name not in (state.extensions or ExtensionSet()).aliases
            for name in state.selected_tool_names
        )
        if state.canary_mismatch:
            DEEPSPACE_CANARY_MISMATCHES_TOTAL.inc()
        state.last_context_epoch = self.context_epochs.reconcile(
            tenant_id=state.auth.tenant_id,
            user_id=state.auth.user_id,
            conversation_id=state.conversation_id,
            provider=str(state.candidate.provider_type),
            model=str(state.candidate.model_name),
            baseline={
                "policy": (
                    DEEPSPACE_COMPACT_POLICY
                    if state.tool_profile == "direct"
                    else DEEPSPACE_AGENT_POLICY
                ),
                "turn_boundary": self._current_turn_memory_boundary(),
            },
            sources={
                "mcp_services": sorted(
                    {
                        str(binding.server.name)
                        for binding in state.mcp_bindings.values()
                        if getattr(binding, "server", None) is not None
                    }
                ),
                "tool_names": sorted(
                    str((tool.get("function") or {}).get("name") or "")
                    for tool in state.tools_for_round
                ),
                "lifecycle": state.lifecycle_instruction or "",
            },
        )
        if (
            state.last_context_epoch.get("changed")
            or state.last_context_epoch.get("updated_reason") == "compaction"
        ):
            state.add = getattr(self.db, "add", None)
            state.flush = getattr(self.db, "flush", None)
            if callable(state.add) and callable(state.flush):
                state.add(
                    DeepSpaceContextEpoch(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        provider_type=str(state.candidate.provider_type),
                        model_name=str(state.candidate.model_name),
                        epoch=int(state.last_context_epoch.get("epoch") or 1),
                        reason=str(
                            state.last_context_epoch.get("updated_reason") or "source_update"
                        ),
                        baseline_digest=str(state.last_context_epoch.get("baseline_digest") or ""),
                        source_snapshot=dict(state.last_context_epoch.get("source_digests") or {}),
                    )
                )
                state.flush()
                # The context epoch's tenant foreign key takes a row
                # lock. Release it before yielding progress or doing
                # any work that can wait on the provider.
                self.db.commit()
        state.request_messages, state.request_compacted = self._fit_history_to_context(
            state.request_messages,
            context_window=state.candidate.context_window,
            max_output_tokens=(state.request_max_tokens or state.context_output_reservation),
            tools=state.tools_for_round,
            history_budget_tokens=state.round_budget.history_budget_tokens,
        )
        state.request_messages = self._strip_provider_internal_fields(state.request_messages)
        state.context_used_tokens = self._estimate_context_tokens(
            state.request_messages,
            state.tools_for_round,
        )
        state.last_token_categories = self._estimate_request_token_categories(
            state.request_messages, state.tools_for_round
        )
        if state.request_compacted:
            state.last_context_epoch = self.context_epochs.advance_for_compaction(
                state.last_context_epoch
            )
            DEEPSPACE_CONTEXT_COMPACTIONS_TOTAL.labels(reason="context_fit").inc()
            DEEPSPACE_CONTEXT_OVERFLOW_PREVENTED_TOTAL.labels(action="compaction").inc()
        state.context_remaining_tokens = (
            max(0, int(state.candidate.context_window) - state.context_used_tokens)
            if state.candidate.context_window
            else None
        )
        state.context_usage = (
            min(1.0, state.context_used_tokens / state.candidate.context_window)
            if state.candidate.context_window
            else None
        )
        state.last_context_used_tokens = state.context_used_tokens
        state.last_context_remaining_tokens = state.context_remaining_tokens
        state.last_context_usage = state.context_usage
        state.last_context_compacted = state.request_compacted
        state.reserved_output_tokens = (
            min(
                state.request_max_tokens or state.context_output_reservation,
                max(0, int(state.candidate.context_window) - state.context_used_tokens),
            )
            if state.candidate.context_window
            else (state.request_max_tokens or 0)
        )
        state.last_reserved_output_tokens = state.reserved_output_tokens
        state.request_input_tokens += state.context_used_tokens
        state.session_input_tokens += state.context_used_tokens
        state.budget_state = self._context_budget_state(
            used_tokens=state.context_used_tokens,
            context_limit=state.candidate.context_window,
            reserved_output_tokens=state.reserved_output_tokens,
            compacted=state.request_compacted,
        )
        precomputer = getattr(self, "compaction_precomputer", None)
        if precomputer is not None and not state.request_compacted:
            existing_summary_text = (
                state.durable_summary.summary_text
                if getattr(state, "durable_summary", None) is not None
                and str(getattr(state.durable_summary, "summary_text", "")).strip()
                else None
            )
            precomputer.maybe_schedule(
                conversation_id=state.conversation_id,
                history=list(state.request_messages),
                used_tokens=state.context_used_tokens,
                context_limit=state.candidate.context_window,
                settings=self.settings,
                provider=getattr(state, "provider", None),
                provider_registry=self.registry,
                existing_summary=existing_summary_text,
                model_name=state.candidate.model_name,
            )
        state.context_transport = self._provider_context_transport(
            candidate=state.candidate,
            auth=state.auth,
            conversation_id=state.conversation_id,  # gitleaks:allow
        )
        state.last_prompt_cache_mode = str(state.context_transport["mode"])
        state.last_prompt_cache_eligible = bool(state.context_transport["cache_eligible"])
        yield sse(
            "metrics",
            {
                "contextUsedTokens": state.context_used_tokens,
                "contextRemainingTokens": state.context_remaining_tokens,
                "contextUsage": state.context_usage,
                "contextUsageSource": "estimated_local",
                "sessionInputTokens": state.session_input_tokens,
                "sessionOutputTokens": state.session_output_tokens,
                "sessionTotalTokens": state.session_input_tokens + state.session_output_tokens,
                "requestInputTokens": state.request_input_tokens,
                "requestOutputTokens": state.request_output_tokens,
                "requestInputTokensSource": "serialized_request_estimate",
                "requestOutputTokensSource": "serialized_response_estimate",
                "userVisibleInputTokens": state.user_visible_input_tokens,
                "userVisibleOutputTokens": 0,
                "conversationVisibleTokens": state.visible_conversation_tokens,
                "maxOutputTokens": state.request_max_tokens,
                "toolProfile": state.tool_profile,
                "toolSchemaCount": len(state.tools_for_round),
                "mcpExposureMode": "broker" if state.mcp_bindings else "none",
                "mcpBrokerToolCount": len(state.mcp_tools),
                "mcpCallsUsed": state.mcp_call_count,
                "mcpCallsRemaining": max(0, self.mcp_max_calls_per_turn - state.mcp_call_count),
                "mcpDiscoveryCallsUsed": state.mcp_discovery_call_count,
                "mcpResultCharsUsed": state.mcp_result_chars,
                "mcpResultCharsRemaining": max(
                    0, self.mcp_max_result_chars_per_turn - state.mcp_result_chars
                ),
                "mcpResultCharsLimit": self.mcp_max_result_chars_per_turn,
                "adaptiveMcpPreviewChars": state.round_budget.mcp_preview_chars,
                "adaptiveHistoryBudgetTokens": state.round_budget.history_budget_tokens,
                "adaptiveToolResultBudgetTokens": state.round_budget.tool_result_budget_tokens,
                "adaptiveBudgetWorkload": state.round_budget.workload,
                "nativeToolResultCharsUsed": state.native_result_chars,
                "nativeToolResultCharsLimit": (
                    state.round_budget.tool_result_budget_tokens * 4
                    if state.round_budget.tool_result_budget_tokens is not None
                    else None
                ),
                "toolProfileCanary": {
                    "tool_count": len(state.tools_for_round),
                    "native_schema_digests": sorted(
                        NATIVE_TOOL_REGISTRY.spec(name).schema_digest
                        for name in self._tool_names(state.tools_for_round)
                        if NATIVE_TOOL_REGISTRY.contains(name)
                    ),
                },
                "contextEpoch": state.last_context_epoch.get("epoch"),
                "contextEpochReason": state.last_context_epoch.get("updated_reason"),
                "contextSourceUpdates": state.last_context_epoch.get("source_updates", []),
                **state.last_token_categories,
                "cachedInputTokens": None,
                "uncachedInputTokens": state.context_used_tokens,
                "tokenCategorySource": "serialized_request_estimate",
                "providerUsageScope": "unavailable",
                "providerUsageRoundCount": 0,
                "providerTurnUsage": {"usage_source": "unavailable"},
                "promptCacheMode": state.context_transport["mode"],
                "promptCacheEligible": state.context_transport["cache_eligible"],
                "promptCacheStatus": state.context_transport.get("cache_status", "unknown"),
                "promptCachePrefixDigest": state.context_transport.get("cache_prefix_digest"),
                **state.budget_state,
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
        state.candidate_metadata = cast(
            dict[str, Any], getattr(state.candidate, "metadata", {}) or {}
        )
        state.cached_reasoning_capabilities = {}
        state.candidate_provider_config_id = getattr(state.candidate, "provider_config_id", None)
        if state.candidate_provider_config_id is not None:
            state.cached_model = self.providers.model_cache.get_model(
                tenant_id=state.auth.tenant_id,
                provider_config_id=state.candidate_provider_config_id,
                model_name=state.candidate.model_name,
                model_kind="chat",
            )
            state.cached_capabilities = getattr(state.cached_model, "capabilities_json", None)
            if isinstance(state.cached_capabilities, dict):
                state.cached_reasoning_capabilities = state.cached_capabilities
        state.profile_capabilities = reasoning_capabilities(
            state.candidate.provider_type,
            state.candidate.model_name,
            base_url=state.candidate.base_url,
        )
        state.resolved_reasoning_capabilities = merge_reasoning_capabilities(
            state.candidate.provider_type,
            state.candidate.model_name,
            base_url=state.candidate.base_url,
            advertised={**state.candidate_metadata, **state.cached_reasoning_capabilities},
        )
        state.codex_efforts = (
            codex_supported_reasoning_efforts(
                state.candidate_metadata.get("codex_desktop"), state.candidate.model_name
            )
            if state.candidate.provider_type == "openai"
            and state.candidate.auth_mode == "codex_desktop"
            else None
        )
        state.codex_efforts_known = state.codex_efforts is not None
        state.cached_efforts_known = (
            state.resolved_reasoning_capabilities.get("reasoning_capabilities_known") is True
        )
        state.raw_configured_efforts = (
            state.codex_efforts
            if state.codex_efforts_known
            else (
                state.resolved_reasoning_capabilities.get("supported_reasoning_efforts", [])
                if state.cached_efforts_known
                else state.candidate_metadata.get("supported_reasoning_efforts", [])
            )
        )
        if (
            not state.codex_efforts_known
            and not state.cached_efforts_known
            and (
                not isinstance(state.raw_configured_efforts, list | tuple)
                or not state.raw_configured_efforts
            )
        ):
            state.compatible_metadata = state.candidate_metadata.get("openai_compatible")
            state.reasoning_metadata = (
                state.compatible_metadata.get("reasoning")
                if isinstance(state.compatible_metadata, dict)
                else None
            )
            state.raw_configured_efforts = (
                state.reasoning_metadata.get("supported_efforts", [])
                if isinstance(state.reasoning_metadata, dict)
                else []
            )
        state.configured_efforts = (
            state.raw_configured_efforts
            if isinstance(state.raw_configured_efforts, list | tuple)
            else ()
        )
        state.supported_efforts = tuple(
            value
            for item in state.configured_efforts
            if (value := normalize_reasoning_effort_value(item)) is not None
        )
        state.reasoning_capabilities_known = (
            state.codex_efforts_known
            or state.cached_efforts_known
            or state.candidate.provider_type == "custom"
            or bool(state.candidate_metadata.get("supported_reasoning_efforts"))
            or state.profile_capabilities.get("reasoning_capabilities_known") is True
        )
        if (
            not state.supported_efforts
            and not state.codex_efforts_known
            and not state.cached_efforts_known
        ):
            state.fallback_efforts = state.profile_capabilities.get(
                "supported_reasoning_efforts", []
            )
            state.supported_efforts = tuple(
                value
                for item in (
                    state.fallback_efforts
                    if isinstance(state.fallback_efforts, list | tuple)
                    else ()
                )
                if (value := normalize_reasoning_effort_value(item)) is not None
            )
        state.selected_effort = _normalize_reasoning_effort(
            state.reasoning_effort,
            state.supported_efforts,
            capabilities_known=state.reasoning_capabilities_known,
        )
        state.request_payload = ChatGenerateRequest(
            model=state.candidate.model_name,
            messages=state.request_messages,
            temperature=self.settings.llm_temperature,
            max_tokens=state.request_max_tokens,
            base_url=state.candidate.base_url or "",
            api_key=state.candidate.api_key,
            stream=True,
            reasoning_enabled=state.thinking_enabled or bool(state.selected_effort),
            reasoning_effort=state.selected_effort,
            images=state.request_images or None,
            tools=state.tools_for_round or None,
            tool_choice=(
                "none"
                if state.disable_tools_for_final_answer and state.tools_for_round
                else (
                    "required"
                    if state.tools_for_round
                    and (
                        state.managed_task_run
                        or (state.round_index == 1 and state.connected_service_tool_required)
                        or (
                            state.round_index == 1
                            and state.tool_call_required
                            and not state.tool_requirement_satisfied
                        )
                    )
                    and supports_required_tool_choice(
                        state.candidate.provider_type, state.candidate.model_name
                    )
                    else ("auto" if state.tools_for_round else None)
                )
            ),
            prompt_cache_mode=(
                state.context_transport["mode"]
                if state.context_transport["mode"] in {"anthropic_auto", "google_implicit"}
                else None
            ),
            prompt_cache_key=str(state.context_transport.get("cache_key") or "") or None,
            prompt_cache_retention=(
                state.context_transport["retention"]
                if state.context_transport.get("retention") in {"in_memory", "5m", "1h", "24h"}
                else None
            ),
            metadata={
                "surface": "deepspace",
                "conversation_id": str(state.conversation_id),
                "provider_type": state.candidate.provider_type,
                **(
                    {
                        "tenant_id": str(state.auth.tenant_id),
                        "user_id": str(state.auth.user_id),
                        "provider_config_id": str(state.candidate.provider_config_id),
                    }
                    if (
                        getattr(state.candidate, "auth_mode", None) == "codex_desktop"
                        or getattr(state.candidate, "metadata", {}).get("runtime_target")
                        == "desktop"
                    )
                    and getattr(state.candidate, "provider_config_id", None)
                    else {}
                ),
                "reasoning_mode": _reasoning_mode_for_turn(
                    thinking_enabled=state.thinking_enabled,
                    requested_effort=state.reasoning_effort,
                    capabilities_known=state.reasoning_capabilities_known,
                ),
                "model_reasoning_capabilities": {
                    key: state.resolved_reasoning_capabilities[key]
                    for key in (
                        "supports_reasoning",
                        "reasoning_capabilities_known",
                        "supported_reasoning_efforts",
                        "supports_thinking_toggle",
                        "request_controls_on",
                        "request_controls_off",
                        "reasoning_effort_values",
                    )
                    if key in state.resolved_reasoning_capabilities
                },
                "timeout_seconds": min(15, int(getattr(self.settings, "llm_timeout_seconds", 15))),
                "read_timeout_seconds": state.provider_read_timeout,
                "run_id": str(state.run_id) if state.run_id else None,
                "turn_index": state.round_index,
                "prompt_cache_eligible": bool(state.context_transport["cache_eligible"]),
                "prompt_cache_status": state.context_transport.get("cache_status", "unknown"),
                "prompt_cache_prefix_digest": state.context_transport.get("cache_prefix_digest"),
                "prompt_baseline_version": state.context_transport.get("baseline_version"),
                "prompt_baseline_digest": state.context_transport.get("baseline_digest"),
            },
        )
        state.round_tool_names = sorted(
            {
                str((tool.get("function") or {}).get("name") or "")
                for tool in (state.tools_for_round or [])
                if isinstance(tool, dict)
                and isinstance(tool.get("function"), dict)
                and (tool.get("function") or {}).get("name")
            }
        )
        state.tool_schema_names = sorted(
            set(state.tool_schema_names or []).union(state.round_tool_names)
        )[:300]
        state.tool_choice_history.append(
            {
                "round": state.round_index,
                "choice": state.request_payload.tool_choice,
                "tools": state.round_tool_names[:300],
            }
        )
        state.tool_choice_history = state.tool_choice_history[-32:]
        state.stream_events = getattr(state.provider, "stream_generate_events", None)
        state.image_fallback_stream_factory = None
        state.image_fallback_no_text_message = None
        if state.attachment_vision_images and len(state.request_images) == len(
            state.attachment_vision_images
        ):
            state.sent_image_ids = {
                file_id
                for file_id, status in state.attachment_vision_statuses.items()
                if status == "Image included as visual input."
            }
            state.ocr_context, state.has_ocr_text = self._attachment_ocr_fallback_instruction(
                state.attachment_rows,
                state.attachment_vision_statuses,
                image_ids=state.sent_image_ids,
                only_not_visual=False,
            )
            if state.has_ocr_text:
                state.fallback_messages, state._ = self._fit_history_to_context(
                    [
                        *state.request_payload.messages,
                        {"role": "system", "content": state.ocr_context},
                    ],
                    context_window=state.candidate.context_window,
                    max_output_tokens=(
                        state.request_max_tokens or state.context_output_reservation
                    ),
                    tools=state.tools_for_round,
                    history_budget_tokens=state.round_budget.history_budget_tokens,
                )
                state.fallback_payload = replace(
                    state.request_payload,
                    messages=state.fallback_messages,
                    images=None,
                )
                if callable(state.stream_events):

                    def image_fallback_stream_factory(
                        payload: ChatGenerateRequest = state.fallback_payload,
                        stream: Any = state.stream_events,
                    ) -> Any:
                        return stream(payload)

                    state.image_fallback_stream_factory = image_fallback_stream_factory

                else:

                    def image_fallback_stream_factory(
                        payload: ChatGenerateRequest = state.fallback_payload,
                        stream: Any = state.provider.stream_generate,
                    ) -> Any:
                        return stream(payload)

                    state.image_fallback_stream_factory = image_fallback_stream_factory

            else:
                state.image_fallback_no_text_message = (
                    "The selected model rejected the image, and upload-time OCR found no "
                    "readable text. Try a clearer image or choose a model with image support."
                )
        state.cancelled_during_provider_stream = False
        state.last_runtime_heartbeat = time.monotonic()
        state.round_provider_usage = {}
        state.round_output_tracking = {
            "first_monotonic": None,
            "last_monotonic": None,
            "event_count": 0,
            "last_event": None,
            "buffered": False,
        }

        # Context preparation may flush a context-epoch row with a
        # tenant foreign key. Commit prepared state before opening or
        # awaiting the provider stream so its transaction cannot hold
        # tenant locks while a model is generating or waiting on I/O.
        self.db.commit()

        xml_tool_buffer = XmlToolCallStreamBuffer()

        if callable(state.stream_events):
            if state.first_provider_request_monotonic is None:
                state.first_provider_request_monotonic = time.monotonic()
            async for state.stream_item in self._provider_stream_with_retry(  # noqa: B020
                lambda payload=state.request_payload, stream=state.stream_events: stream(payload),
                run_id=state.run_id,
                deadline=state.loop_deadline,
                cancel_key=state.provider_cancel_key,
                provider_type=state.candidate.provider_type,
                image_fallback_stream_factory=state.image_fallback_stream_factory,
                image_fallback_no_text_message=state.image_fallback_no_text_message,
            ):
                state.received_monotonic = (
                    state.stream_item.received_monotonic
                    if isinstance(state.stream_item, _TimedProviderItem)
                    else time.monotonic()
                )
                state.provider_event = (
                    state.stream_item.value
                    if isinstance(state.stream_item, _TimedProviderItem)
                    else state.stream_item
                )
                if (
                    state.run_id is not None
                    and time.monotonic() - state.last_runtime_heartbeat >= 5.0
                ):
                    self.runtime.heartbeat(run_id=state.run_id)
                    state.last_runtime_heartbeat = time.monotonic()
                if state.run_id is not None and (
                    state.bg_subagent_started or state.subagent_timelines
                ):
                    now_bg = time.monotonic()
                    if now_bg - float(state.last_round_bg_poll) >= 0.25:
                        state.last_round_bg_poll = now_bg
                        for bg_activity in self._poll_background_subagent_events(state):
                            self._record_subagent_activity(state, bg_activity)
                            yield sse("subagent_activity", bg_activity)
                if not isinstance(state.provider_event, dict):
                    continue
                state.event_type = str(state.provider_event.get("type") or "")
                if state.event_type == "stream_timing":
                    if state.provider_event.get("mode") == "buffered":
                        state.round_output_tracking["buffered"] = True
                    continue
                if state.event_type == "provider_continuation":
                    state.continuation = state.provider_event.get("reasoning_content")
                    if isinstance(state.continuation, str) and state.continuation:
                        _mark_provider_output_event(
                            state.round_output_tracking,
                            state.provider_event,
                            received_monotonic=state.received_monotonic,
                        )
                        state.provider_continuation_parts.append(state.continuation)
                    continue
                if isinstance(state.provider_event.get("usage"), dict):
                    state.normalized_usage = normalize_chat_usage(state.provider_event["usage"])
                    # Some adapters emit a terminal usage event with
                    # an empty usage object.  Never let that event
                    # erase a real usage record received earlier in
                    # the same round.
                    if state.normalized_usage["usage_source"] == "provider":
                        state.round_provider_usage.update(
                            {
                                key: value
                                for key, value in state.normalized_usage.items()
                                if value is not None and key != "usage_source"
                            }
                        )
                        state.round_provider_usage["usage_source"] = "provider"
                        state.last_provider_usage = dict(state.round_provider_usage)
                if state.event_type == "runtime_cancelled":
                    state.cancelled_during_provider_stream = True
                    break
                if state.event_type == "agent_status":
                    yield sse(
                        "agent_status",
                        {
                            "status": str(state.provider_event.get("status") or "running"),
                            "provider_type": state.candidate.provider_type,
                        },
                    )
                    continue
                if state.event_type == "media":
                    state.raw_media = state.provider_event.get("media")
                    if not isinstance(state.raw_media, list):
                        continue
                    for state.media in state.raw_media:  # noqa: B020
                        if not isinstance(state.media, dict):
                            continue
                        state.content_type = state.media.get("content_type") or state.media.get(
                            "mime_type"
                        )
                        state.data_base64 = state.media.get("data_base64") or state.media.get(
                            "data"
                        )
                        if not isinstance(state.content_type, str) or not isinstance(
                            state.data_base64, str
                        ):
                            continue
                        yield sse(
                            "media_status",
                            {
                                "phase": "uploading",
                                "message": "Saving generated media securely.",
                            },
                        )
                        try:
                            state.artifact = self.media_artifacts.persist_base64(
                                tenant_id=state.auth.tenant_id,
                                user_id=state.auth.user_id,
                                conversation_id=state.conversation_id,
                                message_id=state.assistant_message.id,
                                content_type=state.content_type,
                                data_base64=state.data_base64,
                                provider_type=state.candidate.provider_type,
                                model_name=state.candidate.model_name,
                                title=(
                                    state.media.get("title")
                                    if isinstance(state.media.get("title"), str)
                                    else None
                                ),
                                metadata={
                                    "turn_index": state.round_index,
                                    "generation": {
                                        # The user-provided prompt is persisted only with
                                        # this tenant/user-owned artifact. It contains no
                                        # provider credentials or connection secrets.
                                        "prompt": state.prompt[:8_000],
                                        "provider_type": state.candidate.provider_type,
                                        "model_name": state.candidate.model_name,
                                    },
                                },
                            )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "DeepSpace provider media could not be persisted safely",
                                exc_info=True,
                                extra={"conversation_id": str(state.conversation_id)},
                            )
                            yield sse(
                                "media_status",
                                {
                                    "phase": "failed",
                                    "message": "Generated media could not be saved securely.",
                                },
                            )
                            continue
                        state.generated_artifacts.append(state.artifact)
                        yield sse(
                            "media_status",
                            {
                                "phase": "ready",
                                "message": "Generated media is ready.",
                                "artifact_id": state.artifact["id"],
                            },
                        )
                        yield sse(
                            "artifact",
                            {"artifact": state.artifact, "turn_index": state.round_index},
                        )
                    continue
                if state.event_type in {
                    "tool_calls_delta",
                    "tool_call_delta",
                    "tool_call",
                    "tool_calls",
                    "function_call",
                }:
                    state.raw_deltas = (
                        state.provider_event.get("tool_calls")
                        or state.provider_event.get("tool_call")
                        or state.provider_event.get("function_call")
                    )
                    if state.raw_deltas:
                        _mark_provider_output_event(
                            state.round_output_tracking,
                            state.provider_event,
                            received_monotonic=state.received_monotonic,
                        )
                    if isinstance(state.raw_deltas, dict):
                        state.raw_deltas = [state.raw_deltas]
                    self._tool_call_accumulator(state.tool_calls, state.raw_deltas)
                    if isinstance(state.raw_deltas, list):
                        for state.position, state.item in enumerate(state.raw_deltas):  # noqa: B020
                            if not isinstance(state.item, dict):
                                continue
                            try:
                                state.call_index = int(state.item.get("index", state.position))
                            except (TypeError, ValueError):
                                state.call_index = state.position
                            state.current_call = state.tool_calls.get(state.call_index)
                            if not isinstance(state.current_call, dict):
                                continue
                            state.tool_name = self._tool_name(state.current_call).strip()
                            state.function = state.current_call.get("function")
                            state.arguments = (
                                state.function.get("arguments")
                                if isinstance(state.function, dict)
                                else None
                            )
                            if not state.tool_name or not isinstance(state.arguments, str):
                                # Do not surface a transient anonymous tool. We wait until the
                                # provider has named it, then send the accumulated real arguments.
                                continue
                            state.emitted_length = state.emitted_tool_argument_lengths.get(
                                state.call_index, 0
                            )
                            if len(state.arguments) <= state.emitted_length:
                                continue
                            state.call_id = str(
                                state.current_call.get("id") or f"tool_{state.call_index}"
                            )
                            state.call_id_clean = re.sub(r"[^a-zA-Z0-9_-]", "", state.call_id)[
                                :16
                            ] or str(state.call_index)
                            yield sse(
                                "tool_delta",
                                {
                                    "tool_name": state.tool_name,
                                    "tool_id": state.call_id,
                                    "step_id": f"tool_stream_{state.round_index}_{state.call_index}_{state.call_id_clean}",
                                    "tool_input": {},
                                    "text": state.arguments[state.emitted_length :],
                                    "stream": "arguments",
                                    "turn_index": state.round_index,
                                },
                            )
                            state.emitted_tool_argument_lengths[state.call_index] = len(
                                state.arguments
                            )
                    continue
                state.text = state.provider_event.get("text")
                if state.event_type not in {
                    "thinking",
                    "reasoning",
                    "reasoning_delta",
                }:
                    state.provider_reasoning = (
                        state.provider_event.get("reasoning_content")
                        or state.provider_event.get("reasoning")
                        or state.provider_event.get("thinking")
                    )
                    if isinstance(state.provider_reasoning, str) and state.provider_reasoning:
                        _mark_provider_output_event(
                            state.round_output_tracking,
                            state.provider_event,
                            received_monotonic=state.received_monotonic,
                        )
                        state.visible_reasoning = redact_reasoning_text(state.provider_reasoning)
                        state.thinking_parts.append(state.visible_reasoning)
                        yield sse("thinking", {"text": state.visible_reasoning})
                if not isinstance(state.text, str) or not state.text:
                    continue
                _mark_provider_output_event(
                    state.round_output_tracking,
                    state.provider_event,
                    received_monotonic=state.received_monotonic,
                )
                if state.event_type in {"thinking", "reasoning", "reasoning_delta"}:
                    state.visible_reasoning = redact_reasoning_text(state.text)
                    state.thinking_parts.append(state.visible_reasoning)
                    yield sse("thinking", {"text": state.visible_reasoning})
                elif state.event_type in {"delta", "text", "content"}:
                    if state.first_token_monotonic is None:
                        state.first_token_monotonic = state.received_monotonic
                    safe_delta = xml_tool_buffer.feed(state.text)
                    if safe_delta:
                        state.answer_parts.append(safe_delta)
                        yield sse("delta", {"text": safe_delta})
        else:
            if state.first_provider_request_monotonic is None:
                state.first_provider_request_monotonic = time.monotonic()
            async for state.stream_item in self._provider_stream_with_retry(  # noqa: B020
                lambda payload=state.request_payload, stream=state.provider.stream_generate: stream(
                    payload
                ),
                run_id=state.run_id,
                deadline=state.loop_deadline,
                cancel_key=state.provider_cancel_key,
                provider_type=state.candidate.provider_type,
                image_fallback_stream_factory=state.image_fallback_stream_factory,
                image_fallback_no_text_message=state.image_fallback_no_text_message,
            ):
                state.received_monotonic = (
                    state.stream_item.received_monotonic
                    if isinstance(state.stream_item, _TimedProviderItem)
                    else time.monotonic()
                )
                state.chunk = (
                    state.stream_item.value
                    if isinstance(state.stream_item, _TimedProviderItem)
                    else state.stream_item
                )
                if (
                    state.run_id is not None
                    and time.monotonic() - state.last_runtime_heartbeat >= 5.0
                ):
                    self.runtime.heartbeat(run_id=state.run_id)
                    state.last_runtime_heartbeat = time.monotonic()
                if state.run_id is not None and (
                    state.bg_subagent_started or state.subagent_timelines
                ):
                    now_bg = time.monotonic()
                    if now_bg - float(state.last_round_bg_poll) >= 0.25:
                        state.last_round_bg_poll = now_bg
                        for bg_activity in self._poll_background_subagent_events(state):
                            self._record_subagent_activity(state, bg_activity)
                            yield sse("subagent_activity", bg_activity)
                if isinstance(state.chunk, dict) and state.chunk.get("type") == "runtime_cancelled":
                    state.cancelled_during_provider_stream = True
                    break
                if not state.chunk:
                    continue
                if state.round_output_tracking["first_monotonic"] is None:
                    state.round_output_tracking["first_monotonic"] = state.received_monotonic
                state.round_output_tracking["last_monotonic"] = state.received_monotonic
                state.round_output_tracking["event_count"] += 1
                if state.first_token_monotonic is None:
                    state.first_token_monotonic = state.received_monotonic
                safe_chunk = xml_tool_buffer.feed(state.chunk)
                if safe_chunk:
                    state.answer_parts.append(safe_chunk)
                    yield sse("delta", {"text": safe_chunk})

        trailing_xml_text, xml_extracted = xml_tool_buffer.finalize(
            start_index=(max(state.tool_calls.keys(), default=-1) + 1),
            call_id_prefix=f"xml_call_r{state.round_index}",
        )
        if xml_extracted.tool_calls:
            state.answer_parts[state.round_answer_start :] = (
                [xml_extracted.cleaned_text] if xml_extracted.cleaned_text else []
            )
            next_xml_index = max(state.tool_calls.keys(), default=-1) + 1
            for xml_offset, xml_call in enumerate(xml_extracted.tool_calls):
                state.tool_calls[next_xml_index + xml_offset] = xml_call
        elif trailing_xml_text:
            state.answer_parts.append(trailing_xml_text)
            yield sse("delta", {"text": trailing_xml_text})

        state.round_output_event_count = int(state.round_output_tracking["event_count"])
        if state.run_id is not None and (state.bg_subagent_started or state.subagent_timelines):
            for bg_activity in self._poll_background_subagent_events(state):
                self._record_subagent_activity(state, bg_activity)
                yield sse("subagent_activity", bg_activity)
        state.round_first_output_monotonic = state.round_output_tracking["first_monotonic"]
        if state.round_provider_usage.get("usage_source") == "provider":
            state.provider_usage_rounds_seen += 1
            state.provider_turn_usage_seen = True
            for state.usage_key in state.provider_turn_usage_totals:  # noqa: B020
                state.usage_value = state.round_provider_usage.get(state.usage_key)
                if isinstance(state.usage_value, int) and state.usage_value >= 0:
                    state.provider_turn_usage_totals[state.usage_key] += state.usage_value
                    state.provider_turn_usage_rounds[state.usage_key] += 1
            # Providers such as Gemini may repeat cumulative usage
            # metadata on several stream chunks. Count cache events
            # once per model request, after the final normalized
            # values for that request are known.
            state.provider_label = _safe_label(str(state.candidate.provider_type))
            if state.round_provider_usage.get("cached_input_tokens") is not None:
                DEEPSPACE_CACHE_USAGE_TOTAL.labels(
                    provider=state.provider_label, status="read"
                ).inc()
            if state.round_provider_usage.get("cache_write_input_tokens") is not None:
                DEEPSPACE_CACHE_USAGE_TOTAL.labels(
                    provider=state.provider_label, status="write"
                ).inc()
        if state.round_output_event_count:
            if state.provider_first_output_monotonic is None:
                state.provider_first_output_monotonic = state.round_first_output_monotonic
            state.round_output_tokens = state.round_provider_usage.get("output_tokens")
            if isinstance(state.round_output_tokens, int) and state.round_output_tokens >= 0:
                state.provider_output_tokens_total += state.round_output_tokens
                state.provider_output_usage_rounds += 1
            else:
                state.provider_output_usage_complete = False

            if (
                not state.round_output_tracking["buffered"]
                and state.round_output_event_count >= 2
                and state.round_first_output_monotonic is not None
                and state.round_output_tracking["last_monotonic"] is not None
            ):
                state.round_generation_ms = max(
                    0.0,
                    (
                        state.round_output_tracking["last_monotonic"]
                        - state.round_first_output_monotonic
                    )
                    * 1000,
                )
                if state.round_generation_ms >= 1.0:
                    state.provider_generation_duration_ms += state.round_generation_ms
                else:
                    state.provider_generation_timing_complete = False
            else:
                state.provider_generation_timing_complete = False
                state.provider_generation_buffered = state.provider_generation_buffered or bool(
                    state.round_output_tracking["buffered"]
                )

        if state.cancelled_during_provider_stream:
            state.terminal_status = "cancelled"
            if state.run_id is not None:
                self.runtime.finish(run_id=state.run_id, status="cancelled", error="user_cancelled")
            state.control = "break"
            return

        if not provider_telemetry.closed:
            usage = state.round_provider_usage or {}
            started = state.first_provider_request_monotonic
            self.session_telemetry.record_provider_result(
                provider_telemetry.span,
                success=state.terminal_status not in ("failed", "cancelled"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                latency_ms=(int((time.monotonic() - started) * 1000) if started else None),
                error=state.message if state.terminal_status == "failed" else None,
            )
            provider_telemetry.close()

        # Tool arguments are model output too.  Include their
        # serialized form in the provider-neutral fallback estimate
        # used by local providers that do not return token usage.
        state.round_tool_output = (
            json.dumps(
                state.tool_calls,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if state.tool_calls
            else ""
        )
        state.round_output_text = (
            "".join(state.answer_parts[state.round_answer_start :])
            + "".join(state.thinking_parts[state.round_thinking_start :])
            + state.round_tool_output
        )
        state.round_output_tokens = (
            self._estimate_context_tokens(
                [{"role": "assistant", "content": state.round_output_text}]
            )
            if state.round_output_text
            else 0
        )
        state.request_output_tokens += state.round_output_tokens
        state.session_output_tokens += state.round_output_tokens

        if state.run_id is not None:
            self.runtime.record_step(
                run_id=state.run_id,
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                step_type="model_turn",
                status="completed",
                input_json={
                    "turn_index": state.round_index,
                    "tool_count": len(state.tool_calls),
                },
                result_json={
                    "answer_chars": sum(len(item) for item in state.answer_parts),
                    "thinking_chars": sum(len(item) for item in state.thinking_parts),
                },
            )

        state.normalized_call_items = [
            (index, state.tool_calls[index]) for index in sorted(state.tool_calls)
        ]
