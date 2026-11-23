from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY
from app.providers.services.anthropic_provider import AnthropicProvider
from app.providers.services.base import ProviderRequestError
from app.providers.services.google_provider import GoogleProvider
from app.providers.services.openai_codex_provider import OpenAICodexProvider
from app.providers.services.openai_compatible import OpenAICompatibleProvider
from app.providers.services.opencode_zen_provider import OpenCodeZenProvider
from app.providers.services.types import ChatGenerateRequest

TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def _request(**overrides: Any) -> ChatGenerateRequest:
    values: dict[str, Any] = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Use tools when needed."},
            {"role": "user", "content": "Find current news."},
        ],
        "temperature": 0,
        "max_tokens": 256,
        "base_url": "https://provider.example/v1",
        "api_key": "test-key",
        "tools": [TOOL],
        "tool_choice": "required",
    }
    values.update(overrides)
    return ChatGenerateRequest(**values)


def test_google_builds_native_function_declarations() -> None:
    payload = GoogleProvider._build_payload(_request())

    declaration = payload["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "web_search"
    assert declaration["parameters"]["required"] == ["query"]
    assert declaration["parameters"]["type"] == "OBJECT"
    assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert payload["systemInstruction"]["parts"][0]["text"] == "Use tools when needed."


def test_google_deduplicates_function_declarations_before_transport() -> None:
    duplicate = {**TOOL, "function": {**TOOL["function"], "description": "duplicate"}}

    payload = GoogleProvider._build_payload(_request(tools=[TOOL, duplicate]))

    declarations = payload["tools"][0]["functionDeclarations"]
    assert [item["name"] for item in declarations] == ["web_search"]


@pytest.mark.unit_no_db
def test_google_converts_attached_image_to_mime_aware_inline_data() -> None:
    payload = GoogleProvider._build_payload(_request(images=["data:image/png;base64,iVBORw0KGgo="]))

    assert payload["contents"][-1]["parts"][-1] == {
        "inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}
    }


def test_google_strips_openai_only_schema_keywords_recursively() -> None:
    request = _request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "todo_write",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "content": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 1000,
                                        }
                                    },
                                },
                            }
                        },
                    },
                },
            }
        ]
    )

    parameters = GoogleProvider._build_payload(request)["tools"][0]["functionDeclarations"][0][
        "parameters"
    ]
    assert parameters["type"] == "OBJECT"
    assert "additionalProperties" not in parameters
    assert parameters["properties"]["tasks"]["type"] == "ARRAY"
    assert parameters["properties"]["tasks"]["items"]["type"] == "OBJECT"
    assert "additionalProperties" not in parameters["properties"]["tasks"]["items"]
    assert "minLength" not in parameters["properties"]["tasks"]["items"]["properties"]["content"]


def test_google_stream_error_reads_and_preserves_provider_message() -> None:
    class Response:
        status_code = 400

        def json(self) -> dict[str, Any]:
            raise AssertionError("streaming errors must use the already-read body")

        @property
        def text(self) -> str:
            raise AssertionError("streaming response text must not be accessed before read")

    with pytest.raises(ProviderRequestError, match="Invalid tool schema"):
        GoogleProvider._raise_provider_error(
            Response(), b'{"error":{"message":"Invalid tool schema"}}'
        )


def test_google_converts_tool_history_to_function_response() -> None:
    request = _request(
        messages=[
            {"role": "user", "content": "Find current news."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "thought_signature": "sig-123",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"news"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"results":[]}'},
        ]
    )
    _system, contents = GoogleProvider._build_contents(request.messages)

    assert contents[1]["parts"][0]["thoughtSignature"] == "sig-123"
    assert contents[-1]["parts"][0]["functionResponse"]["name"] == "web_search"
    assert contents[-1]["parts"][0]["functionResponse"]["response"] == {"results": []}


def test_google_extracts_native_function_call() -> None:
    text, thinking, calls, media = GoogleProvider._extract_candidate_parts(
        {
            "content": {
                "parts": [
                    {
                        "functionCall": {
                            "name": "web_search",
                            "args": {"query": "news"},
                        },
                        "thoughtSignature": "sig-123",
                    }
                ]
            }
        }
    )

    assert text == ""
    assert thinking is None
    assert calls[0]["function"]["name"] == "web_search"
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "news"}
    assert calls[0]["thought_signature"] == "sig-123"
    assert media == []


