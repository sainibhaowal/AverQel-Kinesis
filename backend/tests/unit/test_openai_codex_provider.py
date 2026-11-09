from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.providers.services import openai_codex_provider as codex_module
from app.providers.services.base import ProviderCapabilityError, ProviderRequestError
from app.providers.services.codex_cli_runtime import CodexCliAccountMismatchError
from app.providers.services.openai_codex_config import (
    codex_reasoning_effort_values,
    codex_supported_reasoning_efforts,
    validate_codex_desktop_settings,
)
from app.providers.services.openai_codex_provider import (
    OpenAICodexProvider,
    OpenAIPlanUsageLimitError,
)
from app.providers.services.types import ChatGenerateRequest

pytestmark = pytest.mark.unit_no_db


def _request() -> ChatGenerateRequest:
    return ChatGenerateRequest(
        model="gpt-5-codex",
        messages=[
            {"role": "system", "content": "Follow the app's policy."},
            {"role": "user", "content": "Summarize this."},
        ],
        temperature=0.2,
        max_tokens=256,
        base_url="https://api.openai.com/v1",
        reasoning_enabled=True,
        reasoning_effort="high",
        metadata={"tenant_id": "tenant-test", "user_id": "user-test"},
    )


def _codex_provider(
    efforts: list[str] | None = None,
    effort_values: dict[str, str] | None = None,
) -> OpenAICodexProvider:
    model: dict[str, Any] = {"id": "gpt-5-codex", "name": "GPT-5 Codex"}
    if efforts is not None:
        model["supported_reasoning_efforts"] = efforts
    if effort_values is not None:
        model["reasoning_effort_values"] = effort_values
    return OpenAICodexProvider(
        metadata={
            "codex_desktop": {
                "account_id": "0123456789abcdef0123456789abcdef",
                "models": [model],
            }
        }
    )


def _local_codex_provider() -> OpenAICodexProvider:
    return OpenAICodexProvider(
        metadata={
            "codex_desktop": {
                "account_id": "0123456789abcdef0123456789abcdef",
                "models": [{"id": "gpt-5-codex", "name": "GPT-5 Codex"}],
            }
        },
        tenant_id="tenant-test",
        owner_user_id="user-test",
    )


def _enable_local_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="memory://")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: True)


@pytest.mark.parametrize(
    ("ui_effort", "api_effort"),
    [
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("very_high", "xhigh"),
        ("extreme_high", "max"),
        ("ultra", "ultra"),
    ],
)
def test_codex_reasoning_levels_map_to_distinct_supported_api_efforts(
    ui_effort: str, api_effort: str
) -> None:
    provider = _codex_provider(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])

    payload = provider._responses_payload(replace(_request(), reasoning_effort=ui_effort))

    assert payload["reasoning"] == {"effort": api_effort, "summary": "auto"}


def test_codex_model_discovery_preserves_exact_reasoning_capabilities() -> None:
    provider = _codex_provider(["low", "high", "very_high"])

    model = provider.list_models()[0]

    assert model.capabilities["supported_reasoning_efforts"] == ["low", "high", "very_high"]
    assert model.capabilities["supports_reasoning"] is True


def test_codex_model_advertised_custom_effort_reaches_the_api_unchanged() -> None:
    provider = _codex_provider(["tier_5"], {"tier_5": "tier-five"})

    payload = provider._responses_payload(replace(_request(), reasoning_effort="tier_5"))

    assert payload["reasoning"] == {"effort": "tier-five", "summary": "auto"}
    assert codex_supported_reasoning_efforts(provider.codex_settings, "gpt-5-codex") == ("tier_5",)
    assert codex_reasoning_effort_values(provider.codex_settings, "gpt-5-codex") == {
        "tier_5": "tier-five"
    }


