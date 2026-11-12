"""Coverage for model-directed selection among configured web providers."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.tool_registry import WebSearchArguments
from app.providers.services.selection_service import ProviderSelectionService

pytestmark = pytest.mark.unit_no_db


def test_web_search_arguments_accept_a_configured_provider() -> None:
    parsed = WebSearchArguments.model_validate({"query": "current weather", "provider": "tavily"})

    assert parsed.provider == "tavily"


def test_automatic_web_search_resolution_returns_all_healthy_providers() -> None:
    providers = [
        SimpleNamespace(
            id=uuid.uuid4(),
            enabled=True,
            provider_type="tavily",
            priority=80,
            updated_at="2026-11-08T00:00:00+00:00",
            workspace_id=None,
            auth_mode="api_key",
            metadata_json={},
            display_name="Tavily",
            is_local=False,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            enabled=True,
            provider_type="searxng",
            priority=20,
            updated_at="2026-11-08T00:00:00+00:00",
            workspace_id=None,
            auth_mode="none",
            metadata_json={},
            display_name="SearXNG",
            is_local=True,
        ),
    ]
    service = object.__new__(ProviderSelectionService)
    service._providers_in_resolution_scope = lambda **_: providers
    service.health_checks = SimpleNamespace(get_latest_check=lambda **_: None)
    service._resolve_secret_value = lambda **_: "secret"
    service._provider_url = lambda provider: f"https://{provider.provider_type}.example"
    candidates: list[Any] = []

    service._append_automatic_web_search_candidates(
        tenant_id=uuid.uuid4(),
        workspace_id=None,
        owner_user_id=None,
        notes=[],
        candidates=candidates,
        seen_provider_configs=set(),
    )

    assert [candidate.provider_type for candidate in candidates] == ["tavily", "searxng"]


def test_model_provider_choice_uses_only_offered_web_provider() -> None:
    selected: dict[str, Any] = {}

    class ServiceStub:
        @staticmethod
        def _loop_guard_refusal(**_: Any) -> None:
            return None

        @staticmethod
        async def _dispatch_package_tool(**_: Any) -> None:
            return None

        @staticmethod
        async def _run_web_search(**kwargs: Any) -> tuple[None, dict[str, str]]:
            selected.update(kwargs)
            return None, {"provider": kwargs["candidate"].provider_type}

    searx = (object(), SimpleNamespace(provider_type="searxng"))
    tavily = (object(), SimpleNamespace(provider_type="tavily"))
    payload = asyncio.run(
        DeepSpaceChatService._execute_productivity_tool(
            ServiceStub(),
            tool_name="web_search",
            arguments={"query": "current news", "provider": "searxng"},
            auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
            conversation_id=uuid.uuid4(),
            web_provider=tavily[0],
            web_candidate=tavily[1],
            web_search_options={"searxng": searx, "tavily": tavily},
            request=None,
        )
    )

    assert selected["candidate"].provider_type == "searxng"
    assert payload == {"provider": "searxng"}


def test_model_cannot_select_a_web_provider_not_offered_this_turn() -> None:
    class ServiceStub:
        @staticmethod
        def _loop_guard_refusal(**_: Any) -> None:
            return None

        @staticmethod
        async def _dispatch_package_tool(**_: Any) -> None:
            return None

    with pytest.raises(ValueError, match="not available"):
        asyncio.run(
            DeepSpaceChatService._execute_productivity_tool(
                ServiceStub(),
                tool_name="web_search",
                arguments={"query": "current news", "provider": "tavily"},
                auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
                conversation_id=uuid.uuid4(),
                web_provider=object(),
                web_candidate=SimpleNamespace(provider_type="searxng"),
                web_search_options={
                    "searxng": (object(), SimpleNamespace(provider_type="searxng"))
                },
                request=None,
            )
        )


def test_analyze_document_comparison_keeps_the_turn_web_provider_options() -> None:
    options = {"tavily": (object(), SimpleNamespace(provider_type="tavily"))}
    nested: dict[str, Any] = {}

    class ServiceStub:
        @staticmethod
        def _loop_guard_refusal(**_: Any) -> None:
            return None

        @staticmethod
        async def _dispatch_package_tool(**kwargs: Any) -> dict[str, str] | None:
            if kwargs["tool_name"] == "document_compare":
                nested.update(kwargs)
                return {"status": "ok"}
            return None

        async def _execute_productivity_tool(self, **kwargs: Any) -> dict[str, str] | None:
            return await self._dispatch_package_tool(**kwargs)

    result = asyncio.run(
        DeepSpaceChatService._execute_productivity_tool(
            ServiceStub(),
            tool_name="analyze",
            arguments={
                "operation": "compare_documents",
                "left_file_id": "left",
                "right_file_id": "right",
            },
            auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
            conversation_id=uuid.uuid4(),
            web_provider=options["tavily"][0],
            web_candidate=options["tavily"][1],
            web_search_options=options,
            request=None,
        )
    )

    assert result == {"status": "ok"}
    assert nested["web_search_options"] is options
