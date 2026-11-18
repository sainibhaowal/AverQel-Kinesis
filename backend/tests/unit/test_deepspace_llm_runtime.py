"""Tests for DeepSpace LLM Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import pytest

from app.deepspace.services.llm.adapter import LlmAdapter
from app.deepspace.services.llm.brand import ProviderRoute
from app.deepspace.services.llm.error import (
    DUPLICATE_ADAPTER_CODE,
    INVALID_ADAPTER_CODE,
    NO_ADAPTER_CODE,
    LlmError,
)
from app.deepspace.services.llm.runtime import LlmRuntime
from app.deepspace.services.llm.types import (
    GenerateOptions,
    LlmModelContext,
    LlmModelInfo,
    LlmProviderInfo,
    LlmResolvedModelInfo,
    StreamChunk,
)

pytestmark = pytest.mark.unit_no_db


class MockAdapter(LlmAdapter):
    def __init__(self) -> None:
        self.stream_called = False

    async def stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        self.stream_called = True
        yield {"type": "text", "text": "hello"}
        yield {"type": "complete", "finish_reason": "stop"}

    async def list_models(self, provider: ProviderRoute) -> list[LlmModelInfo]:
        return [LlmModelInfo(provider=provider, id="test-model", name="Test Model")]

    async def resolve_model(
        self, provider: ProviderRoute, model: str, signal: Any = None
    ) -> LlmResolvedModelInfo:
        return LlmResolvedModelInfo(
            provider=provider,
            id=model,
            name="Test Model",
            context=LlmModelContext(context_window=4096),
        )


class TestLlmRuntime:
    def setup_method(self) -> None:
        self.ctx = type(
            "MockContext",
            (),
            {
                "events": type("MockEvents", (), {"dispatch": lambda self, *args, **kwargs: []})(),
                "logger": type("MockLogger", (), {"warning": lambda self, msg: None})(),
            },
        )()
        self.runtime = LlmRuntime(ctx=self.ctx)

    def test_register_adapter(self) -> None:
        adapter = MockAdapter()
        dispose = self.runtime.register_adapter([ProviderRoute("test")], adapter)
        assert ProviderRoute("test") in self.runtime.adapters
        dispose()
        assert ProviderRoute("test") not in self.runtime.adapters

    def test_duplicate_adapter_raises(self) -> None:
        adapter1 = MockAdapter()
        adapter2 = MockAdapter()
        self.runtime.register_adapter([ProviderRoute("test")], adapter1)
        with pytest.raises(LlmError) as exc_info:
            self.runtime.register_adapter([ProviderRoute("test")], adapter2)
        assert exc_info.value.code == DUPLICATE_ADAPTER_CODE

    def test_invalid_adapter_metadata(self) -> None:
        class BadAdapter(LlmAdapter):
            def provider_info(self, provider: ProviderRoute) -> LlmProviderInfo:
                return LlmProviderInfo(id=ProviderRoute("other"), name="Bad")

            async def stream(self, options: GenerateOptions):
                yield {"type": "text", "text": ""}

        with pytest.raises(LlmError) as exc_info:
            self.runtime.register_adapter([ProviderRoute("test")], BadAdapter())
        assert exc_info.value.code == INVALID_ADAPTER_CODE

    def test_list_providers(self) -> None:
        adapter = MockAdapter()
        self.runtime.register_adapter([ProviderRoute("test")], adapter)
        providers = self.runtime.list_providers()
        assert len(providers) == 1
        assert providers[0].id == ProviderRoute("test")

    def test_configurable_providers(self) -> None:
        from app.deepspace.services.llm.types import LlmConfigurableProvider

        entry = LlmConfigurableProvider(
            provider=ProviderRoute("configurable"),
            display_name="Configurable",
            settings_ns="test",
        )
        dispose = self.runtime.register_configurable_providers([entry])
        configurable = self.runtime.list_configurable_providers()
        assert len(configurable) == 1
        assert configurable[0].provider == ProviderRoute("configurable")
        dispose()
        assert len(self.runtime.list_configurable_providers()) == 0

    @pytest.mark.asyncio
    async def test_prepare_call(self) -> None:
        adapter = MockAdapter()
        self.runtime.register_adapter([ProviderRoute("test")], adapter)

        from app.deepspace.services.llm.types import LlmCallConfig

        config = LlmCallConfig(provider=ProviderRoute("test"), model="test-model")
        prepared = await self.runtime.prepare_call(config)

        assert prepared.config.provider == ProviderRoute("test")
        assert prepared.config.model == "test-model"
        assert prepared.stream is not None

    @pytest.mark.asyncio
    async def test_stream_with_waterfall(self) -> None:
        adapter = MockAdapter()
        self.runtime.register_adapter([ProviderRoute("test")], adapter)

        from app.deepspace.services.llm.types import LlmCallConfig

        config = LlmCallConfig(provider=ProviderRoute("test"), model="test-model")
        prepared = await self.runtime.prepare_call(config)

        # Build the exact options dict that matches the resolved config
        opts = {
            "provider": prepared.config.provider,
            "model": prepared.config.model,
            "messages": [],
            "temperature": prepared.config.temperature,
            "top_p": prepared.config.top_p,
            "max_tokens": prepared.config.max_tokens,
            "reasoning_effort": prepared.config.reasoning_effort,
            "stop": prepared.config.stop,
            "seed": prepared.config.seed,
            "tool_history": prepared.config.tool_history,
        }
        chunks = []
        async for chunk in prepared.stream(opts):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0]["type"] == "text"
        assert chunks[1]["type"] == "complete"

    @pytest.mark.asyncio
    async def test_no_adapter_error(self) -> None:
        chunks = []
        async for chunk in self.runtime.stream(
            {"provider": ProviderRoute("unknown"), "model": "x", "messages": []}
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert chunks[0]["error"]["code"] == NO_ADAPTER_CODE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