def test_google_extracts_native_inline_media() -> None:
    _text, _thinking, calls, media = GoogleProvider._extract_candidate_parts(
        {
            "content": {
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}},
                ]
            }
        }
    )

    assert calls == []
    assert media == [{"content_type": "image/png", "data_base64": "aGVsbG8="}]


def test_anthropic_translates_tools_and_tool_history() -> None:
    request = _request(
        messages=[
            {"role": "user", "content": "Find current news."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu-1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"news"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu-1", "content": '{"results":[]}'},
        ]
    )
    system, messages = AnthropicProvider._system_and_messages(request.messages)

    assert system is None
    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert messages[2]["content"][0]["tool_use_id"] == "toolu-1"
    assert AnthropicProvider._tools_payload(request)[0]["input_schema"]["required"] == ["query"]


def test_anthropic_deduplicates_function_definitions_before_transport() -> None:
    duplicate = {**TOOL, "function": {**TOOL["function"], "description": "duplicate"}}

    tools = AnthropicProvider._tools_payload(_request(tools=[TOOL, duplicate]))

    assert [item["name"] for item in tools] == ["web_search"]


def test_tool_capable_provider_adapters_preserve_the_complete_native_catalog() -> None:
    definitions = [
        {
            "type": "function",
            "function": {
                "name": f"native_tool_{index:03d}",
                "description": f"Native capability {index}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(148)
    ]
    request = _request(tools=definitions)
    expected_names = [f"native_tool_{index:03d}" for index in range(148)]

    codex_names = [tool["name"] for tool in OpenAICodexProvider._function_tools(definitions)]
    assert codex_names == expected_names
    assert [tool["name"] for tool in AnthropicProvider._tools_payload(request)] == expected_names
    assert [tool["name"] for tool in GoogleProvider._tool_declarations(request)] == expected_names
    assert [
        tool["name"] for tool in OpenCodeZenProvider._convert_tools_to_responses(definitions)
    ] == expected_names
    responses_payload = OpenAICompatibleProvider()._build_responses_payload(
        request,
        stream=False,
    )
    assert [tool["name"] for tool in responses_payload["tools"]] == expected_names

    captured: dict[str, Any] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}}]}

    def post(_url: str, **kwargs: Any) -> Response:
        captured.update(kwargs)
        return Response()

    chat_request = replace(
        request,
        metadata={**request.metadata, "httpx_module": SimpleNamespace(post=post)},
    )
    OpenAICompatibleProvider().generate(chat_request)
    assert [tool["function"]["name"] for tool in captured["json"]["tools"]] == expected_names


def test_tool_capable_adapters_preserve_the_full_native_action_registry() -> None:
    expected_names = sorted(NATIVE_TOOL_REGISTRY.names() - {"discover_tools"})
    definitions = NATIVE_TOOL_REGISTRY.emit_many(expected_names)
    request = _request(tools=definitions)

    assert {"fs_read", "fs_write", "fs_edit", "fs_delete", "fs_glob", "fs_grep"}.issubset(
        expected_names
    )
    assert "url_read" in expected_names
    assert "discover_tools" not in expected_names
    assert [
        tool["name"] for tool in OpenAICodexProvider._function_tools(definitions)
    ] == expected_names
    assert [tool["name"] for tool in AnthropicProvider._tools_payload(request)] == expected_names
    assert [tool["name"] for tool in GoogleProvider._tool_declarations(request)] == expected_names
    assert [
        tool["name"] for tool in OpenCodeZenProvider._convert_tools_to_responses(definitions)
    ] == expected_names
    response_tools = OpenAICompatibleProvider()._build_responses_payload(
        request,
        stream=False,
    )["tools"]
    assert [tool["name"] for tool in response_tools] == expected_names


@pytest.mark.unit_no_db
def test_anthropic_converts_attached_image_to_native_base64_block() -> None:
    _system, messages = AnthropicProvider._system_and_messages(
        _request().messages,
        ["data:image/webp;base64,UklGRg=="],
    )

    assert messages[-1]["content"][-1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/webp",
            "data": "UklGRg==",
        },
    }


def test_anthropic_extracts_tool_use_response() -> None:
    text, thinking, calls = AnthropicProvider._extract_text_blocks(
        [
            {
                "type": "tool_use",
                "id": "toolu-1",
                "name": "web_search",
                "input": {"query": "news"},
            }
        ]
    )

    assert text == ""
    assert thinking is None
    assert calls[0]["id"] == "toolu-1"
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "news"}