def test_codex_request_preserves_attached_image_mime_type() -> None:
    provider = _codex_provider()
    request = replace(_request(), images=["data:image/png;base64,iVBORw0KGgo="])

    payload = provider._responses_payload(request)

    assert payload["input"][-1] == {
        "role": "user",
        "content": [{"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}],
    }


def test_codex_explicit_empty_effort_catalog_is_not_treated_as_unknown() -> None:
    settings = {"models": [{"id": "gpt-5-codex", "supported_reasoning_efforts": []}]}

    assert codex_supported_reasoning_efforts(settings, "gpt-5-codex") == ()
    assert (
        codex_supported_reasoning_efforts({"models": [{"id": "gpt-5-codex"}]}, "gpt-5-codex")
        is None
    )


def test_codex_configuration_keeps_and_validates_model_effort_metadata() -> None:
    metadata = {
        "codex_desktop": {
            "account_id": "0123456789abcdef0123456789abcdef",
            "models": [
                {
                    "id": "gpt-5-codex",
                    "name": "GPT-5 Codex",
                    "supported_reasoning_efforts": ["low", "very_high", "extreme_high"],
                }
            ],
        }
    }

    normalized = validate_codex_desktop_settings(metadata, default_chat_model="gpt-5-codex")
    assert normalized["models"][0]["supported_reasoning_efforts"] == [
        "low",
        "very_high",
        "extreme_high",
    ]

    metadata["codex_desktop"]["models"][0]["supported_reasoning_efforts"] = ["ultra"]
    normalized = validate_codex_desktop_settings(metadata)
    assert normalized["models"][0]["supported_reasoning_efforts"] == ["ultra"]

    metadata["codex_desktop"]["models"][0]["supported_reasoning_efforts"] = ["invalid/value"]
    with pytest.raises(ValueError, match="reasoning metadata"):
        validate_codex_desktop_settings(metadata)


def test_codex_request_rejects_effort_not_advertised_by_selected_model() -> None:
    provider = _codex_provider(["low", "medium", "high"])

    with pytest.raises(ProviderCapabilityError, match="does not support"):
        provider._responses_payload(replace(_request(), reasoning_effort="very_high"))


def test_streams_text_and_tool_arguments_then_completes_without_exposing_account_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_local_cli(monkeypatch)
    calls: list[dict[str, Any]] = []

    async def stream_responses(**kwargs: Any) -> Any:
        calls.append(kwargs)
        yield {"type": "response.output_text.delta", "delta": "Ready."}
        yield {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "call-1",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": "",
            },
        }
        yield {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "arguments": '{"query":"weather"}',
        }
        yield {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 42, "output_tokens": 9, "total_tokens": 51}},
        }

    monkeypatch.setattr(codex_module.codex_cli_runtime, "stream_responses", stream_responses)
    provider = _local_codex_provider()
    request = replace(
        _request(),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
    )

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in provider.stream_generate_events(request)]

    events = asyncio.run(collect())

    assert {"type": "delta", "text": "Ready."} in events
    tool_call_events = [event for event in events if event.get("type") == "tool_calls_delta"]
    assert len(tool_call_events) == 2
    assert tool_call_events[0]["tool_calls"][0]["function"]["name"] == "lookup"
    assert tool_call_events[1]["tool_calls"][0]["function"]["arguments"] == '{"query":"weather"}'
    assert {
        "type": "usage",
        "usage": {"prompt_tokens": 42, "completion_tokens": 9, "total_tokens": 51},
    } in events
    assert len(calls) == 1
    assert calls[0]["account_id"] == "0123456789abcdef0123456789abcdef"
    assert calls[0]["payload"]["model"] == "gpt-5-codex"
    assert calls[0]["payload"]["tools"][0]["tools"][0]["name"] == "lookup"
    assert "tenant-test" not in str(calls[0]["payload"])


def test_plan_usage_limit_error_is_returned_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_local_cli(monkeypatch)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "stream_responses",
        lambda **_kwargs: _error_stream(),
    )
    monkeypatch.setattr(
        codex_module.codex_chatgpt_oauth_runtime,
        "has_account",
        lambda **_kwargs: pytest.fail("a usage limit must not trigger an OAuth retry"),
    )
    provider = _local_codex_provider()

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in provider.stream_generate_events(_request())]

    with pytest.raises(OpenAIPlanUsageLimitError) as error:
        asyncio.run(collect())

    assert error.value.status_code == 429
    assert error.value.no_retry is True


def _error_stream() -> Any:
    async def emit_error() -> Any:
        yield {
            "type": "response.failed",
            "error": {
                "message": "Usage limit reached.",
                "code": "subscription_sharing_usage_limit_exceeded",
            },
        }

    return emit_error()


def test_codex_normalize_event_handles_reasoning_and_thinking() -> None:
    tool_ids: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    accumulated_arguments: dict[int, str] = {}

    delta_events = OpenAICodexProvider._normalize_event(
        {"type": "response.reasoning.delta", "delta": "Analyzing architecture..."},
        tool_ids=tool_ids,
        tool_names=tool_names,
        accumulated_arguments=accumulated_arguments,
    )
    assert delta_events == [{"type": "thinking", "text": "Analyzing architecture..."}]

    summary_events = OpenAICodexProvider._normalize_event(
        {"type": "response.reasoning_summary_text.delta", "delta": "Checked the request."},
        tool_ids=tool_ids,
        tool_names=tool_names,
        accumulated_arguments=accumulated_arguments,
    )
    assert summary_events == [{"type": "thinking", "text": "Checked the request."}]

    done_events = OpenAICodexProvider._normalize_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "text": "Plan complete."},
        },
        tool_ids=tool_ids,
        tool_names=tool_names,
        accumulated_arguments=accumulated_arguments,
    )
    assert done_events == [{"type": "thinking", "text": "Plan complete."}]


