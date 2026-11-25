"""Tools mixin for DeepSpaceChatService."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.auth.dependencies import AuthContext
from app.core.config import DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS
from app.deepspace.services.chat_service.productivity_tools import _ProductivityToolsMixin
from app.deepspace.services.chat_service.utils import (
    _FAKE_TOOL_MARKER_RE,
    FATAL_TOOL_ERROR_CATEGORIES,
    MAX_COORDINATION_READS_WITHOUT_PROGRESS_PER_TOOL,
    MAX_TOOL_RETRIES,
    MIN_TOOL_CALL_SECONDS,
    NATIVE_TOOL_REGISTRY,
    RECOVERABLE_RESEARCH_TOOL_NAMES,
    RECOVERABLE_TOOL_ERROR_CATEGORIES,
    RECOVERABLE_TOOL_ERROR_CODES,
    TODO_WRITE_EVIDENCE_COMPACTION_MARKER,
    TODO_WRITE_EVIDENCE_MAX_CHARS,
    _cached_native_tool_schema_json,
    _pkg_attr,
    _request_run_id,
    _TurnState,
    _unlocked_gate,
    summarize_tool_arguments,
)
from app.deepspace.services.conversation_execution_limiter import (
    SharedExecutionLimitError,
    conversation_execution_gate,
    shared_conversation_execution_slot,
)
from app.deepspace.services.execution_batches import ExecutionBatch, plan_execution_batches
from app.deepspace.services.hook_gate import (
    APPROVED_CALL,
    APPROVED_FULL_ACCESS_CALL,
    call_signature,
)
from app.deepspace.services.host_access import (
    is_known_read_only_command,
    tool_requires_human_confirmation,
)
from app.deepspace.services.loop_guard import (
    COORDINATION_PROGRESS_READ_TOOLS,
    REPEATABLE_COORDINATION_READ_TOOLS,
    LoopGuardLimits,
    guard_result,
    loop_guard_registry,
)
from app.deepspace.services.mcp_bridge import DeepSpaceMCPTool
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL,
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
)
from app.deepspace.services.permission_presets import AutoReviewEngine
from app.deepspace.services.presets import (
    PresetResolution,
)
from app.deepspace.services.reasoning_privacy import (
    redact_reasoning_text,
)
from app.deepspace.services.repeat_tool_reminder import repeat_tool_reminder_registry
from app.deepspace.services.runtime_policy import ToolPolicyDecision
from app.deepspace.services.session_events import (
    TOOL_CALL,
    TOOL_RESULT,
)
from app.deepspace.services.spill_store import SpillRef, SpillStoreError, spill_result
from app.deepspace.services.subagent_runner import CHILD_TURN_TOOL_NAMES
from app.deepspace.services.tool_pipeline import DeepSpaceToolPipeline
from app.deepspace.services.tool_requirements import (
    requires_connected_service_action,
)
from app.deepspace.services.tools import (
    NATIVE_TOOL_DEADLINES,
    ToolContext,
    dispatch_native_tool,
)
from app.integrations.services.mcp_runtime import MCPToolPolicyDecision
from app.providers.services import ChatGenerateRequest
from app.providers.services.types import WebSearchRequest, WebSearchResponse

logger = logging.getLogger(__name__)


class _ToolsMixin(_ProductivityToolsMixin):
    @property
    def tool_pipeline(self) -> DeepSpaceToolPipeline:
        """The pipeline every tool call traverses.

        A service that was built without the constructor still gets a
        pipeline, so a partially composed runtime never loses the stages.
        The pipeline carries the deadline each tool declares, so a tool that
        declares one runs under it instead of the deployment's default.
        """
        pipeline = getattr(self, "_tool_pipeline", None)
        if pipeline is None:
            pipeline = DeepSpaceToolPipeline(
                self.tool_policy,
                settings=getattr(self, "settings", None),
                tool_timeouts=NATIVE_TOOL_DEADLINES,
            )
            pipeline.register_pre("deepspace-hooks", self._hook_participant())
            self._tool_pipeline = pipeline
        return pipeline

    def repeat_tool_reminder(self, conversation_id: uuid.UUID):
        """Get the repeat tool reminder tracker for a conversation."""
        return repeat_tool_reminder_registry().get(str(conversation_id))

    @staticmethod
    def _looks_like_fake_tool_output(text: str) -> bool:
        """Detect tool syntax printed as prose, without ever executing it."""

        lowered = text.casefold()
        if _FAKE_TOOL_MARKER_RE.search(text) or any(
            marker in lowered
            for marker in (
                "function_calls",
                "function_call>",
                "｜dsml｜invoke",
                "｜dsml｜parameter",
            )
        ):
            return True
        normalized = text.replace("```json", "").replace("```", "").strip()
        decoder = json.JSONDecoder()
        candidates: list[object] = []
        for index, character in enumerate(normalized):
            if character not in "[{":
                continue
            try:
                payload, _end = decoder.raw_decode(normalized[index:])
            except json.JSONDecodeError:
                continue
            candidates.append(payload)

        def is_tool_payload(payload: object) -> bool:
            if isinstance(payload, list):
                return any(is_tool_payload(item) for item in payload)
            if not isinstance(payload, dict):
                return False
            keys = {str(key).casefold() for key in payload}
            if "function-call" in keys:
                return True
            if "tool_name" in keys and keys.intersection(
                {"arguments", "parameters", "tool_input", "input"}
            ):
                return True
            return any(is_tool_payload(value) for value in payload.values())

        if any(is_tool_payload(payload) for payload in candidates):
            return True
        return False

    @staticmethod
    def _tool_name(call: dict[str, Any]) -> str:
        function = call.get("function")
        return str(function.get("name") or "unknown") if isinstance(function, dict) else "unknown"

    @staticmethod
    def _repair_tool_arguments_for_validation(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Repair bounded, non-action metadata before strict tool validation.

        Providers often place a complete source list in ``todo_write``
        completion evidence. Evidence is descriptive audit metadata, so it is
        safe to compact that one field while keeping the typed contract strict
        for every other argument and every other tool.
        """

        if tool_name == "write" and "mode" in arguments:
            normalized = dict(arguments)
            mode = normalized.pop("mode")
            if "operation" not in normalized and mode in {
                "create",
                "replace",
                "append",
            }:
                normalized["operation"] = mode
            return normalized, True
        if tool_name != "todo_write":
            return arguments, False
        evidence = arguments.get("evidence")
        if not isinstance(evidence, str) or len(evidence) <= TODO_WRITE_EVIDENCE_MAX_CHARS:
            return arguments, False
        marker = TODO_WRITE_EVIDENCE_COMPACTION_MARKER
        available = TODO_WRITE_EVIDENCE_MAX_CHARS - len(marker)
        head_chars = min(650, available)
        tail_chars = available - head_chars
        compacted = f"{evidence[:head_chars]}{marker}{evidence[-tail_chars:]}"
        return {**arguments, "evidence": compacted}, True

    @staticmethod
    def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
        return {
            str(item.get("function", {}).get("name") or "")
            for item in tools
            if isinstance(item.get("function"), dict)
        }

    @staticmethod
    def _direct_native_tool_definitions(
        tool_names: set[str], *, policy_version: str = "native-tool-policy-v1"
    ) -> list[dict[str, Any]]:
        """Return cached schemas for the current allowlist as fresh dictionaries."""
        names = sorted(
            name
            for name in tool_names
            if name != "discover_tools" and NATIVE_TOOL_REGISTRY.contains(name)
        )
        schema_key = tuple((name, NATIVE_TOOL_REGISTRY.spec(name).schema_digest) for name in names)
        return [
            json.loads(definition)
            for definition in _cached_native_tool_schema_json(schema_key, policy_version)
        ]

    def _consume_screenshot_tool_result(self, state: Any) -> dict[str, Any] | None:
        """Attach a screenshot tool image to model vision input and chat artifacts."""
        if not isinstance(state.tool_payload, dict):
            return None
        raw_b64 = state.tool_payload.get("image_base64")
        if not isinstance(raw_b64, str) or not raw_b64.strip():
            return None
        encoded = raw_b64.strip()
        raw_fmt = str(state.tool_payload.get("format") or "png").strip().lower()
        if raw_fmt == "jpg":
            raw_fmt = "jpeg"
        if raw_fmt not in {"png", "jpeg", "webp", "gif"}:
            raw_fmt = "png"
        content_type = f"image/{raw_fmt}"

        state.pending_images.append(f"data:{content_type};base64,{encoded}")
        cleaned_payload = {
            key: value for key, value in state.tool_payload.items() if key != "image_base64"
        }

        artifact: dict[str, Any] | None = None
        if (
            getattr(self, "media_artifacts", None) is not None
            and getattr(state, "assistant_message", None) is not None
        ):
            title = (
                "Browser screenshot"
                if state.tool_name == "browser_screenshot"
                else "Desktop screenshot"
            )
            provider_type = (
                "browser_automation" if state.tool_name == "browser_screenshot" else "computer_use"
            )
            try:
                artifact = self.media_artifacts.persist_content_base64(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    message_id=state.assistant_message.id,
                    content_type=content_type,
                    data_base64=encoded,
                    provider_type=provider_type,
                    model_name=None,
                    title=title,
                    metadata={"source": state.tool_name, "turn_index": state.round_index},
                    requested_kind="image",
                )
            except Exception:  # noqa: BLE001 - keep turn alive if artifact storage fails
                logger.warning(
                    "DeepSpace screenshot artifact could not be persisted",
                    exc_info=True,
                    extra={
                        "conversation_id": str(state.conversation_id),
                        "tool_name": state.tool_name,
                    },
                )
        if isinstance(artifact, dict) and artifact.get("id"):
            state.generated_artifacts.append(artifact)
            cleaned_payload["artifact_id"] = artifact["id"]
            cleaned_payload["artifact_url"] = artifact.get("url")
            cleaned_payload["content_type"] = artifact.get("content_type") or content_type
            cleaned_payload["size_bytes"] = artifact.get("size_bytes")
        state.tool_payload = cleaned_payload
        return artifact

    def _spill_tool_result(
        self,
        *,
        auth: AuthContext,
        tool_name: str,
        payload: dict[str, Any],
        preview_budget_tokens: int = 1_024,
    ) -> tuple[dict[str, Any], SpillRef | None]:
        """Offload one tool result that is too large for the model context.

        A result the deployment cannot store is refused instead of ending the
        turn, so the model is told to narrow the call and the turn continues.

        Args:
            auth: The authenticated tenant and user.
            tool_name: The tool whose result is spilled.
            payload: The tool's result.

        Returns:
            The payload the model receives, and the reference when the result
            was spilled.
        """
        try:
            return spill_result(
                settings=self.settings,
                tenant_id=auth.tenant_id,
                tool_name=tool_name,
                payload=payload,
                threshold_chars=self._spill_threshold_chars(),
                preview_budget_tokens=max(64, min(1_024, int(preview_budget_tokens))),
            )
        except SpillStoreError as exc:
            return (
                {
                    "status": "refused",
                    "error": str(exc),
                    "message": (
                        "The result is larger than this deployment can store, so it was "
                        "neither carried in the context nor saved. Narrow the call - a "
                        "smaller range, a shorter output, or a summary - and try again."
                    ),
                },
                None,
            )

    def _spill_threshold_chars(self) -> int:
        """Return the character count above which a tool result spills.

        The preset the conversation runs may narrow the deployment's own
        threshold, so the smaller of the two applies.

        Returns:
            The threshold in characters.
        """
        resolution = getattr(self, "_preset_resolution", None)
        if isinstance(resolution, PresetResolution) and resolution.spill_threshold_chars:
            return int(resolution.spill_threshold_chars)
        return int(getattr(self.settings, "deepspace_spill_threshold_chars", 24_000))

    @staticmethod
    def _requires_connected_service_tool(
        prompt: str, mcp_bindings: dict[str, DeepSpaceMCPTool]
    ) -> bool:
        """Require a call when the user explicitly asks for an attached service.

        A short request such as "check my Gmail" used to leave tool choice on
        ``auto``. Some models then answered from their generic training rather
        than calling the Gmail tool they had been given. Keep ordinary writing
        requests conversational, but require a tool when the user names an
        attached service or explicitly asks to use MCP.

        A conversation may hold several attached services at once, and then an
        action verb alone does not say which one the user means. Only a service
        the user named is required in that case, so a request the native tools
        can serve is not blocked behind an unrelated connected service.
        """
        return requires_connected_service_action(prompt, mcp_bindings)

    def resolve_web_search_state(
        self, auth: AuthContext
    ) -> tuple[Any | None, Any | None, dict[str, tuple[Any, Any]]]:
        """Resolve configured web search providers for the main agent or a child worker."""
        web_candidate: Any | None = None
        web_provider: Any | None = None
        web_search_options: dict[str, tuple[Any, Any]] = {}
        try:
            web_selection = self.providers.resolve_web_search(
                tenant_id=auth.tenant_id,
                workspace_id=None,
                actor_user_id=auth.user_id,
            )
            for candidate in web_selection.candidates:
                provider_name = str(candidate.provider_type or "").casefold()
                if provider_name not in {"searxng", "tavily"}:
                    continue
                if provider_name in web_search_options:
                    continue
                try:
                    provider = self.registry.get_web_search_provider_from_selection(candidate)
                except Exception:  # noqa: BLE001 - expose only providers that initialized.
                    logger.warning(
                        "DeepSpace web-search provider %s could not initialize",
                        provider_name,
                        exc_info=True,
                    )
                    continue
                web_search_options[provider_name] = (provider, candidate)
            if web_search_options:
                default_name = next(iter(web_search_options))
                web_provider, web_candidate = web_search_options[default_name]
        except Exception:  # noqa: BLE001
            logger.warning(
                "DeepSpace web search is unavailable; continuing without the tool",
                exc_info=True,
            )
            web_candidate = None
            web_provider = None
            web_search_options = {}
        return web_provider, web_candidate, web_search_options

    def build_tool_context(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None = None,
        web_candidate: Any | None = None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        mcp_budget: dict[str, int] | None = None,
        request: Any | None = None,
        depth: int = 0,
        parent_agent_id: uuid.UUID | None = None,
        sandbox_mode: str | None = None,
        workspace_root: Path | None = None,
        run_id: uuid.UUID | None = None,
        skill_scope: Any = None,
        progress_callback: Callable[[str], None] | None = None,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> ToolContext:
        """Build the context one native tool call runs with.

        The context carries the request's own state, so a tool never reaches
        back into the chat service for it.  The executor it holds is the
        service's own tool dispatcher, which is what makes a delegated child's
        tools behave exactly like the parent's.

        Args:
            auth: The authenticated tenant and user.
            conversation_id: The conversation the call belongs to.
            web_provider: The web search provider, when one is configured.
            web_candidate: The web search selection, when one is configured.
            request: The incoming request, when the call has one.
            depth: The delegation depth of the calling agent.
            parent_agent_id: The immediate parent child, for a delegated call.
            sandbox_mode: The sandbox mode this conversation runs in.
            workspace_root: The root of the workspace this conversation works
                in.
            skill_scope: The skills this agent may see, or None when it is
                the unrestricted main agent.

        Returns:
            The tool context.
        """
        return ToolContext(
            auth=auth,
            conversation_id=conversation_id,
            settings=self.settings,
            # A partially composed runtime still serves the tools that do not
            # need the database, the ledger, or a provider route.
            db=getattr(self, "db", None),
            task_store=getattr(self, "task_store", None),
            tool_result_store=getattr(self, "tool_result_store", None),
            provider_registry=getattr(self, "registry", None),
            provider_selection=getattr(self, "providers", None),
            native_executor=self._package_tool_executor(
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                web_search_options=web_search_options,
                mcp_bindings=mcp_bindings,
                mcp_budget=mcp_budget,
                sandbox_mode=sandbox_mode,
                request=request,
                skill_scope=skill_scope,
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
            ),
            web_search_options=web_search_options or {},
            mcp_bindings=mcp_bindings or {},
            tool_policy=getattr(self, "tool_policy", None),
            sandbox_mode=sandbox_mode,
            workspace_root=workspace_root,
            run_id=run_id or _request_run_id(request),
            parent_agent_id=parent_agent_id,
            depth=depth,
            skill_scope=skill_scope,
            progress_callback=progress_callback,
            activity_callback=activity_callback,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )

    def _package_tool_executor(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        mcp_budget: dict[str, int] | None = None,
        sandbox_mode: str | None = None,
        skill_scope: Any = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> Any:
        """Build the executor a delegated child's tool calls run through.

        Args:
            auth: The authenticated tenant and user.
            conversation_id: The conversation the call belongs to.
            web_provider: The web search provider, when one is configured.
            web_candidate: The web search selection, when one is configured.
            request: The incoming request, when the call has one.
            skill_scope: The skills the calling agent may see, carried into
                every nested tool call it dispatches.

        Returns:
            An awaitable that runs one native tool call.
        """
        active_mcp_budget = (
            mcp_budget
            if mcp_budget is not None
            else {"calls": 0, "discovery": 0, "result_chars": 0}
        )

        async def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name == MCP_BROKER_TOOL:
                operation = str(arguments.get("operation") or "").casefold()
                is_read = operation in {"search", "schema", "result"}
                decision = None
            else:
                decision = self.tool_policy.before_tool(name, arguments)
                if not decision.allowed:
                    return {
                        "status": "denied",
                        "error": decision.reason
                        or "The nested tool is blocked by DeepSpace policy.",
                    }
                if decision.requires_approval:
                    return {
                        "status": "requires_approval",
                        "requires_approval": True,
                        "error": (
                            f"{name} needs a direct tool call so DeepSpace can show its approval "
                            "request. No nested action was run."
                        ),
                    }
                is_read = decision.mode == "read" or decision.risk_level == "read"

            try:
                async with _pkg_attr(
                    "shared_conversation_execution_slot", shared_conversation_execution_slot
                )(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    is_read=is_read,
                    max_parallel_reads=int(
                        getattr(self.settings, "deepspace_max_concurrent_tools", 8)
                    ),
                    timeout_seconds=float(
                        getattr(self.settings, "deepspace_tool_timeout_seconds", 120)
                    ),
                ):
                    if name == MCP_BROKER_TOOL:
                        return await self._execute_nested_mcp_broker_call(
                            arguments=arguments,
                            auth=auth,
                            conversation_id=conversation_id,
                            bindings=mcp_bindings or {},
                            permission_mode=sandbox_mode,
                            budget=active_mcp_budget,
                        )
                    return await self._execute_productivity_tool(
                        tool_name=name,
                        arguments=arguments,
                        auth=auth,
                        conversation_id=conversation_id,
                        web_provider=web_provider,
                        web_candidate=web_candidate,
                        web_search_options=web_search_options,
                        mcp_bindings=mcp_bindings,
                        mcp_budget=active_mcp_budget,
                        request=request,
                        skill_scope=skill_scope,
                        thinking_enabled=thinking_enabled,
                        reasoning_effort=reasoning_effort,
                    )
            except SharedExecutionLimitError as exc:
                return {
                    "status": "capacity",
                    "error": str(exc),
                    "error_code": exc.code,
                }

        return execute

    async def _execute_nested_mcp_broker_call(
        self,
        *,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        bindings: dict[str, DeepSpaceMCPTool],
        permission_mode: str | None,
        budget: dict[str, int],
    ) -> dict[str, Any]:
        """Run a child agent's MCP broker operation inside its parent scope."""
        operation = str(arguments.get("operation") or "").strip().casefold()
        if not bindings:
            return {"status": "unavailable", "message": "No MCP tools are connected for this turn."}
        if operation in {"search", "schema", "result"}:
            if budget["result_chars"] + 256 >= self.mcp_max_result_chars_per_turn:
                return {
                    "status": "budget_exhausted",
                    "message": "The child reached its MCP result budget.",
                }
            if budget["discovery"] >= self.mcp_max_discovery_calls_per_turn:
                return {
                    "status": "budget_exhausted",
                    "message": "The child reached its MCP search and schema budget.",
                }
            budget["discovery"] += 1
            if operation == "search":
                result = self.mcp_broker.search(
                    bindings,
                    str(arguments.get("query") or ""),
                    cursor=str(arguments.get("cursor") or "") or None,
                )
            elif operation == "schema":
                result = self.mcp_broker.get_schema(bindings, str(arguments.get("tool_ref") or ""))
            else:
                stored_result = self.mcp_result_store.get(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    ref=str(arguments.get("result_ref") or ""),
                )
                if stored_result is None:
                    return {
                        "status": "error",
                        "message": "The MCP result reference is missing or expired.",
                    }
                result = stored_result
            remaining = self.mcp_max_result_chars_per_turn - budget["result_chars"]
            bounded = self.mcp_broker.bound_result(
                result, max_chars=min(self.mcp_broker.max_result_chars, remaining - 128)
            )
            budget["result_chars"] += len(
                json.dumps(bounded, ensure_ascii=False, default=str, separators=(",", ":"))
            )
            if not isinstance(bounded, dict):
                return {"status": "error", "message": "The MCP result could not be bounded safely."}
            return {str(key): value for key, value in bounded.items()}
        if operation != "call":
            return {
                "status": "error",
                "message": "Choose MCP operation search, schema, call, or result.",
            }
        if budget["result_chars"] + 256 >= self.mcp_max_result_chars_per_turn:
            return {
                "status": "budget_exhausted",
                "message": "The child reached its MCP result budget.",
            }
        if budget["calls"] >= self.mcp_max_calls_per_turn:
            return {
                "status": "budget_exhausted",
                "message": "The child reached its MCP remote-call budget.",
            }
        binding = self.mcp_broker.resolve(bindings, str(arguments.get("tool_ref") or ""))
        if binding is None:
            return {"status": "error", "message": "That MCP tool is not available in this turn."}
        decision = self.mcp_bridge.policy_for_tool(
            auth=auth,
            conversation_id=conversation_id,
            binding=binding,
            permission_mode=permission_mode,
        )
        if not decision.allowed:
            return {
                "status": "denied",
                "message": decision.reason or "MCP policy blocked this call.",
            }
        if decision.requires_approval:
            return {
                "status": "requires_approval",
                "message": (
                    "This MCP action needs direct user approval. No remote action was run; "
                    "ask the parent agent to make it as a direct tool call."
                ),
            }
        budget["calls"] += 1
        result = await self.mcp_bridge.execute(
            auth=auth,
            conversation_id=conversation_id,
            binding=binding,
            arguments=(
                {str(key): value for key, value in arguments["arguments"].items()}
                if isinstance(arguments.get("arguments"), dict)
                else {}
            ),
            approval_granted=False,
        )
        if result.get("is_error") or result.get("status") == "error":
            return {"status": "error", "message": str(result.get("message") or "MCP call failed.")}
        full_result = {
            "mcp_server": binding.server_name,
            "mcp_tool": binding.raw_name,
            **result,
        }
        remaining = self.mcp_max_result_chars_per_turn - budget["result_chars"]
        bounded = self.mcp_broker.bound_result(
            full_result,
            max_chars=min(self.mcp_broker.max_result_chars, remaining - 256),
        )
        if isinstance(bounded, dict) and bounded.get("more_available") is True:
            result_ref = self.mcp_result_store.put(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                value=full_result,
            )
            bounded = {
                **bounded,
                "result_ref": result_ref,
                "message": "Use mcp_broker operation=result with this reference for more detail.",
            }
        budget["result_chars"] += len(
            json.dumps(bounded, ensure_ascii=False, default=str, separators=(",", ":"))
        )
        return bounded if isinstance(bounded, dict) else {"result": bounded}

    async def _execute_package_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
    ) -> dict[str, Any]:
        """Run one tool the native tool package owns.

        Args:
            tool_name: The tool to run.
            arguments: The validated arguments.
            auth: The authenticated tenant and user.
            conversation_id: The conversation the call belongs to.
            web_provider: The web search provider, when one is configured.
            web_candidate: The web search selection, when one is configured.
            request: The incoming request, when the call has one.

        Returns:
            The tool's result.

        Raises:
            ValueError: When the tool package does not own the name.
        """
        result = await self._dispatch_package_tool(
            tool_name=tool_name,
            arguments=arguments,
            auth=auth,
            conversation_id=conversation_id,
            web_provider=web_provider,
            web_candidate=web_candidate,
            request=request,
        )
        if result is None:
            raise ValueError(f"Tool '{tool_name}' is not available in DeepSpace.")
        return result

    async def _dispatch_package_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        mcp_budget: dict[str, int] | None = None,
        run_id: uuid.UUID | None = None,
        sandbox_mode: str | None = None,
        skill_scope: Any = None,
        progress_callback: Callable[[str], None] | None = None,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any] | None:
        """Run one tool the native tool package owns.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.
            auth: The authenticated tenant and user.
            conversation_id: The conversation the call belongs to.
            web_provider: The web search provider, when one is configured.
            web_candidate: The web search selection, when one is configured.
            web_search_options: The providers offered as choices for this turn.
            mcp_bindings: The conversation-scoped MCP snapshot available to this call.
            request: The incoming request, when the call has one.
            skill_scope: The skills the calling agent may see.

        Returns:
            The tool's result, or None when the tool package does not own the
            name and the caller's existing chain should handle it.
        """
        from app.deepspace.services.tools import NATIVE_TOOL_HANDLERS

        if tool_name not in NATIVE_TOOL_HANDLERS:
            return None
        effective_sandbox = (
            "danger-full-access"
            if APPROVED_FULL_ACCESS_CALL.get() == call_signature(tool_name, arguments)
            else (sandbox_mode or self._conversation_sandbox_mode(conversation_id, auth))
        )
        context = self.build_tool_context(
            auth=auth,
            conversation_id=conversation_id,
            web_provider=web_provider,
            web_candidate=web_candidate,
            web_search_options=web_search_options,
            mcp_bindings=mcp_bindings,
            mcp_budget=mcp_budget,
            request=request,
            sandbox_mode=effective_sandbox,
            workspace_root=self._conversation_workspace_root(conversation_id, auth),
            run_id=run_id,
            skill_scope=skill_scope,
            progress_callback=progress_callback,
            activity_callback=activity_callback,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )
        return await dispatch_native_tool(tool_name, arguments, context)

    def _loop_guard_refusal(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        conversation_id: uuid.UUID,
        record: bool = True,
    ) -> dict[str, Any] | None:
        """Ask the loop guard whether one tool call may run.

        The guard stops an agent that calls a write tool with the same
        arguments over and over, or that runs past its turn budget. Bounded
        coordination reads may repeat while the team changes. A refused call
        is reported with a typed code rather than hanging.

        Args:
            tool_name: The tool the model called.
            arguments: The tool's arguments.
            conversation_id: The conversation the call belongs to.

        Returns:
            The refusal, or None when the call may run.
        """
        settings = getattr(self, "settings", None)
        if settings is None:
            return None
        if not bool(getattr(settings, "deepspace_loop_guard_enabled", True)):
            return None
        guard = loop_guard_registry().get(
            conversation_id,
            LoopGuardLimits.from_settings(settings),
        )
        trip = guard.check(
            tool_name,
            arguments,
            enforce_repetition_limits=tool_name not in REPEATABLE_COORDINATION_READ_TOOLS,
        )
        if trip is not None:
            return guard_result(trip)
        if record:
            guard.record(
                tool_name,
                arguments,
                enforce_repetition_limits=tool_name not in REPEATABLE_COORDINATION_READ_TOOLS,
            )
        return None

    def _loop_guard_snapshot(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        """Capture bounded guard state for checkpoints of an active run."""
        if not bool(getattr(self.settings, "deepspace_loop_guard_enabled", True)):
            return None
        return (
            loop_guard_registry()
            .get(
                conversation_id,
                LoopGuardLimits.from_settings(self.settings),
            )
            .snapshot()
        )

    def _coordination_read_no_progress(self, state: _TurnState) -> bool:
        """Record a coordination observation and detect an unchanged poll loop.

        Repetition by itself is not unsafe for team-state reads: a teammate can
        change between two identical-looking calls. The per-tool fingerprint
        therefore resets the streak when the returned observation changes, and
        only stops a turn after the same observation has been returned enough
        times without an intervening state change.

        Args:
            state: The current turn state containing the completed read result.

        Returns:
            ``True`` when the unchanged-read limit has been reached.
        """
        tool_name = str(state.tool_name or "")
        if tool_name not in COORDINATION_PROGRESS_READ_TOOLS:
            return False
        fingerprints = state.coordination_read_fingerprints
        streaks = state.coordination_read_streaks
        if not isinstance(fingerprints, dict):
            fingerprints = {}
            state.coordination_read_fingerprints = fingerprints
        if not isinstance(streaks, dict):
            streaks = {}
            state.coordination_read_streaks = streaks
        observed_payload = state.tool_payload
        if tool_name in COORDINATION_PROGRESS_READ_TOOLS and isinstance(observed_payload, dict):
            # Older child-roster providers exposed the parent conversation
            # event cursor as ``revision``. Tool-call/result events increment
            # that cursor, so including it would turn an unchanged read into
            # apparent progress and allow an endless model polling loop.
            observed_payload = {
                key: value for key, value in observed_payload.items() if key != "revision"
            }
        fingerprint = hashlib.sha256(
            json.dumps(
                observed_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if fingerprints.get(tool_name) == fingerprint:
            streak = int(streaks.get(tool_name, 0)) + 1
        else:
            streak = 1
        fingerprints[tool_name] = fingerprint
        streaks[tool_name] = streak
        try:
            configured_limit = int(
                getattr(
                    self.settings,
                    "deepspace_coordination_read_no_progress_limit",
                    MAX_COORDINATION_READS_WITHOUT_PROGRESS_PER_TOOL,
                )
            )
        except (TypeError, ValueError):
            configured_limit = MAX_COORDINATION_READS_WITHOUT_PROGRESS_PER_TOOL
        return streak >= max(1, configured_limit)

    @staticmethod
    def _image_read_unavailable(
        requested_url: str,
        detail: str,
        *,
        retrieval_status: str,
    ) -> dict[str, Any]:
        return {
            "url": requested_url,
            "content_type": None,
            "format": None,
            "width": None,
            "height": None,
            "bytes": 0,
            "retrieval_status": retrieval_status,
            "error_category": "source_unavailable",
            "message": (
                "The requested public image was not inspected. "
                f"{detail} Continue with another verified image URL; do not claim visual details."
            ),
            "citations": [
                {
                    "title": "Image unavailable",
                    "url": requested_url,
                    "snippet": "The URL was not verified as a readable image.",
                    "source": "web_fetch",
                    "retrieval_status": retrieval_status,
                }
            ],
        }

    def _tool_call_budget(self, loop_deadline: float) -> float:
        """Return the seconds one tool call may take.

        The deployment's tool budget owns the deadline, and the turn's own
        remaining time bounds it, so a call never runs past the turn and
        never runs longer than the deployment allows.  A call that declares
        a deadline of its own keeps it, because the pipeline applies that
        deadline first.

        Args:
            loop_deadline: The turn's deadline, as a monotonic time.

        Returns:
            The budget in seconds.
        """
        settings = getattr(self, "settings", None)
        configured = float(
            getattr(
                settings,
                "deepspace_tool_timeout_seconds",
                DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS,
            )
        )
        remaining = loop_deadline - time.monotonic()
        return max(MIN_TOOL_CALL_SECONDS, min(configured, max(MIN_TOOL_CALL_SECONDS, remaining)))

    @staticmethod
    def _normalize_tool_error_category(category: Any, error_code: Any, error: Any) -> str:
        """Normalize a tool failure without turning infrastructure into tool feedback."""

        normalized_category = str(category or "tool").strip().casefold()
        normalized_code = str(error_code or "").strip().upper()
        message = " ".join(str(error or "").split()).casefold()
        if normalized_code == "SHARED_EXECUTION_BUSY":
            return "capacity"
        if normalized_code == "SHARED_EXECUTION_UNAVAILABLE":
            return "runtime"
        if normalized_code == "TOOL_TIMEOUT":
            return "timeout"
        if normalized_category == "policy" and "database check failed" in message:
            return "runtime"
        if normalized_category == "policy" and any(
            marker in message
            for marker in ("authorization failed", "tenant scope", "tenant isolation")
        ):
            return "authorization"
        if normalized_category in FATAL_TOOL_ERROR_CATEGORIES:
            return normalized_category
        if normalized_category in RECOVERABLE_TOOL_ERROR_CATEGORIES:
            return normalized_category
        if any(
            marker in message
            for marker in (
                "permission denied",
                "access denied",
                "operation not permitted",
                "eacces",
                "permissionerror",
            )
        ):
            return "permission_denied"
        if any(marker in message for marker in ("no such file", "file not found", "not found")):
            return "not_found"
        if any(marker in message for marker in ("timed out", "timeout", "time limit")):
            return "timeout"
        if any(
            marker in message
            for marker in ("blocked by policy", "denied by policy", "policy blocked")
        ):
            return "policy"
        if normalized_category in {"source_unavailable", "blocked", "policy", "capacity"}:
            return normalized_category
        return normalized_category or "tool"

    @classmethod
    def _tool_failure_is_recoverable(
        cls, tool_name: str, *, category: Any, error_code: Any, error: Any
    ) -> bool:
        """Return whether the model may safely receive this failed tool result."""

        normalized_category = cls._normalize_tool_error_category(category, error_code, error)
        normalized_code = str(error_code or "").strip().upper()
        if normalized_code in RECOVERABLE_TOOL_ERROR_CODES:
            return True
        if normalized_category in FATAL_TOOL_ERROR_CATEGORIES:
            return False
        if normalized_category in RECOVERABLE_TOOL_ERROR_CATEGORIES:
            return True
        # Public research readers already expose bounded source failures. Keep
        # their existing recovery behavior while treating an unknown native
        # tool exception as an internal application failure.
        return tool_name in RECOVERABLE_RESEARCH_TOOL_NAMES and normalized_category == "tool"

    @staticmethod
    def _tool_failure_recovery(category: str) -> str:
        """Give the model bounded instructions for a recoverable tool result."""

        if category == "permission_denied":
            return (
                "The operation was not completed because the operating system denied access. "
                "Do not retry the same protected path or claim success. Explain the limitation, "
                "offer an allowed location, or ask the user to run the command and paste its output."
            )
        if category == "capacity":
            return (
                "The tool could not obtain a safe execution slot. Do not repeat the same call "
                "in a loop; explain that active work must finish or answer from the evidence already available."
            )
        if category == "timeout":
            return (
                "The operation did not finish within its bounded time. Do not claim completion; "
                "give a smaller or safer alternative when one is available."
            )
        if category == "policy":
            return (
                "The requested operation was not run because the active policy denied it. "
                "Do not bypass policy or suggest privilege escalation."
            )
        if category == "not_found":
            return (
                "The requested target was not found. Do not claim it was read or changed; "
                "ask for another target or explain how the user can verify it."
            )
        return (
            "The requested operation did not complete. State that clearly and use only the "
            "evidence returned by other successful tools."
        )

    @classmethod
    def _normalize_tool_failure_result(
        cls, tool_name: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a bounded, model-safe representation of a failed tool result."""

        error = " ".join(str(result.get("error") or f"{tool_name} failed safely.").split())
        error = redact_reasoning_text(error)[:1600]
        category = cls._normalize_tool_error_category(
            result.get("error_category"), result.get("error_code"), error
        )
        recoverable = cls._tool_failure_is_recoverable(
            tool_name,
            category=category,
            error_code=result.get("error_code"),
            error=error,
        )
        normalized = {
            **result,
            "success": False,
            "error": error,
            "error_category": category,
            "recoverable": recoverable,
        }
        if recoverable and not normalized.get("recovery"):
            normalized["recovery"] = cls._tool_failure_recovery(category)
        return normalized

    async def _run_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
        loop_deadline: float,
        run_id: uuid.UUID | None,
        turn: int | None = None,
        step: int | None = None,
        read_semaphore: asyncio.Semaphore,
        write_lock: asyncio.Lock,
        mcp_binding: DeepSpaceMCPTool | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        mcp_approval_granted: bool = False,
        assistant_message_id: uuid.UUID | None = None,
        ignore_existing_tasks: bool = False,
        user_prompt: str | None = None,
        native_tool_names: set[str] | None = None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_budget: dict[str, int] | None = None,
        progress_callback: Callable[[str], None] | None = None,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        full_access_approved = APPROVED_FULL_ACCESS_CALL.get() == call_signature(
            tool_name, arguments
        )
        effective_permission_mode = (
            "danger-full-access"
            if full_access_approved
            else self._conversation_sandbox_mode(conversation_id, auth)
        )
        decision: Any
        if tool_name in {MCP_SEARCH_TOOLS, MCP_GET_TOOL_SCHEMA, MCP_GET_RESULT, "get_tool_result"}:
            decision = MCPToolPolicyDecision(
                True,
                mode="always_allow",
                risk_level="read",
                approval_requirement="auto",
                reason="DeepSpace MCP broker metadata operation.",
            )
        else:
            decision = (
                self.mcp_bridge.policy_for_tool(
                    auth=auth,
                    conversation_id=conversation_id,
                    binding=mcp_binding,
                    permission_mode=effective_permission_mode,
                )
                if mcp_binding is not None
                else self.tool_policy.before_tool(tool_name, arguments)
            )
            if (
                not decision.allowed
                and full_access_approved
                and getattr(decision, "mode", None) == "mode_required"
            ):
                decision = ToolPolicyDecision(
                    True,
                    mode=decision.mode,
                    risk_level="high",
                    reason=decision.reason,
                )
        if not decision.allowed:
            if getattr(decision, "mode", None) == "mode_required" and not full_access_approved:
                reason_text = (
                    decision.reason or "Switch to Full access before asking DeepSpace to use it."
                )
                return self._normalize_tool_failure_result(
                    tool_name,
                    {
                        "success": False,
                        "error": reason_text,
                        "error_category": "policy",
                        "error_code": "MODE_REQUIRED",
                        "payload": {
                            "status": "mode_required",
                            "required_mode": "danger-full-access",
                            "message": reason_text,
                        },
                    },
                )
            return self._normalize_tool_failure_result(
                tool_name,
                {
                    "success": False,
                    "error": decision.reason or "Tool blocked by policy.",
                    "error_category": "policy",
                    "error_code": "TOOL_BLOCKED_BY_POLICY",
                },
            )

        # The typed log records every tool call, whatever the outcome, so a
        # projection can rebuild the timeline without reading the frames.
        self._record_session_event(
            auth=auth,
            conversation_id=conversation_id,
            run_id=run_id,
            turn=turn,
            step=step,
            event_type=TOOL_CALL,
            tool_name=tool_name,
            payload={"arguments": summarize_tool_arguments(arguments)},
        )

        gate: AbstractAsyncContextManager[Any]
        is_shared_read = (
            getattr(decision, "mode", None) == "read"
            or getattr(decision, "risk_level", None) == "read"
        )
        is_waiting_for_child = (
            tool_name == "get_agent"
            and isinstance(arguments, dict)
            and int(arguments.get("wait_timeout_seconds") or 0) > 0
        )
        if tool_name in CHILD_TURN_TOOL_NAMES or is_waiting_for_child:
            # The delegated child's turn runs in the child's own context, so
            # it does not hold the parent's write lock while it works. A
            # bounded get_agent wait also cannot hold a lease the child needs.
            gate = _unlocked_gate()
        elif is_shared_read:
            gate = read_semaphore
        else:
            gate = write_lock
        async with conversation_execution_gate(
            gate,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            is_read=is_shared_read,
            max_parallel_reads=int(getattr(self.settings, "deepspace_max_concurrent_tools", 8)),
            timeout_seconds=self._tool_call_budget(loop_deadline),
            shared=not is_waiting_for_child,
        ) as capacity_error:
            if capacity_error is not None:
                return self._normalize_tool_failure_result(
                    tool_name,
                    {
                        "success": False,
                        "error": str(capacity_error),
                        "error_category": "capacity",
                        "error_code": capacity_error.code,
                    },
                )
            for attempt in range(MAX_TOOL_RETRIES + 1):
                if time.monotonic() >= loop_deadline:
                    return self._normalize_tool_failure_result(
                        tool_name,
                        {
                            "success": False,
                            "error": "Tool execution stopped by the runtime policy timeout.",
                            "error_category": "timeout",
                            "error_code": "TOOL_TIMEOUT",
                        },
                    )
                if run_id is not None and self.runtime.is_cancel_requested(run_id=run_id):
                    return self._normalize_tool_failure_result(
                        tool_name,
                        {
                            "success": False,
                            "error": "Tool execution cancelled by the user.",
                            "error_category": "cancelled",
                            "error_code": "TOOL_CANCELLED",
                        },
                    )
                try:

                    async def body() -> Any:
                        return await self._execute_productivity_tool(
                            tool_name=tool_name,
                            arguments=arguments,
                            auth=auth,
                            conversation_id=conversation_id,
                            web_provider=web_provider,
                            web_candidate=web_candidate,
                            web_search_options=web_search_options,
                            request=request,
                            mcp_binding=mcp_binding,
                            mcp_bindings=mcp_bindings,
                            mcp_approval_granted=mcp_approval_granted,
                            assistant_message_id=assistant_message_id,
                            ignore_existing_tasks=ignore_existing_tasks,
                            user_prompt=user_prompt,
                            native_tool_names=native_tool_names,
                            mcp_budget=mcp_budget,
                            run_id=run_id,
                            turn=turn,
                            step=step,
                            progress_callback=progress_callback,
                            activity_callback=activity_callback,
                            thinking_enabled=thinking_enabled,
                            reasoning_effort=reasoning_effort,
                        )

                    outcome = await self.tool_pipeline.run(
                        tool_name=tool_name,
                        arguments=arguments,
                        body=body,
                        timeout_seconds=self._tool_call_budget(loop_deadline),
                    )
                    if not outcome.success:
                        self._record_session_event(
                            auth=auth,
                            conversation_id=conversation_id,
                            run_id=run_id,
                            turn=turn,
                            step=step,
                            event_type=TOOL_RESULT,
                            tool_name=tool_name,
                            payload={
                                "status": "failed",
                                "error": outcome.error,
                                "stages": list(outcome.stages),
                            },
                        )
                        failure = {
                            "success": False,
                            "error": outcome.error or f"{tool_name} failed safely.",
                            "error_category": outcome.error_category or "tool",
                        }
                        if outcome.error_code:
                            failure["error_code"] = outcome.error_code
                        if outcome.requires_approval:
                            # A hook asked about the call instead of
                            # refusing it.  The call did not run, and the
                            # turn hands it to the approval flow.
                            failure["requires_approval"] = True
                            failure["approval_reason"] = outcome.error
                        return self._normalize_tool_failure_result(tool_name, failure)
                    self._record_session_event(
                        auth=auth,
                        conversation_id=conversation_id,
                        run_id=run_id,
                        turn=turn,
                        step=step,
                        event_type=TOOL_RESULT,
                        tool_name=tool_name,
                        payload={
                            "status": "completed",
                            "duration_ms": outcome.duration_ms,
                            "stages": list(outcome.stages),
                        },
                    )
                    return {"success": True, "payload": outcome.payload}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DeepSpace tool failed: %s", tool_name, exc_info=True)
                    # A failed SQLAlchemy write leaves the session in a
                    # pending-rollback state. Recover before retrying or
                    # checking the task ledger so one tool failure cannot
                    # terminate the whole SSE stream.
                    rollback = getattr(self.db, "rollback", None)
                    if callable(rollback):
                        try:
                            rollback()
                        except Exception:  # noqa: BLE001
                            logger.warning("DeepSpace tool rollback failed", exc_info=True)
                    if attempt >= MAX_TOOL_RETRIES:
                        result: dict[str, Any] = {
                            "success": False,
                            "error": f"{tool_name} failed safely: {exc}",
                            "error_category": "tool",
                        }
                        if tool_name == "web_fetch":
                            result["recovery"] = (
                                "The URL could not be read safely. Continue with web_search "
                                "using the URL host/title, or ask the user for another source; "
                                "do not retry the same blocked URL repeatedly."
                            )
                        self._record_session_event(
                            auth=auth,
                            conversation_id=conversation_id,
                            run_id=run_id,
                            turn=turn,
                            step=step,
                            event_type=TOOL_RESULT,
                            tool_name=tool_name,
                            payload={"status": "failed", "error": str(result.get("error"))[:500]},
                        )
                        return self._normalize_tool_failure_result(tool_name, result)
        return self._normalize_tool_failure_result(
            tool_name,
            {"success": False, "error": f"{tool_name} failed safely.", "error_category": "tool"},
        )

    @staticmethod
    def _domain_allowed_by_config(domain: str, configured: object) -> bool:
        if not isinstance(configured, list) or not configured:
            return True
        host = domain.lower().lstrip(".")
        return any(
            isinstance(item, str)
            and (
                host == item.strip().lower().lstrip(".")
                or host.endswith(f".{item.strip().lower().lstrip('.')}")
            )
            for item in configured
        )

    async def _run_web_search(
        self,
        *,
        provider: Any,
        candidate: Any,
        arguments: dict[str, Any],
        auth: AuthContext,
        request: Any | None,
    ) -> tuple[WebSearchResponse, dict[str, Any]]:
        query = str(arguments.get("query") or "").strip()[:512]
        max_results_raw = arguments.get("max_results", 5)
        try:
            max_results = max(1, min(int(max_results_raw), 10))
        except (TypeError, ValueError):
            max_results = 5
        configured_allowed = candidate.metadata.get("allowed_domains")
        requested_domains = arguments.get("domains")
        if isinstance(requested_domains, list) and requested_domains:
            allowed_domains = [
                str(item).strip().lower()
                for item in requested_domains
                if isinstance(item, str)
                and str(item).strip()
                and self._domain_allowed_by_config(str(item), configured_allowed)
            ][:20]
        else:
            allowed_domains = configured_allowed
        metadata: dict[str, Any] = {
            **dict(candidate.metadata),
            "allowed_domains": allowed_domains,
            "time_range": arguments.get("time_range"),
            "current_date": datetime.now(UTC).date().isoformat(),
            "date_policy": "Prefer results published within the requested time range and verify publication dates.",
            "tenant_id": str(auth.tenant_id),
            "user_id": str(auth.user_id),
        }
        if request is not None:
            metadata["rate_limit_request"] = request
        search_request = WebSearchRequest(
            query=query,
            max_results=max_results,
            timeout_seconds=15,
            include_answer=False,
            include_raw_content=False,
            provider_name=candidate.provider_type,
            metadata=metadata,
        )
        response = await asyncio.to_thread(provider.search, search_request)
        citations = [
            {
                "id": index,
                "title": item.title,
                "url": item.url,
                "snippet": item.content[:800],
                "favicon": item.favicon,
                "published_date": item.published_date,
                "source": item.source,
            }
            for index, item in enumerate(response.results, start=1)
        ]
        return response, {
            "query": response.query,
            "provider": candidate.provider_type,
            "citations": citations,
            "results": citations,
        }

    @staticmethod
    def _append_citations(answer: str, citations: list[dict[str, Any]]) -> str:
        if not citations:
            return answer
        lines = ["", "### Sources"]
        for item in citations[:8]:
            title = str(item.get("title") or "Source").replace("[", "(").replace("]", ")")
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                status = str(item.get("retrieval_status") or "")
                note = {
                    "read_full": " — read in full",
                    "search_snippet_only": " — search snippet only",
                    "blocked": " — blocked; not read",
                    "unavailable": " — unavailable; not read",
                }.get(status, "")
                lines.append(f"[R{item.get('id', '?')}] [{title}]({url}){note}")
        return answer.rstrip() + "\n" + "\n".join(lines) if len(lines) > 2 else answer

    @staticmethod
    def _native_research_summary(stats: dict[str, int]) -> str:
        """Describe completed native research calls without executing anything."""
        parts: list[str] = []
        for key, singular, plural in (
            ("searches", "search", "searches"),
            ("results", "result considered", "results considered"),
            ("pages", "page fetched", "pages fetched"),
            ("blocked_pages", "page blocked", "pages blocked"),
            ("images", "image inspected", "images inspected"),
        ):
            count = stats.get(key, 0)
            if count:
                label = singular if count == 1 else plural
                parts.append(f"{count} {label}")
        if not parts:
            return ""
        return f"> *Native web research · {' · '.join(parts)}*"

    @staticmethod
    def _is_independent_native_read_call(
        item: dict[str, Any],
        *,
        mcp_bindings: dict[str, DeepSpaceMCPTool],
    ) -> bool:
        """Identify native reads, including strictly allowlisted shell queries."""
        name = str(item.get("tool_name") or "")
        if (
            name == "discover_tools"
            or mcp_bindings.get(name) is not None
            or not NATIVE_TOOL_REGISTRY.contains(name)
        ):
            return False
        arguments = item.get("arguments")
        if (
            name == "get_agent"
            and isinstance(arguments, dict)
            and int(arguments.get("wait_timeout_seconds") or 0) > 0
        ):
            # Waiting is a scheduling barrier, not an independent read batch.
            return False
        spec = NATIVE_TOOL_REGISTRY.spec(name)
        if spec.mode == "read":
            return True
        if name not in {"bash", "pwsh"}:
            return False
        if not isinstance(arguments, dict):
            return False
        command = str(arguments.get("command") or "")
        return is_known_read_only_command(command) and not tool_requires_human_confirmation(
            name,
            arguments,
            permission_mode="danger-full-access",
        )

    @staticmethod
    def _is_parallel_safe_call(
        item: dict[str, Any],
        *,
        mcp_bindings: dict[str, DeepSpaceMCPTool],
    ) -> bool:
        """Identify calls eligible to join a parallel execution batch."""
        service_cls = _pkg_attr("DeepSpaceChatService", None)
        if service_cls._is_independent_native_read_call(item, mcp_bindings=mcp_bindings):
            return True
        name = str(item.get("tool_name") or "")
        arguments = item.get("arguments")
        if (
            name == "get_agent"
            and isinstance(arguments, dict)
            and int(arguments.get("wait_timeout_seconds") or 0) > 0
        ):
            return False
        if not NATIVE_TOOL_REGISTRY.contains(name):
            return False
        return NATIVE_TOOL_REGISTRY.spec(name).concurrency == "read_parallel"

    @staticmethod
    def _plan_tool_execution_batches(
        valid_calls: list[dict[str, Any]],
        *,
        mcp_bindings: dict[str, DeepSpaceMCPTool],
    ) -> list[ExecutionBatch]:
        """Keep batch policy outside the stream orchestration loop."""

        return plan_execution_batches(
            valid_calls,
            is_read=lambda item: _pkg_attr("DeepSpaceChatService", None)._is_parallel_safe_call(
                item,
                mcp_bindings=mcp_bindings,
            ),
        )

    @staticmethod
    def _is_parallel_managed_work_batch(
        calls: Sequence[dict[str, Any]],
        *,
        stage: str,
        mcp_bindings: dict[str, DeepSpaceMCPTool],
        max_calls: int,
    ) -> bool:
        """Allow only bounded, native read batches while a task is being worked.

        A persisted task's lifecycle mutations must remain one-at-a-time so the
        ledger is an accurate record of the work. The work itself can include
        independent research and workspace reads. Those calls already use the
        execution batcher's semaphore and result-order preservation, but this
        separate admission check prevents discovery, MCP, and every write from
        sharing a managed-task model turn.
        """

        if stage != "work" or len(calls) < 2 or len(calls) > max_calls:
            return False
        service_cls = _pkg_attr("DeepSpaceChatService", None)
        for call in calls:
            tool_name = service_cls._tool_name(call)
            arguments = service_cls._parse_tool_arguments(call)
            item = {
                "tool_name": tool_name,
                "arguments": arguments or {},
            }
            if not service_cls._is_independent_native_read_call(
                item,
                mcp_bindings=mcp_bindings,
            ):
                return False
        return True

    def _hook_approval_request(self, state: _TurnState) -> dict[str, Any]:
        """Build the approval request a hook's question produces.

        A hook that asks about a call does not refuse it: the call did not
        run, and the user decides whether it may.  The request carries the
        hook's own question, so the user sees why the agent is waiting.

        Args:
            state: The turn's state.

        Returns:
            The approval request, in the shape the approval flow reads.
        """
        reason = str(
            state.result.get("approval_reason")
            or state.result.get("error")
            or "A hook asked to confirm this tool call."
        )
        binding = state.mcp_bindings.get(state.tool_name)
        return {
            "approval_id": str(uuid.uuid4()),
            "call_id": state.call_id,
            "turn_index": state.round_index,
            "step_id": state.step_id,
            "tool_name": state.tool_name,
            "mcp_server": binding.server.name if binding is not None else None,
            "mcp_tool": binding.raw_name if binding is not None else None,
            "tool_input": state.item["arguments"],
            "risk_level": state.risk_level,
            "permission_level": "human",
            "message": reason,
            "decision": "pending",
            "requested_at": self._now(),
        }

    async def _run_approved_tool_call(
        self,
        *,
        state: _TurnState,
        tool_name: str,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
        loop_deadline: float,
        run_id: uuid.UUID | None,
        mcp_binding: DeepSpaceMCPTool | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        assistant_message_id: uuid.UUID | None = None,
        user_prompt: str | None = None,
        native_tool_names: set[str] | None = None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_budget: dict[str, int] | None = None,
        progress_callback: Callable[[str], None] | None = None,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Run a call the user approved.

        The hooks are not asked about the call again: the user has already
        answered for it, and an approval that asked again would never end.

        Args:
            state: The turn's state.
            tool_name: The tool the model called.
            arguments: The validated arguments.
            auth: The caller's identity.
            conversation_id: The conversation the call belongs to.
            web_provider: The web provider, when the turn has one.
            web_candidate: The provider candidate, when the turn has one.
            request: The request, when the turn has one.
            loop_deadline: The turn's deadline.
            run_id: The run the call belongs to.
            mcp_binding: The connected service the call belongs to.
            mcp_bindings: Every connected service the turn knows.
            assistant_message_id: The assistant message the call belongs to.
            user_prompt: The prompt the turn answers.
            native_tool_names: The native tools the turn offers.
            web_search_options: The web providers available to this turn.
            mcp_budget: The shared MCP call and result budgets for this turn.

        Returns:
            The call's result.
        """
        signature = call_signature(tool_name, arguments)
        token = APPROVED_CALL.set(signature)
        resumed_pending = getattr(state, "resumed_pending", None)
        full_access_token = (
            APPROVED_FULL_ACCESS_CALL.set(signature)
            if isinstance(resumed_pending, dict)
            and resumed_pending.get("required_mode") == "danger-full-access"
            else None
        )
        try:
            return await self._run_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                request=request,
                loop_deadline=loop_deadline,
                run_id=run_id,
                turn=state.turn_number,
                step=state.round_index,
                read_semaphore=asyncio.Semaphore(1),
                write_lock=asyncio.Lock(),
                mcp_binding=mcp_binding,
                mcp_bindings=mcp_bindings,
                mcp_approval_granted=True,
                web_search_options=(
                    web_search_options
                    if web_search_options is not None
                    else state.web_search_options
                ),
                assistant_message_id=assistant_message_id,
                user_prompt=user_prompt,
                native_tool_names=native_tool_names,
                mcp_budget=mcp_budget if mcp_budget is not None else state.mcp_budget,
                progress_callback=progress_callback,
                activity_callback=activity_callback,
            )
        finally:
            if full_access_token is not None:
                APPROVED_FULL_ACCESS_CALL.reset(full_access_token)
            APPROVED_CALL.reset(token)

    async def _auto_review_tool_call(
        self,
        state: _TurnState,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Ask the turn's model whether a tool may skip human approval.

        Args:
            state: The current turn, whose selected provider answers.
            tool_name: The tool awaiting approval.
            arguments: The validated arguments.

        Returns:
            A ``ReviewDecision``, or ``None`` when auto-review is disabled or
            the turn has no bound provider.
        """
        if not bool(getattr(self.settings, "deepspace_auto_review_enabled", False)):
            return None
        candidate = state.candidate
        provider = state.provider
        if provider is None or candidate is None:
            return None

        async def review(system_prompt: str, user_prompt: str) -> str:
            """Call the turn's provider once with the reviewer prompt."""
            request = ChatGenerateRequest(
                model=str(candidate.model_name),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=300,
                base_url=str(candidate.base_url or ""),
                api_key=candidate.api_key,
                stream=False,
            )
            response = await asyncio.to_thread(provider.generate, request)
            if not isinstance(response.content, str):
                raise TypeError("The provider returned a non-text review response.")
            return response.content

        return await AutoReviewEngine(self.settings, reviewer=review).review(
            tool_name,
            arguments,
            {
                "conversation_id": str(state.conversation_id),
                "round": state.round_index,
                "risk_level": state.risk_level,
            },
        )
