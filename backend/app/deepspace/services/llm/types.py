"""Core types for DeepSpace LLM service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, cast

from app.deepspace.services.llm.brand import ProviderRoute

# Model modalities
ModelModalityValue = Literal["text", "image", "audio", "file"]


@dataclass(frozen=True, slots=True)
class LlmProviderInfo:
    id: ProviderRoute
    name: str
    description: str | None = None
    icon_url: str | None = None


@dataclass(frozen=True, slots=True)
class LlmModelInfo:
    provider: ProviderRoute
    id: str
    name: str
    description: str | None = None
    input_modalities: tuple[ModelModalityValue, ...] = ("text",)
    output_modalities: tuple[ModelModalityValue, ...] = ("text",)
    context_window: int | None = None
    max_tokens: int | None = None
    reasoning: LlmReasoningInfo | None = None
    badge_text: str | None = None
    badge_tone: Literal["default", "beta", "deprecated"] = "default"


@dataclass(frozen=True, slots=True)
class LlmDiscoveredModel:
    id: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    input_modalities: tuple[ModelModalityValue, ...] = ("text",)


@dataclass(frozen=True, slots=True)
class LlmModelContext:
    context_window: int
    default_max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmResolvedModelInfo:
    provider: ProviderRoute
    id: str
    name: str
    description: str | None = None
    input_modalities: tuple[ModelModalityValue, ...] = ("text",)
    context: LlmModelContext | None = None
    default_max_tokens: int | None = None
    system_prompt_update: Literal["in_history", "addition_only"] | None = None
    tool_update: Literal["in_history", "addition_only"] | None = None
    reasoning: LlmReasoningInfo | None = None


@dataclass(frozen=True, slots=True)
class LlmReasoningEffort:
    id: str
    name: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class LlmReasoningInfo:
    efforts: tuple[LlmReasoningEffort, ...]
    default_effort: str | None = None


@dataclass(frozen=True, slots=True)
class LlmImageRequestPricing:
    price_per_image: float
    price_per_megapixel: float | None = None


@dataclass(frozen=True, slots=True)
class LlmImageRequestTarget:
    width: int
    height: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class LlmConfigurableProvider:
    provider: ProviderRoute
    display_name: str
    description: str | None = None
    settings_ns: str = ""
    settings_path: tuple[str, ...] = ()
    auth_type: Literal["api_key", "oauth", "none"] = "api_key"
    default_model: str | None = None


@dataclass(frozen=True, slots=True)
class LlmModelDiscoveryRequest:
    provider: ProviderRoute | None = None
    base_url: str | None = None
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


# Message types
class ContentBlock(TypedDict, total=False):
    type: Literal["text", "image", "file", "tool_call", "tool_result"]
    text: str
    image: dict[str, Any]
    file: dict[str, Any]
    tool_call: dict[str, Any]
    tool_result: dict[str, Any]


class RequestMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    name: str
    source: dict[str, Any]


# Tool schema
class ToolSchema(TypedDict, total=False):
    type: Literal["function"]
    function: dict[str, Any]


# Stream chunks
StreamChunkType = Literal[
    "text",
    "tool_call",
    "tool_result",
    "reasoning",
    "usage",
    "error",
    "complete",
]


class StreamChunk(TypedDict, total=False):
    type: StreamChunkType
    text: str
    tool_call: dict[str, Any]
    tool_result: dict[str, Any]
    reasoning: dict[str, Any]
    usage: dict[str, int]
    error: dict[str, Any]
    complete: bool
    tool_calls: list[dict[str, Any]]
    finish_reason: str


# Generate options
class GenerateOptions(TypedDict, total=False):
    provider: ProviderRoute
    model: str
    messages: list[RequestMessage]
    tools: list[ToolSchema]
    tool_choice: Literal["auto", "none", "required"] | dict[str, Any]
    temperature: float
    top_p: float
    max_tokens: int
    reasoning_effort: str
    stop: list[str]
    seed: int
    signal: Any  # AbortSignal
    metadata: dict[str, Any]


# Call config
@dataclass(frozen=True, slots=True)
class LlmCallConfig:
    provider: ProviderRoute
    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    stop: list[str] = field(default_factory=list)
    seed: int | None = None
    tool_history: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LlmCallConfigAdapterDefaults:
    reasoning_effort: bool = False
    max_tokens: bool = False


class ConfigureCall(TypedDict, total=False):
    """Callback to select concrete controls using captured model metadata."""

    pass  # Will be a callable


@dataclass(frozen=True, slots=True)
class PreparedLlmCall:
    config: LlmCallConfig
    retry_policy: ResolvedRetryPolicy
    adapter_defaults: LlmCallConfigAdapterDefaults
    stream: Any  # Callable[[GenerateOptions], Any]
    context: LlmModelContext | None = None
    input_modalities: tuple[ModelModalityValue, ...] | None = None
    system_prompt_update: Literal["in_history", "addition_only"] | None = None
    tool_update: Literal["in_history", "addition_only"] | None = None


@dataclass(frozen=True, slots=True)
class PreparedAdapterCall:
    model: LlmResolvedModelInfo
    stream: Any  # Callable[[GenerateOptions], Any]


# System prompt / tool updates
SystemPromptUpdate = Literal["in_history", "addition_only"]
ToolUpdate = Literal["in_history", "addition_only"]


# Retry policy
@dataclass(frozen=True, slots=True)
class ResolvedRetryPolicy:
    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    retry_on: tuple[int, ...]  # HTTP status codes
    retry_on_errors: tuple[str, ...]  # Error codes


def resolve_retry_policy(
    policy: ResolvedRetryPolicy | None,
    context: str,
) -> ResolvedRetryPolicy:
    if policy is not None:
        return policy
    return ResolvedRetryPolicy(
        max_attempts=3,
        base_delay_ms=1000,
        max_delay_ms=30000,
        retry_on=(429, 500, 502, 503, 504),
        retry_on_errors=("RATE_LIMIT", "OVERLOADED", "TIMEOUT"),
    )


# Content handling
def content_has_file(content: str | list[ContentBlock]) -> bool:
    if isinstance(content, str):
        return False
    return any(block.get("type") == "file" for block in content)


def content_has_image(content: str | list[ContentBlock]) -> bool:
    if isinstance(content, str):
        return False
    return any(block.get("type") == "image" for block in content)


def file_handle_text(ref: dict[str, Any], host_path: str | None) -> str:
    if host_path:
        return f"[File: {host_path}]"
    return f"[File: {ref.get('id', 'unknown')}]"


def project_files_to_text(
    messages: list[RequestMessage],
    read_path: Callable[[dict[str, Any]], str | None],
) -> list[RequestMessage]:
    result: list[RequestMessage] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_content: list[ContentBlock] = []
            for block in content:
                if block.get("type") == "file":
                    ref = block.get("file") or {}
                    path = read_path(ref)
                    new_content.append({"type": "text", "text": file_handle_text(ref, path)})
                else:
                    new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)
    return result


def project_images_for_text_model(messages: list[RequestMessage]) -> list[RequestMessage]:
    result: list[RequestMessage] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_content: list[ContentBlock] = [b for b in content if b.get("type") != "image"]
            if len(new_content) != len(content):
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


def project_tool_updates(
    messages: list[RequestMessage],
    tools: list[ToolSchema] | None,
    tool_update: ToolUpdate | None,
    tool_history: list[str],
) -> tuple[list[RequestMessage], list[ToolSchema] | None]:
    if tool_update == "addition_only":
        # Only include tools not in history
        new_tools = [
            t for t in (tools or []) if t.get("function", {}).get("name") not in tool_history
        ]
        return messages, new_tools if new_tools else None
    return messages, tools


def freeze_message(msg: RequestMessage) -> RequestMessage:
    """Deep-freeze a message for immutable transport."""
    return cast(RequestMessage, dict(msg))


def deep_freeze(obj: Any) -> Any:
    """Recursively freeze an object for immutability."""
    if isinstance(obj, dict):
        return tuple(sorted((k, deep_freeze(v)) for k, v in obj.items()))
    elif isinstance(obj, list):
        return tuple(deep_freeze(v) for v in obj)
    elif isinstance(obj, set):
        return tuple(sorted(deep_freeze(v) for v in obj))
    return obj


def structured_clone(obj: Any) -> Any:
    """Create a deep clone of an object."""
    import copy

    return copy.deepcopy(obj)


def call_config_equals(a: Any, b: Any) -> bool:
    """Compare two call configs for equality."""
    return bool(deep_freeze(a) == deep_freeze(b))


__all__ = [
    "ModelModalityValue",
    "LlmProviderInfo",
    "LlmModelInfo",
    "ProviderRoute",
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
    "ConfigureCall",
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
]
