"""DeepSpace LLM Service — native Python implementation of @deepseek-ai/dsh-llm."""

from __future__ import annotations

from typing import Any

from app.deepspace.services.llm.adapter import LlmAdapter
from app.deepspace.services.llm.brand import (
    ModelModality,
    ProviderRequestId,
    ProviderRoute,
)
from app.deepspace.services.llm.deepseek.adapter import DeepSeekLlmAdapter, create_deepseek_adapter
from app.deepspace.services.llm.error import (
    DUPLICATE_ADAPTER_CODE,
    DUPLICATE_DIRECTORY_CODE,
    INVALID_ADAPTER_CODE,
    INVALID_CATALOG_CODE,
    INVALID_CREDENTIAL_CODE,
    INVALID_DIRECTORY_CODE,
    INVALID_DISCOVERY_CODE,
    INVALID_MODEL_CONTEXT_CODE,
    INVALID_MODEL_INFO_CODE,
    INVALID_MODEL_MAX_TOKENS_CODE,
    INVALID_MODEL_REASONING_CODE,
    INVALID_PREPARED_CALL_CODE,
    NO_ADAPTER_CODE,
    NO_DISCOVERY_CODE,
    REGISTRATION_DISPOSED_CODE,
    UNSUPPORTED_REASONING_EFFORT_CODE,
    HarnessError,
    LlmError,
    assert_usable_api_key,
    normalize_api_key,
)
from app.deepspace.services.llm.pi_ai.adapter import PiAiLlmAdapter, create_pi_ai_adapter
from app.deepspace.services.llm.runtime import LlmRuntime
from app.deepspace.services.llm.token_meter.projection import (
    ContextBreakdownProjection,
    MeasurementProjection,
    RoutePricing,
    SurfaceProjection,
    TurnUsage,
    UsageProjection,
)
from app.deepspace.services.llm.types import (
    ContentBlock,
    GenerateOptions,
    LlmCallConfig,
    LlmCallConfigAdapterDefaults,
    LlmConfigurableProvider,
    LlmDiscoveredModel,
    LlmImageRequestPricing,
    LlmImageRequestTarget,
    LlmModelContext,
    LlmModelDiscoveryRequest,
    LlmModelInfo,
    LlmProviderInfo,
    LlmReasoningEffort,
    LlmReasoningInfo,
    LlmResolvedModelInfo,
    PreparedAdapterCall,
    PreparedLlmCall,
    RequestMessage,
    ResolvedRetryPolicy,
    StreamChunk,
    StreamChunkType,
    SystemPromptUpdate,
    ToolSchema,
    ToolUpdate,
    content_has_file,
    content_has_image,
    file_handle_text,
    freeze_message,
    project_files_to_text,
    project_images_for_text_model,
    project_tool_updates,
    resolve_retry_policy,
)

# Global runtime instance
_LLM_RUNTIME: LlmRuntime | None = None


def get_llm_runtime(ctx: Any) -> LlmRuntime:
    """Get or create the global LLM runtime."""
    global _LLM_RUNTIME
    if _LLM_RUNTIME is None:
        _LLM_RUNTIME = LlmRuntime(ctx=ctx)
    return _LLM_RUNTIME


def init_llm_runtime(ctx: Any) -> LlmRuntime:
    """Initialize the LLM runtime with a context."""
    global _LLM_RUNTIME
    _LLM_RUNTIME = LlmRuntime(ctx=ctx)
    return _LLM_RUNTIME


__all__ = [
    # Brand
    "ProviderRequestId",
    "ProviderRequestId_",
    "ModelModality",
    "ModelModality_",
    "ProviderRoute",
    "ProviderRoute_",
    # Errors
    "LlmError",
    "HarnessError",
    "assert_usable_api_key",
    "normalize_api_key",
    "INVALID_CREDENTIAL_CODE",
    "NO_ADAPTER_CODE",
    "DUPLICATE_ADAPTER_CODE",
    "INVALID_ADAPTER_CODE",
    "REGISTRATION_DISPOSED_CODE",
    "INVALID_DIRECTORY_CODE",
    "DUPLICATE_DIRECTORY_CODE",
    "NO_DISCOVERY_CODE",
    "INVALID_DISCOVERY_CODE",
    "INVALID_CATALOG_CODE",
    "INVALID_MODEL_INFO_CODE",
    "INVALID_MODEL_CONTEXT_CODE",
    "INVALID_MODEL_MAX_TOKENS_CODE",
    "INVALID_MODEL_REASONING_CODE",
    "UNSUPPORTED_REASONING_EFFORT_CODE",
    "INVALID_PREPARED_CALL_CODE",
    # Types
    "LlmProviderInfo",
    "LlmModelInfo",
    "LlmDiscoveredModel",
    "LlmModelContext",
    "LlmResolvedModelInfo",
    "LlmReasoningEffort",
    "LlmReasoningInfo",
    "LlmImageRequestPricing",
    "LlmImageRequestTarget",
    "LlmConfigurableProvider",
    "LlmModelDiscoveryRequest",
    "RequestMessage",
    "ContentBlock",
    "ToolSchema",
    "StreamChunk",
    "StreamChunkType",
    "GenerateOptions",
    "LlmCallConfig",
    "LlmCallConfigAdapterDefaults",
    "PreparedLlmCall",
    "PreparedAdapterCall",
    "SystemPromptUpdate",
    "ToolUpdate",
    "ResolvedRetryPolicy",
    "resolve_retry_policy",
    "content_has_file",
    "content_has_image",
    "file_handle_text",
    "project_files_to_text",
    "project_images_for_text_model",
    "project_tool_updates",
    "freeze_message",
    # Adapter
    "LlmAdapter",
    "PreparedAdapterCall",
    # Runtime
    "LlmRuntime",
    # Providers
    "DeepSeekLlmAdapter",
    "create_deepseek_adapter",
    "PiAiLlmAdapter",
    "create_pi_ai_adapter",
    # Token Meter
    "TurnUsage",
    "RoutePricing",
    "MeasurementProjection",
    "ContextBreakdownProjection",
    "SurfaceProjection",
    "UsageProjection",
    # Factory
    "get_llm_runtime",
    "init_llm_runtime",
]
