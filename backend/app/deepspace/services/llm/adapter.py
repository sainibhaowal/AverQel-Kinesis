"""Abstract LLM Adapter for DeepSpace."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any

from app.deepspace.services.llm.brand import ProviderRoute
from app.deepspace.services.llm.types import (
    GenerateOptions,
    LlmImageRequestPricing,
    LlmImageRequestTarget,
    LlmModelInfo,
    LlmProviderInfo,
    LlmResolvedModelInfo,
    ResolvedRetryPolicy,
    StreamChunk,
)


class LlmAdapter(ABC):
    """Abstract base class for LLM provider adapters."""

    def provider_info(self, provider: ProviderRoute) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=str(provider))

    def provider_retry_policy(self, provider: ProviderRoute) -> ResolvedRetryPolicy | None:
        return None

    def image_request_pricing(
        self,
        provider: ProviderRoute,
        model: str,
    ) -> LlmImageRequestPricing | None:
        return None

    def image_request_target(
        self,
        provider: ProviderRoute,
        model: str,
        image: dict[str, Any],
    ) -> LlmImageRequestTarget:
        return LlmImageRequestTarget(
            width=image.get("width", 0),
            height=image.get("height", 0),
            max_bytes=image.get("bytes", 0),
        )

    async def list_models(self, provider: ProviderRoute) -> list[LlmModelInfo]:
        return []

    async def resolve_model(
        self,
        provider: ProviderRoute,
        model: str,
        signal: Any | None = None,
    ) -> LlmResolvedModelInfo:
        return LlmResolvedModelInfo(
            provider=provider,
            id=model,
            name=model,
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

    @abstractmethod
    def stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        """Stream one model call as raw chunks. The only required method."""
        pass


@dataclass(frozen=True, slots=True)
class PreparedAdapterCall:
    model: LlmResolvedModelInfo
    stream: Any  # Callable[[GenerateOptions], Any]


__all__ = ["LlmAdapter", "PreparedAdapterCall"]
