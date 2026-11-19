"""DeepSeek LLM Adapter for DeepSpace."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterable
from typing import Any

from app.deepspace.services.llm.adapter import LlmAdapter, PreparedAdapterCall
from app.deepspace.services.llm.error import INVALID_CREDENTIAL_CODE, LlmError
from app.deepspace.services.llm.types import (
    GenerateOptions,
    LlmImageRequestPricing,
    LlmModelContext,
    LlmModelInfo,
    LlmProviderInfo,
    LlmReasoningEffort,
    LlmReasoningInfo,
    LlmResolvedModelInfo,
    ProviderRoute,
    RequestMessage,
    ResolvedRetryPolicy,
    StreamChunk,
    resolve_retry_policy,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODELS = {
    "deepseek-chat": {
        "name": "DeepSeek Chat",
        "context_window": 64000,
        "max_tokens": 8192,
        "input_modalities": ("text",),
        "supports_reasoning": False,
    },
    "deepseek-reasoner": {
        "name": "DeepSeek Reasoner",
        "context_window": 64000,
        "max_tokens": 8192,
        "input_modalities": ("text",),
        "supports_reasoning": True,
        "reasoning_efforts": [
            {"id": "low", "name": "Low"},
            {"id": "medium", "name": "Medium"},
            {"id": "high", "name": "High"},
        ],
    },
}


class DeepSeekLlmAdapter(LlmAdapter):
    """DeepSeek provider adapter."""

    def __init__(self, api_key: str, base_url: str = DEEPSEEK_BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client: Any = None

    @property
    def _http_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "DeepSpace/1.0",
                },
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    def provider_info(self, provider: ProviderRoute) -> LlmProviderInfo:
        return LlmProviderInfo(
            id=provider,
            name="DeepSeek",
            description="DeepSeek API provider",
        )

    def provider_retry_policy(self, provider: ProviderRoute) -> ResolvedRetryPolicy:
        return resolve_retry_policy(None, f'llm: provider "{provider}" retryPolicy')

    def image_request_pricing(
        self, provider: ProviderRoute, model: str
    ) -> LlmImageRequestPricing | None:
        # DeepSeek doesn't have image pricing currently
        return None

    async def list_models(self, provider: ProviderRoute) -> list[LlmModelInfo]:
        models = []
        for model_id, info in DEEPSEEK_MODELS.items():
            reasoning = None
            if info.get("supports_reasoning"):
                efforts = [
                    LlmReasoningEffort(id=e["id"], name=e["name"])
                    for e in info.get("reasoning_efforts", [])
                ]
                reasoning = LlmReasoningInfo(efforts=tuple(efforts), default_effort="medium")
            models.append(
                LlmModelInfo(
                    provider=provider,
                    id=model_id,
                    name=info["name"],
                    context_window=info["context_window"],
                    max_tokens=info["max_tokens"],
                    input_modalities=info["input_modalities"],
                    reasoning=reasoning,
                )
            )
        return models

    async def resolve_model(
        self,
        provider: ProviderRoute,
        model: str,
        signal: Any | None = None,
    ) -> LlmResolvedModelInfo:
        info = DEEPSEEK_MODELS.get(model)
        if not info:
            raise LlmError(f"Unknown DeepSeek model: {model}", "UNKNOWN_MODEL")

        reasoning = None
        if info.get("supports_reasoning"):
            efforts = [
                LlmReasoningEffort(id=e["id"], name=e["name"])
                for e in info.get("reasoning_efforts", [])
            ]
            reasoning = LlmReasoningInfo(efforts=tuple(efforts), default_effort="medium")

        return LlmResolvedModelInfo(
            provider=provider,
            id=model,
            name=info["name"],
            context=LlmModelContext(
                context_window=info["context_window"],
                default_max_tokens=info["max_tokens"],
            ),
            default_max_tokens=info["max_tokens"],
            input_modalities=info["input_modalities"],
            reasoning=reasoning,
            system_prompt_update="in_history",
            tool_update="in_history",
        )

    async def prepare_call(
        self,
        provider: ProviderRoute,
        model: str,
        signal: Any | None = None,
    ) -> PreparedAdapterCall:
        model_info = await self.resolve_model(provider, model, signal)
        return PreparedAdapterCall(
            model=model_info,
            stream=self.stream,
        )

    async def stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        """Stream a call to DeepSeek API."""
        model = options.get("model", "deepseek-chat")
        messages = options.get("messages", [])
        tools = options.get("tools")
        tool_choice = options.get("tool_choice")
        temperature = options.get("temperature", 1.0)
        top_p = options.get("top_p", 1.0)
        max_tokens = options.get("max_tokens")
        reasoning_effort = options.get("reasoning_effort")
        stop = options.get("stop")
        seed = options.get("seed")
        _ = options.get("signal")

        # Build request
        request_body = {
            "model": model,
            "messages": self._format_messages(messages),
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
        }

        if max_tokens:
            request_body["max_tokens"] = max_tokens
        if reasoning_effort:
            request_body["reasoning_effort"] = reasoning_effort
        if stop:
            request_body["stop"] = stop
        if seed is not None:
            request_body["seed"] = seed
        if tools:
            request_body["tools"] = tools
            if tool_choice:
                request_body["tool_choice"] = tool_choice

        # Stream response
        async with self._http_client.stream(
            "POST", "/v1/chat/completions", json=request_body
        ) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                raise LlmError(
                    f"DeepSeek API error: {error_text.decode()}",
                    "PROVIDER_ERROR",
                    status=response.status_code,
                )

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        yield self._parse_chunk(chunk)
                    except json.JSONDecodeError:
                        continue

    def _format_messages(self, messages: list[RequestMessage]) -> list[dict[str, Any]]:
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if isinstance(content, str):
                formatted.append({"role": role, "content": content})
            elif isinstance(content, list):
                # Handle multi-modal content
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                formatted.append({"role": role, "content": "".join(text_parts)})
            else:
                formatted.append({"role": role, "content": ""})

        return formatted

    def _parse_chunk(self, chunk: dict[str, Any]) -> StreamChunk:
        choices = chunk.get("choices", [])
        if not choices:
            return {"type": "text", "text": ""}

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Text content
        if "content" in delta and delta["content"]:
            return {"type": "text", "text": delta["content"]}

        # Tool calls
        if "tool_calls" in delta:
            tool_calls = []
            for tc in delta["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc.get("id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", ""),
                        },
                    }
                )
            return {"type": "tool_call", "tool_calls": tool_calls}

        # Reasoning content (DeepSeek specific)
        if "reasoning_content" in delta and delta["reasoning_content"]:
            return {"type": "reasoning", "text": delta["reasoning_content"]}

        # Usage
        if "usage" in chunk:
            usage = chunk["usage"]
            return {
                "type": "usage",
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }

        # Finish
        if finish_reason:
            return {"type": "complete", "finish_reason": finish_reason}

        return {"type": "text", "text": ""}


def create_deepseek_adapter(
    api_key: str | None = None, base_url: str | None = None
) -> DeepSeekLlmAdapter:
    """Factory to create DeepSeek adapter with credentials from environment or args."""
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LlmError("DeepSeek API key not provided", INVALID_CREDENTIAL_CODE)
    url = base_url or os.environ.get("DEEPSEEK_BASE_URL", DEEPSEEK_BASE_URL)
    return DeepSeekLlmAdapter(api_key=key, base_url=url)


__all__ = ["DeepSeekLlmAdapter", "create_deepseek_adapter", "DEEPSEEK_MODELS"]
