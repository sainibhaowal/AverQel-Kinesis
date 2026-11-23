"""DeepSpace Chat Service package."""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import base64
import copy
import difflib
import hashlib
import itertools
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import redis.asyncio as aioredis
from sqlalchemy import select

from app.auth.dependencies import AuthContext
from app.core.config import DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS, Settings
from app.deepspace.memory.memory_service import MemoryService
from app.deepspace.models.context_epoch import DeepSpaceContextEpoch
from app.deepspace.models.request_metric import DeepSpaceRequestMetric
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.repositories.context_summaries import DeepSpaceContextSummaryRepository
from app.deepspace.repositories.request_metrics import DeepSpaceRequestMetricsRepository
from app.deepspace.services.chat_service.core import DeepSpaceChatService
from app.deepspace.services.chat_service.utils import (
    _FAKE_TOOL_MARKER_RE,
    _PRIVATE_PROVIDER_CONTINUATIONS_KEY,
    ANALYZE_TOOL,
    ASK_USER_TOOL,
    ATTACHMENT_VISION_MIME_TYPES,
    CONTEXT_AUTO_COMPACT_THRESHOLD,
    CONTEXT_COMPACT_THRESHOLD,
    CONTEXT_EMERGENCY_THRESHOLD,
    CONTEXT_WATCH_THRESHOLD,
    DEEPSPACE_AGENT_POLICY,
    DEEPSPACE_COMPACT_POLICY,
    DEEPSPACE_PROMPT_BASELINE_VERSION,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    FATAL_TOOL_ERROR_CATEGORIES,
    GET_TOOL_RESULT_TOOL,
    IMAGE_READ_TOOL,
    MAX_ATTACHMENT_OCR_CONTEXT_CHARS,
    MAX_ATTACHMENT_OCR_TEXT_CHARS,
    MAX_ATTACHMENT_VISION_BYTES,
    MAX_ATTACHMENT_VISION_IMAGES,
    MAX_COMPACT_HISTORY_CHARS,
    MAX_CONNECTED_TOOL_RECOVERY_RETRIES,
    MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL,
    MAX_COORDINATION_READS_WITHOUT_PROGRESS_PER_TOOL,
    MAX_DEADLINE_CONTINUATIONS,
    MAX_EMPTY_PROVIDER_RETRIES,
    MAX_NATIVE_DISCOVERY_ATTEMPTS_PER_TURN,
    MAX_POLICY_DENIALS_PER_TURN,
    MAX_PROVIDER_STREAM_RETRIES,
    MAX_RAW_HISTORY_MESSAGES,
    MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN,
    MAX_TOOL_RETRIES,
    MAX_WAIT_AGENT_ATTEMPTS_PER_TURN,
    MAX_WAIT_AGENT_TOTAL_SECONDS_PER_TURN,
    MIN_TOOL_CALL_SECONDS,
    NATIVE_TOOL_REGISTRY,
    RECOVERABLE_RESEARCH_TOOL_NAMES,
    RECOVERABLE_TOOL_ERROR_CATEGORIES,
    RECOVERABLE_TOOL_ERROR_CODES,
    SANDBOX_EXECUTE_TOOL,
    TODO_WRITE_EVIDENCE_COMPACTION_MARKER,
    TODO_WRITE_EVIDENCE_MAX_CHARS,
    TODO_WRITE_TOOL,
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    DeepSpaceEmptyResponseError,
    DeepSpaceToolExecutionError,
    ProviderContinuationPersistenceError,
    _cached_native_tool_schema_json,
    _capability_failure_message,
    _epoch_seconds,
    _mark_provider_output_event,
    _NoRetryProviderRequestError,
    _normalize_reasoning_effort,
    _pkg_attr,
    _reasoning_mode_for_turn,
    _relabel_citations,
    _request_run_id,
    _summarize_tool_argument_value,
    _TimedProviderItem,
    _TurnState,
    _unlocked_gate,
    sse,
    summarize_tool_arguments,
)
from app.deepspace.services.compaction_pipeline import compact_request
from app.deepspace.services.context_budget import AdaptiveContextBudget
from app.deepspace.services.context_cache import DeepSpaceContextCache
from app.deepspace.services.context_epoch import DeepSpaceContextEpochStore
from app.deepspace.services.conversation_execution_limiter import (
    SharedExecutionLimitError,
    conversation_execution_gate,
    shared_conversation_execution_slot,
)
from app.deepspace.services.conversation_retrieval import ConversationRetrievalService
from app.deepspace.services.deliverables import record_turn_changes
from app.deepspace.services.deployment_persona import deployment_persona_section
from app.deepspace.services.execution_batches import ExecutionBatch, plan_execution_batches
from app.deepspace.services.extensions import (
    ExtensionSet,
    alias_definition,
    extensions_for,
    resolve_alias,
)
from app.deepspace.services.goal_system_prompt import (
    blocked_threshold_from_settings,
    goal_system_prompt,
)
from app.deepspace.services.hook_gate import (
    APPROVED_CALL,
    HookParticipant,
    call_signature,
    hook_audit_recorder,
)
from app.deepspace.services.hooks import hook_registry
from app.deepspace.services.host_access import (
    is_known_read_only_command,
    tool_confirmation_warning,
    tool_requires_human_confirmation,
)
from app.deepspace.services.library_storage import safe_extraction_status
from app.deepspace.services.loop_guard import (
    COORDINATION_PROGRESS_READ_TOOLS,
    REPEATABLE_COORDINATION_READ_TOOLS,
    LoopGuardLimits,
    guard_result,
    loop_guard_registry,
)
from app.deepspace.services.mcp_bridge import DeepSpaceMCPBridge, DeepSpaceMCPTool
from app.deepspace.services.mcp_connection_intent import (
    has_matching_connected_mcp,
    is_explicit_mcp_action_request,
    missing_stdio_mcp_options,
)
from app.deepspace.services.mcp_result_store import MCPResultStore
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL,
    MCP_BROKER_TOOL_NAMES,
    MCP_CALL_TOOL,
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
    MCPToolBroker,
)
from app.deepspace.services.media_artifacts import DeepSpaceMediaArtifactService
from app.deepspace.services.model_selection import ModelSelectionService
from app.deepspace.services.native_tool_broker import (
    NativeToolBroker,
)
from app.deepspace.services.permission_presets import AutoReviewEngine
from app.deepspace.services.plan_mode import (
    PLAN_MODE_INSTRUCTION,
    PlanModeService,
)
from app.deepspace.services.plugins import (
    PluginStateService,
    conversation_plugin_scope,
    plugin_guidance,
    plugin_registry,
)
from app.deepspace.services.presets import (
    PresetResolution,
    all_tool_names,
    effective_preset_resolution,
    resolve_preset,
    strictest_sandbox_mode,
)
from app.deepspace.services.provider_circuit import DeepSpaceProviderCircuit
from app.deepspace.services.reasoning_privacy import (
    REASONING_REDACTION_VERSION,
    redact_reasoning_text,
)
from app.deepspace.services.repeat_tool_reminder import repeat_tool_reminder_registry
from app.deepspace.services.run_events import cancellation_key
from app.deepspace.services.runtime_invariants import (
    InvariantContext,
    get_invariant_registry,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.runtime_store import DeepSpaceRuntimeStore
from app.deepspace.services.runtime_transitions import RuntimePhase, transition_checkpoint
from app.deepspace.services.sandbox_executor import SandboxExecutorError, execute_sandbox
from app.deepspace.services.sandbox_mode import SandboxModeService
from app.deepspace.services.session_events import (
    ASSISTANT_MESSAGE,
    STEP_END,
    STEP_START,
    TODO_WRITE,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    SessionEventStore,
)
from app.deepspace.services.session_telemetry import SpanHandle, get_session_telemetry
from app.deepspace.services.spill_policy import take_recovered_images
from app.deepspace.services.spill_store import SpillRef, SpillStoreError, spill_result
from app.deepspace.services.subagent_runner import CHILD_TURN_TOOL_NAMES
from app.deepspace.services.task_loop import (
    DeepSpaceTaskLoopStore,
    normalize_harness_todos,
    summarize_tasks,
    todo_counts,
)
from app.deepspace.services.time_context import (
    TimeContextService,
    parse_browser_time_zone,
)
from app.deepspace.services.todo_localization import (
    request_locale,
    todo_summary,
)
from app.deepspace.services.tool_pipeline import DeepSpaceToolPipeline
from app.deepspace.services.tool_registry import (
    NativeToolValidationError,
    ToolRegistry,
    build_native_tool_registry,
)
from app.deepspace.services.tool_requirements import (
    excludes_mcp_for_prompt,
    requires_connected_service_action,
    requires_tool_execution,
)
from app.deepspace.services.tool_result_store import ToolResultStore
from app.deepspace.services.tools import (
    NATIVE_TOOL_DEADLINES,
    NATIVE_TOOL_DEFINITIONS,
    ToolContext,
    dispatch_native_tool,
    unavailable_tool_names,
)
from app.deepspace.services.tools.workflow import WORKFLOW_SYSTEM_GUIDANCE
from app.deepspace.services.workspace_instructions import (
    prepare_instruction_message,
    prepare_skill_catalog_message,
)
from app.deepspace.services.workspace_root import (
    WorkspaceUnavailableError,
    workspace_root_for,
)
from app.deepspace.services.workspace_snapshot import (
    collect_snapshot_files,
    snapshot_store,
)
from app.deepspace.services.workspaces import WorkspaceSelectionService
from app.documents.repositories.chunks import RetrievedChunkRow
from app.ingestion.services.embedding_service import EmbeddingService
from app.integrations.services.mcp_runtime import MCPToolPolicyDecision
from app.providers.services import ChatGenerateRequest, ProviderRegistry
from app.providers.services.base import ProviderRequestError
from app.providers.services.image_inputs import SUPPORTED_IMAGE_MIME_TYPES
from app.providers.services.openai_codex_config import codex_supported_reasoning_efforts
from app.providers.services.provider_secret_crypto import (
    ProviderSecretCrypto,
    ProviderSecretCryptoError,
)
from app.providers.services.reasoning_capabilities import (
    is_deepseek_v4_model,
    merge_reasoning_capabilities,
    normalize_reasoning_effort_value,
    reasoning_capabilities,
    supports_required_tool_choice,
    uses_deepseek_reasoning_protocol,
)
from app.providers.services.selection_service import ProviderSelectionService
from app.providers.services.tool_definitions import unique_tool_definitions
from app.providers.services.types import WebSearchRequest, WebSearchResponse
from app.providers.services.usage_normalizer import normalize_chat_usage
from app.query.services.reranker_service import RerankerService
from app.query.services.visual_blocks import normalize_visual_markdown
from app.realtime.event_bus import publish_event_sync
from app.system.services.cache_service import get_redis_client
from app.system.services.metrics_service import (
    DEEPSPACE_BUDGET_ALLOCATIONS_TOTAL,
    DEEPSPACE_CACHE_USAGE_TOTAL,
    DEEPSPACE_CANARY_MISMATCHES_TOTAL,
    DEEPSPACE_CONTEXT_COMPACTIONS_TOTAL,
    DEEPSPACE_CONTEXT_OVERFLOW_PREVENTED_TOTAL,
    DEEPSPACE_EXECUTION_BATCHES_TOTAL,
    DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL,
    DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL,
    _safe_label,
)
from app.system.services.rate_limit_service import RateLimitService
from app.system.services.storage_quota import StorageQuotaService, ensure_capacity_if_supported
from app.system.services.storage_service import StorageService, StorageServiceError

__all__ = [
    "ANALYZE_TOOL",
    "APPROVED_CALL",
    "ASK_USER_TOOL",
    "ASSISTANT_MESSAGE",
    "ATTACHMENT_VISION_MIME_TYPES",
    "AbstractAsyncContextManager",
    "AdaptiveContextBudget",
    "Any",
    "AsyncIterator",
    "AuthContext",
    "AutoReviewEngine",
    "CHILD_TURN_TOOL_NAMES",
    "CONTEXT_AUTO_COMPACT_THRESHOLD",
    "CONTEXT_COMPACT_THRESHOLD",
    "CONTEXT_EMERGENCY_THRESHOLD",
    "CONTEXT_WATCH_THRESHOLD",
    "COORDINATION_PROGRESS_READ_TOOLS",
    "ChatGenerateRequest",
    "ConversationRetrievalService",
    "DEEPSPACE_AGENT_POLICY",
    "DEEPSPACE_BUDGET_ALLOCATIONS_TOTAL",
    "DEEPSPACE_CACHE_USAGE_TOTAL",
    "DEEPSPACE_CANARY_MISMATCHES_TOTAL",
    "DEEPSPACE_COMPACT_POLICY",
    "DEEPSPACE_CONTEXT_COMPACTIONS_TOTAL",
    "DEEPSPACE_CONTEXT_OVERFLOW_PREVENTED_TOTAL",
    "DEEPSPACE_EXECUTION_BATCHES_TOTAL",
    "DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL",
    "DEEPSPACE_PROMPT_BASELINE_VERSION",
    "DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL",
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS",
    "DeepSpaceChatRepository",
    "DeepSpaceChatService",
    "DeepSpaceContextCache",
    "DeepSpaceContextEpoch",
    "DeepSpaceContextEpochStore",
    "DeepSpaceContextSummaryRepository",
    "DeepSpaceEmptyResponseError",
    "DeepSpaceMCPBridge",
    "DeepSpaceMCPTool",
    "DeepSpaceMediaArtifactService",
    "DeepSpaceProviderCircuit",
    "DeepSpaceRequestMetric",
    "DeepSpaceRequestMetricsRepository",
    "DeepSpaceRuntimeStore",
    "DeepSpaceTaskLoopStore",
    "DeepSpaceToolExecutionError",
    "DeepSpaceToolPipeline",
    "DeepSpaceToolPolicy",
    "DeepSpaceWorkspaceFile",
    "EmbeddingService",
    "ExecutionBatch",
    "ExtensionSet",
    "FATAL_TOOL_ERROR_CATEGORIES",
    "GET_TOOL_RESULT_TOOL",
    "HookParticipant",
    "IMAGE_READ_TOOL",
    "InvariantContext",
    "Literal",
    "LoopGuardLimits",
    "MAX_ATTACHMENT_OCR_CONTEXT_CHARS",
    "MAX_ATTACHMENT_OCR_TEXT_CHARS",
    "MAX_ATTACHMENT_VISION_BYTES",
    "MAX_ATTACHMENT_VISION_IMAGES",
    "MAX_COMPACT_HISTORY_CHARS",
    "MAX_CONNECTED_TOOL_RECOVERY_RETRIES",
    "MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL",
    "MAX_COORDINATION_READS_WITHOUT_PROGRESS_PER_TOOL",
    "MAX_DEADLINE_CONTINUATIONS",
    "MAX_EMPTY_PROVIDER_RETRIES",
    "MAX_NATIVE_DISCOVERY_ATTEMPTS_PER_TURN",
    "MAX_POLICY_DENIALS_PER_TURN",
    "MAX_PROVIDER_STREAM_RETRIES",
    "MAX_RAW_HISTORY_MESSAGES",
    "MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN",
    "MAX_TOOL_RETRIES",
    "MAX_WAIT_AGENT_ATTEMPTS_PER_TURN",
    "MAX_WAIT_AGENT_TOTAL_SECONDS_PER_TURN",
    "MCPResultStore",
    "MCPToolBroker",
    "MCPToolPolicyDecision",
    "MCP_BROKER_TOOL",
    "MCP_BROKER_TOOL_NAMES",
    "MCP_CALL_TOOL",
    "MCP_GET_RESULT",
    "MCP_GET_TOOL_SCHEMA",
    "MCP_SEARCH_TOOLS",
    "MIN_TOOL_CALL_SECONDS",
    "Mapping",
    "MemoryService",
    "ModelSelectionService",
    "NATIVE_TOOL_DEADLINES",
    "NATIVE_TOOL_DEFINITIONS",
    "NATIVE_TOOL_REGISTRY",
    "NativeToolBroker",
    "NativeToolValidationError",
    "PLAN_MODE_INSTRUCTION",
    "Path",
    "PlanModeService",
    "PluginStateService",
    "PresetResolution",
    "ProviderContinuationPersistenceError",
    "ProviderRegistry",
    "ProviderRequestError",
    "ProviderSecretCrypto",
    "ProviderSecretCryptoError",
    "ProviderSelectionService",
    "REASONING_REDACTION_VERSION",
    "RECOVERABLE_RESEARCH_TOOL_NAMES",
    "RECOVERABLE_TOOL_ERROR_CATEGORIES",
    "RECOVERABLE_TOOL_ERROR_CODES",
    "REPEATABLE_COORDINATION_READ_TOOLS",
    "RateLimitService",
    "RerankerService",
    "RetrievedChunkRow",
    "RuntimePhase",
    "SANDBOX_EXECUTE_TOOL",
    "STEP_END",
    "STEP_START",
    "SUPPORTED_IMAGE_MIME_TYPES",
    "SandboxExecutorError",
    "SandboxModeService",
    "Sequence",
    "SessionEventStore",
    "Settings",
    "SharedExecutionLimitError",
    "SpanHandle",
    "SpillRef",
    "SpillStoreError",
    "StorageQuotaService",
    "StorageService",
    "StorageServiceError",
    "TODO_WRITE",
    "TODO_WRITE_EVIDENCE_COMPACTION_MARKER",
    "TODO_WRITE_EVIDENCE_MAX_CHARS",
    "TODO_WRITE_TOOL",
    "TOOL_CALL",
    "TOOL_RESULT",
    "TURN_END",
    "TURN_START",
    "TimeContextService",
    "ToolContext",
    "ToolRegistry",
    "ToolResultStore",
    "UTC",
    "WEB_FETCH_TOOL",
    "WEB_SEARCH_TOOL",
    "WORKFLOW_SYSTEM_GUIDANCE",
    "WebSearchRequest",
    "WebSearchResponse",
    "WorkspaceSelectionService",
    "WorkspaceUnavailableError",
    "_FAKE_TOOL_MARKER_RE",
    "_NoRetryProviderRequestError",
    "_PRIVATE_PROVIDER_CONTINUATIONS_KEY",
    "_TimedProviderItem",
    "_TurnState",
    "_cached_native_tool_schema_json",
    "_capability_failure_message",
    "_epoch_seconds",
    "_mark_provider_output_event",
    "_normalize_reasoning_effort",
    "_pkg_attr",
    "_reasoning_mode_for_turn",
    "_relabel_citations",
    "_request_run_id",
    "_safe_label",
    "_summarize_tool_argument_value",
    "_unlocked_gate",
    "aioredis",
    "alias_definition",
    "all_tool_names",
    "annotations",
    "asynccontextmanager",
    "asyncio",
    "base64",
    "blocked_threshold_from_settings",
    "build_native_tool_registry",
    "call_signature",
    "cancellation_key",
    "cast",
    "codex_supported_reasoning_efforts",
    "collect_snapshot_files",
    "compact_request",
    "conversation_execution_gate",
    "conversation_plugin_scope",
    "copy",
    "dataclass",
    "datetime",
    "deployment_persona_section",
    "difflib",
    "dispatch_native_tool",
    "effective_preset_resolution",
    "ensure_capacity_if_supported",
    "excludes_mcp_for_prompt",
    "execute_sandbox",
    "extensions_for",
    "field",
    "get_invariant_registry",
    "get_redis_client",
    "get_session_telemetry",
    "goal_system_prompt",
    "guard_result",
    "has_matching_connected_mcp",
    "hashlib",
    "hook_audit_recorder",
    "hook_registry",
    "is_deepseek_v4_model",
    "is_explicit_mcp_action_request",
    "is_known_read_only_command",
    "itertools",
    "json",
    "logging",
    "loop_guard_registry",
    "lru_cache",
    "merge_reasoning_capabilities",
    "missing_stdio_mcp_options",
    "normalize_chat_usage",
    "normalize_harness_todos",
    "normalize_reasoning_effort_value",
    "normalize_visual_markdown",
    "parse_browser_time_zone",
    "plan_execution_batches",
    "plugin_guidance",
    "plugin_registry",
    "prepare_instruction_message",
    "prepare_skill_catalog_message",
    "publish_event_sync",
    "re",
    "reasoning_capabilities",
    "record_turn_changes",
    "redact_reasoning_text",
    "repeat_tool_reminder_registry",
    "replace",
    "request_locale",
    "requires_connected_service_action",
    "requires_tool_execution",
    "resolve_alias",
    "resolve_preset",
    "safe_extraction_status",
    "select",
    "shared_conversation_execution_slot",
    "snapshot_store",
    "spill_result",
    "sse",
    "strictest_sandbox_mode",
    "summarize_tasks",
    "summarize_tool_arguments",
    "supports_required_tool_choice",
    "take_recovered_images",
    "time",
    "todo_counts",
    "todo_summary",
    "tool_confirmation_warning",
    "tool_requires_human_confirmation",
    "transition_checkpoint",
    "unavailable_tool_names",
    "unique_tool_definitions",
    "uses_deepseek_reasoning_protocol",
    "uuid",
    "workspace_root_for",
]
