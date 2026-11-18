"""Pi.ai LLM Adapter for DeepSpace."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterable
from typing import Any

from app.deepspace.services.llm.adapter import LlmAdapter, PreparedAdapterCall
from app.deepspace.services.llm.error import LlmError
from app.deepspace.services.llm.types import (
    GenerateOptions,
    LlmModelContext,
    LlmModelInfo,
    LlmProviderInfo,
    LlmResolvedModelInfo,
    ProviderRoute,
    ResolvedRetryPolicy,
    StreamChunk,
    resolve_retry_policy,
)

PI_AI_BASE_URL = "https://api.pi.ai"
PI_AI_MODELS = {
    "pi-1": {
        "name": "Pi 1",
        "context_window": 32000,
        "max_tokens": 4096,
        "input_modalities": ("text",),
    },
}


class PiAiLlmAdapter(LlmAdapter):
    """Pi.ai provider adapter."""

    def __init__(self, api_key: str | None = None, base_url: str = PI_AI_BASE_URL) -> None:
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
                    "Authorization": f"Bearer {self._api_key}" if self._api_key else "",
                    "Content-Type": "application/json",
                    "User-Agent": "DeepSpace/1.0",
                },
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    def provider_info(self, provider: ProviderRoute) -> LlmProviderInfo:
        return LlmProviderInfo(
            id=provider,
            name="Pi.ai",
            description="Pi.ai conversational AI provider",
        )

    def provider_retry_policy(self, provider: ProviderRoute) -> ResolvedRetryPolicy:
        return resolve_retry_policy(None, f'llm: provider "{provider}" retryPolicy')

    async def list_models(self, provider: ProviderRoute) -> list[LlmModelInfo]:
        models = []
        for model_id, info in PI_AI_MODELS.items():
            models.append(
                LlmModelInfo(
                    provider=provider,
                    id=model_id,
                    name=info["name"],
                    context_window=info["context_window"],
                    max_tokens=info["max_tokens"],
                    input_modalities=info["input_modalities"],
                )
            )
        return models

    async def resolve_model(
        self,
        provider: ProviderRoute,
        model: str,
        signal: Any | None = None,
    ) -> LlmResolvedModelInfo:
        info = PI_AI_MODELS.get(model)
        if not info:
            raise LlmError(f"Unknown Pi.ai model: {model}", "UNKNOWN_MODEL")

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
        model = options.get("model", "pi-1")
        messages = options.get("messages", [])
        temperature = options.get("temperature", 1.0)
        max_tokens = options.get("max_tokens")
        _ = options.get("signal")

        request_body = {
            "model": model,
            "messages": self._format_messages(messages),
            "temperature": temperature,
            "stream": True,
        }

        if max_tokens:
            request_body["max_tokens"] = max_tokens

        async with self._http_client.stream(
            "POST", "/v1/chat/completions", json=request_body
        ) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                raise LlmError(
                    f"Pi.ai API error: {error_text.decode()}",
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

    def _format_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if isinstance(content, str):
                formatted.append({"role": role, "content": content})
            elif isinstance(content, list):
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

        if "content" in delta and delta["content"]:
            return {"type": "text", "text": delta["content"]}

        if "tool_calls" in delta:
            tool_calls = []
            for tc in delta["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", ""),
                        },
                    }
                )
            return {"type": "tool_call", "tool_calls": tool_calls}

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

        if finish_reason:
            return {"type": "complete", "finish_reason": finish_reason}

        return {"type": "text", "text": ""}


def create_pi_ai_adapter(api_key: str | None = None, base_url: str | None = None) -> PiAiLlmAdapter:
    key = api_key or os.environ.get("PI_AI_API_KEY")
    if not key:
        raise LlmError("Pi.ai API key not provided", "INVALID_CREDENTIAL")
    url = base_url or os.environ.get("PI_AI_BASE_URL", PI_AI_BASE_URL)
    return PiAiLlmAdapter(api_key=key, base_url=url)


__all__ = ["PiAiLlmAdapter", "create_pi_ai_adapter"]
