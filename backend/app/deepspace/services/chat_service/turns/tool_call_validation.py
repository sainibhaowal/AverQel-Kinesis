"""Validation and policy pass for DeepSpace turn tool calls."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid

from app.deepspace.services.chat_service.utils import (
    MAX_NATIVE_DISCOVERY_ATTEMPTS_PER_TURN,
    MAX_POLICY_DENIALS_PER_TURN,
    MAX_WAIT_AGENT_ATTEMPTS_PER_TURN,
    MAX_WAIT_AGENT_TOTAL_SECONDS_PER_TURN,
    NATIVE_TOOL_REGISTRY,
    _pkg_attr,
    _TurnState,
    sse,
)
from app.deepspace.services.extensions import (
    resolve_alias,
)
from app.deepspace.services.host_access import (
    tool_confirmation_warning,
)
from app.deepspace.services.loop_guard import (
    REPEATABLE_COORDINATION_READ_TOOLS,
)
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL,
    MCP_CALL_TOOL,
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
)
from app.deepspace.services.tool_registry import (
    NativeToolValidationError,
)
from app.integrations.services.mcp_runtime import MCPToolPolicyDecision

logger = logging.getLogger(__name__)


class _ToolCallValidationMixin:
    async def _validate_tool_calls(self, state: _TurnState):
        """Validate and apply policy to every normalized provider tool call."""
        state.valid_calls = []
        state.tool_execution_failure = None
        state.tool_execution_failure_fatal = None
        state.tool_execution_failure_names = set()
        state.recoverable_tool_failure_count = 0
        if not isinstance(state.consecutive_tool_failures, dict):
            state.consecutive_tool_failures = {}
        if state.policy_denial_count is None:
            state.policy_denial_count = 0
        state.invalid_tool_arguments = False
        state.invalid_tool_call_ids = set()
        state.round_invalid_tool_error_messages = []
        state.round_invalid_tool_names = []
        state.permitted_tool_names = self._tool_names(state.tools_for_round)
        state.first_call_index = state.normalized_call_items[0][0]
        state.starts_managed_plan = False
        if not state.managed_task_run:
            for state._, state.candidate_call in state.normalized_call_items:  # noqa: B020
                state.candidate_arguments = self._parse_tool_arguments(state.candidate_call)
                if (
                    self._tool_name(state.candidate_call) == "todo_write"
                    and isinstance(state.candidate_arguments, dict)
                    and state.candidate_arguments.get("operation") == "replace"
                ):
                    state.starts_managed_plan = True
                    break
        state.parallel_managed_work_batch = self._is_parallel_managed_work_batch(
            [call for _, call in state.normalized_call_items],
            stage=state.task_lifecycle_stage,
            mcp_bindings=state.mcp_bindings,
            max_calls=max(
                1,
                min(
                    int(
                        getattr(
                            self.settings,
                            "deepspace_max_concurrent_tools",
                            8,
                        )
                    ),
                    32,
                ),
            ),
        )
        for state.call_index, state.call in state.normalized_call_items:  # noqa: B020
            state.call_id = str(state.call.get("id") or uuid.uuid4())
            state.call_id_clean = re.sub(r"[^a-zA-Z0-9_-]", "", state.call_id)[:16] or str(
                state.call_index
            )
            # Keep every lifecycle event for this provider function call
            # on one stable timeline entry, even if its provider call id
            # arrives after an earlier streamed argument fragment.
            state.step_id = (
                f"tool_stream_{state.round_index}_{state.call_index}_{state.call_id_clean}"
            )
            state.tool_name = self._tool_name(state.call)
            state.model_tool_name = state.tool_name
            state.arguments = self._parse_tool_arguments(state.call)
            # An extension may publish an alias for a tool the model already
            # has.  The alias is resolved to its target before validation,
            # so the target's own contract still decides the arguments.
            state.alias = (state.extensions.aliases or {}).get(state.tool_name)
            if state.alias is not None:
                state.tool_name = state.alias.target
                if isinstance(state.arguments, dict):
                    state.arguments = resolve_alias(state.alias, state.arguments)
            if state.tool_name == MCP_BROKER_TOOL and isinstance(state.arguments, dict):
                state.operation = str(state.arguments.get("operation") or "").strip().casefold()
                state.broker_operation = {
                    "search": MCP_SEARCH_TOOLS,
                    "schema": MCP_GET_TOOL_SCHEMA,
                    "call": MCP_CALL_TOOL,
                    "result": MCP_GET_RESULT,
                }.get(state.operation)
                if state.broker_operation:
                    state.tool_name = state.broker_operation
                    state.arguments = {
                        key: value for key, value in state.arguments.items() if key != "operation"
                    }
                else:
                    # Keep malformed broker operations on the normal
                    # safe tool-error path; never fall through to an
                    # unscoped execution attempt.
                    state.arguments = None
            if state.arguments is None:
                state.invalid_tool_arguments = True
                state.invalid_tool_call_ids.add(state.call_id)
                state.output = f"The {state.tool_name} arguments were invalid JSON."
                state.round_invalid_tool_error_messages.append(state.output)
                state.round_invalid_tool_names.append(state.tool_name)
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.output,
                        "error_category": "tool",
                    },
                )
                state.conversation_messages.append(
                    {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                )
                continue
            # Resolve the model-facing broker call to one already
            # conversation-scoped binding before policy evaluation.
            # The model cannot choose a server, raw transport, or
            # unscoped tool name; it can only use a reference returned
            # from this request's private catalogue search.
            if state.tool_name == MCP_CALL_TOOL:
                state.requested_ref = str(state.arguments.get("tool_ref") or "").strip()
                state.requested_arguments = state.arguments.get("arguments")
                state.resolved_binding = self.mcp_broker.resolve(
                    state.mcp_bindings, state.requested_ref
                )
                state.requested_arguments_too_large = False
                if isinstance(state.requested_arguments, dict):
                    state.requested_arguments_too_large = (
                        len(
                            json.dumps(
                                state.requested_arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            )
                        )
                        > self.mcp_max_argument_chars_per_call
                    )
                if state.resolved_binding is None or not isinstance(
                    state.requested_arguments, dict
                ):
                    state.output = (
                        "The MCP tool reference or arguments were invalid. "
                        "Search for a connected tool and provide its exact tool_ref "
                        "with a JSON object of arguments."
                    )
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": MCP_CALL_TOOL,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "tool",
                        },
                    )
                    state.conversation_messages.append(
                        {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                    )
                    continue
                if state.requested_arguments_too_large:
                    state.output = (
                        "The MCP arguments exceed the safe per-call budget. "
                        "Send a smaller request or use the connected service's pagination "
                        "arguments."
                    )
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": MCP_CALL_TOOL,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "budget",
                        },
                    )
                    state.conversation_messages.append(
                        {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                    )
                    continue
                # Preserve the conversation-scoped binding after translating
                # the private broker call into the exact exposed MCP name.
                # Policy evaluation below must use the MCP connection policy,
                # not the native productivity-tool allowlist.
                state.mcp_binding = state.resolved_binding
                state.tool_name = state.resolved_binding.exposed_name
                state.arguments = state.requested_arguments
            # A provider may emit an unoffered native function call. The
            # schemas were supplied directly for this plan/task stage, so an
            # omitted native tool is refused rather than loaded on demand.
            # Brokered MCP calls remain valid because the compact broker
            # itself was offered and its exact tool reference is resolved above.
            if state.model_tool_name not in state.permitted_tool_names:
                if state.model_tool_name == "discover_tools":
                    state.legacy_discovery_attempts = int(state.legacy_discovery_attempts or 0) + 1
                    repeated_discovery = (
                        state.legacy_discovery_attempts > MAX_NATIVE_DISCOVERY_ATTEMPTS_PER_TURN
                    )
                    state.output = (
                        "Native tool schemas are already supplied for this request. Call a supplied "
                        "tool directly; discover_tools is unavailable."
                    )
                    if repeated_discovery:
                        state.output = (
                            "Repeated native tool discovery was stopped. The authorized tools were "
                            "already supplied, and no discovery or tool action was executed."
                        )
                        state.forced_answer = (
                            "DeepSpace stopped repeated native tool discovery. No discovery action "
                            "was run; start a new turn and use the tools shown for the selected preset."
                        )
                        state.terminal_status = "blocked"
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": state.model_tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "loop_guard" if repeated_discovery else "policy",
                            "error_code": "REPEATED_TOOL_DISCOVERY" if repeated_discovery else None,
                            "recoverable": not repeated_discovery,
                        },
                    )
                    state.conversation_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": state.call_id,
                            "content": state.output,
                        }
                    )
                    if repeated_discovery:
                        state.control = "break"
                        break
                    continue
                state.output = (
                    f"The tool {state.model_tool_name!r} is not available for this request. "
                    "Choose one of the currently supplied tools."
                )
                if state.managed_task_run and state.task_lifecycle_stage != "work":
                    state.deferred_message = (
                        "The call was deferred without execution. Complete the current persisted "
                        "task step before requesting this capability."
                    )
                    yield sse(
                        "tool_result",
                        {
                            "tool_name": state.model_tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "output": state.deferred_message,
                            "success": True,
                            "deferred": True,
                            "message": "Deferred",
                            "turn_index": state.round_index,
                        },
                    )
                    state.output = state.deferred_message
                else:
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": state.model_tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "policy",
                        },
                    )
                state.conversation_messages.append(
                    {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                )
                continue
            if NATIVE_TOOL_REGISTRY.contains(state.tool_name):
                state.arguments, state.arguments_compacted = (
                    self._repair_tool_arguments_for_validation(
                        state.tool_name,
                        state.arguments,
                    )
                )
                try:
                    state.arguments = NATIVE_TOOL_REGISTRY.validate(
                        state.tool_name, state.arguments
                    )
                except NativeToolValidationError as exc:
                    state.exc = exc
                    state.invalid_tool_arguments = True
                    state.invalid_tool_call_ids.add(state.call_id)
                    state.output = (
                        f"The {state.tool_name} arguments do not match its tool contract: {state.exc}. "
                        "Retry with one valid JSON object and do not invent unsupported fields."
                    )
                    state.round_invalid_tool_error_messages.append(state.output)
                    state.round_invalid_tool_names.append(state.tool_name)
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": state.tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "validation",
                        },
                    )
                    state.conversation_messages.append(
                        {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                    )
                    continue
                if state.arguments_compacted:
                    logger.info(
                        "Compacted oversized todo_write evidence before validation",
                        extra={"tool_name": state.tool_name, "call_id": state.call_id},
                    )
            state.lifecycle_error = None
            if state.starts_managed_plan and (
                state.call_index != state.first_call_index or state.tool_name != "todo_write"
            ):
                state.lifecycle_error = (
                    "A new managed plan must begin with one todo_write call. Wait for its persisted "
                    "result before selecting the next tool."
                )
            elif not state.allow_existing_task_state and (
                state.tool_name == "todo_write"
                and str(state.arguments.get("operation") or "") != "replace"
            ):
                state.lifecycle_error = (
                    "This is a new request. Do not read or modify an earlier task ledger unless the user "
                    "explicitly asks about tasks or continues the saved task."
                )
            elif state.managed_task_run:
                if (
                    state.call_index != state.first_call_index
                    and not state.parallel_managed_work_batch
                ):
                    state.lifecycle_error = (
                        "Managed task execution accepts one ordered tool call per provider turn. "
                        "Only a bounded group of independent native read calls is allowed together "
                        "while working a task; continue dependent steps in a new turn."
                    )
                elif (
                    state.tool_name not in state.permitted_tool_names
                    and state.model_tool_name not in state.permitted_tool_names
                ):
                    state.lifecycle_error = (
                        f"The current managed-task stage requires a different tool; {state.tool_name!r} is not "
                        "available for this step."
                    )
                elif state.task_lifecycle_stage == "start_task":
                    if (
                        state.tool_name != "todo_write"
                        or str(state.arguments.get("operation") or "") != "update"
                        or str(state.arguments.get("task_id") or "")
                        != str(state.active_task_id or "")
                        or str(state.arguments.get("status") or "") != "in_progress"
                    ):
                        state.lifecycle_error = (
                            "Start the current task with todo_write operation='update', its persisted "
                            "task ID, and status 'in_progress'."
                        )
                elif state.task_lifecycle_stage == "work" and state.tool_name == "todo_write":
                    if str(state.arguments.get("operation") or "") != "update":
                        state.lifecycle_error = "The active task can only be updated with todo_write(operation='update')."
                    elif str(state.arguments.get("task_id") or "") != str(
                        state.active_task_id or ""
                    ) or str(state.arguments.get("status") or "") not in {
                        "completed",
                        "blocked",
                        "failed",
                    }:
                        state.lifecycle_error = (
                            "Only the active task may receive a terminal todo_write update with status "
                            "'completed', 'blocked', or 'failed'."
                        )
                    elif not str(state.arguments.get("evidence") or "").strip():
                        state.lifecycle_error = (
                            "A terminal task update requires concise evidence from the real work."
                        )
                    elif (
                        str(state.arguments.get("status") or "") == "completed"
                        and not state.task_has_work_evidence
                    ):
                        state.lifecycle_error = (
                            "Use at least one real work, research, workspace, or connected-service tool before "
                            "marking this task completed. A blocked or failed task needs evidence describing the blocker."
                        )
                if state.lifecycle_error:
                    # A managed-task ordering decision is control flow,
                    # not a failed provider operation. Return a normal
                    # tool result so the provider receives a result for
                    # every call it emitted, while the UI does not
                    # report a false execution failure. The next
                    # provider round sees the persisted lifecycle
                    # state and can select the appropriate call.
                    state.deferred_message = (
                        "The call was deferred without execution. Managed task steps run in order; "
                        "continue with the current persisted step before requesting this action."
                    )
                    yield sse(
                        "tool_result",
                        {
                            "tool_name": state.tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "output": state.deferred_message,
                            "success": True,
                            "deferred": True,
                            "message": "Deferred",
                            "turn_index": state.round_index,
                        },
                    )
                    state.conversation_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": state.call_id,
                            "content": state.deferred_message,
                        }
                    )
                    continue
            state.signature = hashlib.sha256(
                json.dumps(
                    {
                        "name": state.tool_name,
                        "arguments": state.arguments,
                        "task_stage": (
                            state.task_lifecycle_stage if state.tool_name == "todo_write" else None
                        ),
                        "active_task_id": (
                            state.active_task_id if state.tool_name == "todo_write" else None
                        ),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            state.seen_tool_calls[state.signature] = (
                state.seen_tool_calls.get(state.signature, 0) + 1
            )
            state.duplicate_limit = (
                1
                if state.tool_name == "todo_write"
                and str(state.arguments.get("operation") or "") == "check"
                else 2
            )
            if state.tool_name == "wait_agent":
                if state.wait_agent_started_monotonic is None:
                    state.wait_agent_started_monotonic = time.monotonic()
                state.wait_agent_attempts += 1
                wait_elapsed = time.monotonic() - state.wait_agent_started_monotonic
                wait_budget_exhausted = (
                    state.wait_agent_attempts
                    > _pkg_attr(
                        "MAX_WAIT_AGENT_ATTEMPTS_PER_TURN", MAX_WAIT_AGENT_ATTEMPTS_PER_TURN
                    )
                    or wait_elapsed >= MAX_WAIT_AGENT_TOTAL_SECONDS_PER_TURN
                )
            else:
                wait_elapsed = 0.0
                wait_budget_exhausted = False
            if state.tool_name == "wait_agent" and wait_budget_exhausted:
                wait_limit_already_reported = bool(state.wait_agent_limit_notified)
                state.wait_agent_limit_notified = True
                state.output = json.dumps(
                    {
                        "status": "ok",
                        "timed_out": True,
                        "changed": [],
                        "active_members": [],
                        "all_tasks_completed": False,
                        "wait_limit_reached": True,
                        "wait_attempts": state.wait_agent_attempts,
                        "wait_elapsed_seconds": round(wait_elapsed, 3),
                        "message": (
                            "No new team change was detected. Re-check teammates and tasks."
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield sse(
                    "tool_result",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "output": state.output,
                        "success": True,
                        "recoverable": True,
                        "wait_limit_reached": True,
                        "turn_index": state.round_index,
                    },
                )
                state.conversation_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": state.call_id,
                        "content": state.output,
                    }
                )
                if wait_limit_already_reported:
                    state.forced_answer = (
                        "No new team change was detected after the bounded wait period. "
                        "Re-check teammates and tasks when you are ready to continue."
                    )
                    state.terminal_status = "completed"
                    state.control = "break"
                    break
                # Do not set terminal_status or forced_answer on the first
                # limit result: this is a normal read result, so the provider
                # gets one more turn to inspect the board and answer.
                continue
            if (
                state.seen_tool_calls[state.signature] > state.duplicate_limit
                and state.tool_name not in REPEATABLE_COORDINATION_READ_TOOLS
            ):
                state.output = json.dumps(
                    {
                        "status": "denied",
                        "error_code": "DUPLICATE_TOOL_CALL",
                        "message": (
                            f"The same {state.tool_name} call was requested repeatedly. "
                            "The repeated call was not executed. Saved progress remains available."
                        ),
                        "tool_name": state.tool_name,
                        "duplicate_guard": True,
                        "stop_turn": True,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.output,
                        "error_code": "DUPLICATE_TOOL_CALL",
                        "error_category": "loop_guard",
                        "recoverable": False,
                    },
                )
                state.conversation_messages.append(
                    {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                )
                self._request_model_final_answer_without_tools(
                    state,
                    reason=(
                        f"The same {state.tool_name} call was requested repeatedly without progress."
                    ),
                )
                break
            resolved_binding = state.resolved_binding
            state.mcp_binding = (
                resolved_binding
                if resolved_binding is not None and state.tool_name == resolved_binding.exposed_name
                else state.mcp_bindings.get(state.tool_name)
            )
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
            state.mcp_budget_error = None
            if state.tool_name in {MCP_SEARCH_TOOLS, MCP_GET_TOOL_SCHEMA, MCP_GET_RESULT}:
                if state.mcp_result_chars + 256 >= self.mcp_max_result_chars_per_turn:
                    state.mcp_budget_error = (
                        "The MCP result budget for this turn is exhausted. "
                        "The collected results are available for the answer."
                    )
                elif state.mcp_discovery_call_count >= self.mcp_max_discovery_calls_per_turn:
                    state.mcp_budget_error = (
                        "The MCP discovery budget for this turn is exhausted. "
                        "Use the existing results or start a new request."
                    )
                else:
                    state.mcp_discovery_call_count += 1
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_budget["discovery"] = max(
                            int(state.mcp_budget.get("discovery", 0)),
                            int(state.mcp_discovery_call_count),
                        )
            elif state.mcp_binding is not None:
                if state.mcp_call_count >= self.mcp_max_calls_per_turn:
                    state.mcp_budget_error = (
                        "The MCP remote-call budget for this turn is exhausted. "
                        "Use the results already collected or start a new request."
                    )
                elif state.mcp_result_chars + 256 >= self.mcp_max_result_chars_per_turn:
                    state.mcp_budget_error = (
                        "The MCP result budget for this turn is exhausted. "
                        "The collected results are available for the answer."
                    )
                else:
                    state.mcp_call_count += 1
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_budget["calls"] = max(
                            int(state.mcp_budget.get("calls", 0)),
                            int(state.mcp_call_count),
                        )
            if state.mcp_budget_error is not None:
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.mcp_budget_error,
                        "error_category": "budget",
                    },
                )
                state.conversation_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": state.call_id,
                        "content": state.mcp_budget_error,
                    }
                )
                continue
            if state.tool_name in {MCP_SEARCH_TOOLS, MCP_GET_TOOL_SCHEMA, MCP_GET_RESULT}:
                state.decision = MCPToolPolicyDecision(
                    True,
                    mode="always_allow",
                    risk_level="read",
                    approval_requirement="auto",
                    reason="DeepSpace MCP broker metadata operation.",
                )
            else:
                state.decision = (
                    self.mcp_bridge.policy_for_tool(
                        auth=state.auth,
                        conversation_id=state.conversation_id,
                        binding=state.mcp_binding,
                        permission_mode=state.sandbox_mode,
                    )
                    if state.mcp_binding is not None
                    else self.tool_policy.before_tool(state.tool_name, state.arguments)
                )
            if not state.decision.allowed:
                state.output = state.decision.reason or "Tool blocked by DeepSpace policy."
                state.policy_denial_count = int(state.policy_denial_count or 0) + 1
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.output,
                        "error_category": "policy",
                    },
                )
                state.conversation_messages.append(
                    {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
                )
                if state.policy_denial_count >= MAX_POLICY_DENIALS_PER_TURN:
                    self._request_model_final_answer_without_tools(
                        state,
                        reason=f"Tool requests were repeatedly blocked by policy: {state.output}",
                    )
                    break
                continue
            state.requires_approval = bool(getattr(state.decision, "requires_approval", False))
            state.risk_level = str(getattr(state.decision, "risk_level", "medium"))
            mandatory_confirmation = state.risk_level == "critical"
            if state.requires_approval:
                guard_refusal = self._loop_guard_refusal(
                    tool_name=state.tool_name,
                    arguments=state.arguments,
                    conversation_id=state.conversation_id,
                    record=False,
                )
                if guard_refusal is not None:
                    state.output = json.dumps(
                        guard_refusal,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    state.guard_refusal = True
                    state.valid_calls = []
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": state.tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_code": guard_refusal.get("error_code"),
                            "error_category": "loop_guard",
                            "recoverable": False,
                        },
                    )
                    state.conversation_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": state.call_id,
                            "content": state.output,
                        }
                    )
                    state.forced_answer = (
                        "DeepSpace stopped the turn because the same tool request was repeated. "
                        "No new approval was requested and the repeated action was not run."
                    )
                    state.terminal_status = "blocked"
                    state.control = "break"
                    break
            if state.requires_approval and not mandatory_confirmation:
                review = await self._auto_review_tool_call(
                    state, str(state.tool_name), state.arguments
                )
                if review is not None and review.approved:
                    state.requires_approval = False
                    yield sse(
                        "tool_auto_approved",
                        {
                            "tool_name": state.tool_name,
                            "tool_id": state.call_id,
                            "reason": review.reason,
                            "reviewed_by": review.reviewed_by,
                        },
                    )
                elif review is not None:
                    state.output = f"Auto-review denied {state.tool_name}: {review.reason}"
                    yield sse(
                        "tool_error",
                        {
                            "tool_name": state.tool_name,
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "error": state.output,
                            "error_category": "policy",
                        },
                    )
                    state.conversation_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": state.call_id,
                            "content": state.output,
                        }
                    )
                    continue
            if state.requires_approval:
                approval_label = (
                    f"{state.mcp_binding.server.name} {state.mcp_binding.raw_name}"
                    if state.mcp_binding is not None
                    else str(state.tool_name)
                )
                approval_warning = str(
                    getattr(state.decision, "approval_warning", "") or ""
                ).strip() or tool_confirmation_warning(
                    str(state.tool_name),
                    state.arguments,
                    permission_mode=state.sandbox_mode,
                    workspace_root=self._resolve_workspace_root(state),
                )
                state.awaiting_approval = {
                    "approval_id": str(uuid.uuid4()),
                    "call_id": state.call_id,
                    "turn_index": state.round_index,
                    "step_id": state.step_id,
                    "tool_name": state.tool_name,
                    "mcp_server": (
                        state.mcp_binding.server.name if state.mcp_binding is not None else None
                    ),
                    "mcp_tool": (
                        state.mcp_binding.raw_name if state.mcp_binding is not None else None
                    ),
                    "tool_input": state.arguments,
                    "risk_level": state.risk_level,
                    "warning": approval_warning,
                    "permission_level": "human",
                    "message": (
                        f"{approval_label} wants to run. "
                        f"This {state.risk_level} action requires your approval."
                    ),
                    "decision": "pending",
                    "requested_at": self._now(),
                }
                break
            state.valid_calls.append(
                {
                    "call": state.call,
                    "call_id": state.call_id,
                    "tool_name": state.tool_name,
                    "arguments": state.arguments,
                    "step_id": state.step_id,
                }
            )
