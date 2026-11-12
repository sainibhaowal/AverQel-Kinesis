from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.deepspace.services.session_titles import _generate_title_with_llm
from app.providers.services.types import ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


class _Provider:
    def __init__(self) -> None:
        self.request: Any = None

    def generate(self, request: Any) -> ChatGenerateResponse:
        self.request = request
        return ChatGenerateResponse(content=" A better title ")


class _Registry:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def get_chat_provider(self) -> _Provider:
        return self.provider


class _Circuit:
    def is_open(
        self,
        *,
        tenant_id: UUID,
        provider_config_id: UUID | None,
        model_name: str,
    ) -> bool:
        return False


@pytest.mark.asyncio
async def test_title_generator_uses_configured_chat_request() -> None:
    provider = _Provider()
    settings = SimpleNamespace(
        deepspace_session_title_model=None,
        llm_model="configured-model",
        llm_api_base_url="https://provider.invalid/v1",
        llm_api_key="test-key",
    )

    title = await _generate_title_with_llm(
        "Summarize this conversation",
        fallback=None,
        max_tokens=40,
        settings=settings,  # type: ignore[arg-type]
        provider_registry=_Registry(provider),  # type: ignore[arg-type]
        circuit=_Circuit(),  # type: ignore[arg-type]
    )

    assert title == "A better title"
    assert provider.request.model == "configured-model"
    assert provider.request.messages == [{"role": "user", "content": "Summarize this conversation"}]
    assert provider.request.max_tokens == 40
    assert provider.request.api_key == "test-key"