def test_unsupported_backend_refuses_codex_without_falling_back_to_a_host_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="redis://isolated")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: False)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "stream_responses",
        lambda **_kwargs: pytest.fail("unsupported deployments must not start the local CLI"),
    )

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in _local_codex_provider().stream_generate_events(_request())]

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(collect())

    assert error.value.status_code == 503
    assert "standalone local Kinesis backend" in str(error.value)


def test_kinesis_codex_provider_streams_local_cli_without_electron_or_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="memory://")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: True)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "matches_current_cli_account",
        lambda _account_id: True,
    )

    async def stream_responses(**_kwargs: Any) -> Any:
        yield {"type": "response.output_text.delta", "delta": "Direct from backend."}
        yield {"type": "response.completed"}

    monkeypatch.setattr(codex_module.codex_cli_runtime, "stream_responses", stream_responses)
    provider = OpenAICodexProvider(
        metadata={
            "codex_desktop": {
                "account_id": "0123456789abcdef0123456789abcdef",
                "models": [{"id": "gpt-5-codex", "name": "GPT-5 Codex"}],
            }
        },
        tenant_id="tenant-test",
        owner_user_id="user-test",
    )

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in provider.stream_generate_events(_request())]

    events = asyncio.run(collect())

    assert events == [{"type": "delta", "text": "Direct from backend."}]


def test_kinesis_codex_health_uses_local_cli_account_without_electron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="memory://")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: True)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "matches_current_cli_account",
        lambda _account_id: True,
    )
    provider = OpenAICodexProvider(
        metadata={"codex_desktop": {"account_id": "account-test"}},
        tenant_id="tenant-test",
        owner_user_id="user-test",
    )

    status = provider.health_check()

    assert status.status == "healthy"
    assert status.metadata["runtime"] == "backend_codex_cli"


def test_kinesis_browser_account_falls_back_to_the_scoped_oauth_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_local_cli(monkeypatch)
    oauth_calls: list[dict[str, Any]] = []

    async def fail_cli(**_kwargs: Any) -> Any:
        raise CodexCliAccountMismatchError("Different local account.", status_code=409)
        yield {}

    async def stream_oauth(**kwargs: Any) -> Any:
        oauth_calls.append(kwargs)
        yield {"type": "response.output_text.delta", "delta": "OAuth account."}
        yield {"type": "response.completed"}

    monkeypatch.setattr(codex_module.codex_cli_runtime, "stream_responses", fail_cli)
    monkeypatch.setattr(
        codex_module.codex_chatgpt_oauth_runtime,
        "has_account",
        lambda **kwargs: kwargs
        == {
            "account_id": "oauth-account-id",
            "tenant_id": "tenant-test",
            "actor_user_id": "user-test",
        },
    )
    monkeypatch.setattr(codex_module.codex_chatgpt_oauth_runtime, "stream_responses", stream_oauth)
    provider = OpenAICodexProvider(
        metadata={"codex_desktop": {"account_id": "oauth-account-id"}},
        tenant_id="tenant-test",
        owner_user_id="user-test",
    )

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in provider.stream_generate_events(_request())]

    events = asyncio.run(collect())

    assert events == [{"type": "delta", "text": "OAuth account."}]
    assert len(oauth_calls) == 1
    assert oauth_calls[0]["tenant_id"] == "tenant-test"
    assert oauth_calls[0]["actor_user_id"] == "user-test"
    assert oauth_calls[0]["account_id"] == "oauth-account-id"


def test_local_codex_execution_rejects_a_different_provider_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="memory://")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: True)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "stream_responses",
        lambda **_kwargs: pytest.fail("owner mismatch must be rejected before starting Codex"),
    )
    provider = OpenAICodexProvider(
        metadata={"codex_desktop": {"account_id": "account-test"}},
        tenant_id="tenant-test",
        owner_user_id="different-user",
    )

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in provider.stream_generate_events(_request())]

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(collect())

    assert error.value.status_code == 403


def test_codex_health_is_degraded_when_no_local_account_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_module, "get_settings", lambda: SimpleNamespace(redis_url="memory://")
    )
    monkeypatch.setattr(codex_module, "local_codex_cli_backend_enabled", lambda _url: True)
    monkeypatch.setattr(
        codex_module.codex_cli_runtime,
        "matches_current_cli_account",
        lambda _account_id: False,
    )
    monkeypatch.setattr(
        codex_module.codex_chatgpt_oauth_runtime,
        "has_account",
        lambda **_kwargs: False,
    )
    provider = OpenAICodexProvider(
        metadata={"codex_desktop": {"account_id": "account-test"}},
        tenant_id="tenant-test",
        owner_user_id="user-test",
    )

    status = provider.health_check()

    assert status.status == "degraded"
    assert status.error_code == "codex_account_unavailable"
