from __future__ import annotations

import hashlib
import json
import re
import secrets
import string
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from typing import Any, Final, Literal

from app.core.lazy_import import lazy_import
from app.providers.services.anthropic_provider import AnthropicProvider
from app.providers.services.base import ProviderCapabilityError, ProviderRequestError
from app.providers.services.context_window import (
    extract_context_window,
    extract_max_output_tokens,
    resolve_verified_context_window,
)
from app.providers.services.google_provider import GoogleProvider
from app.providers.services.openai_compatible import OpenAICompatibleProvider
from app.providers.services.reasoning_capabilities import reasoning_capabilities
from app.providers.services.tool_definitions import unique_tool_definitions
from app.providers.services.types import (
    ChatGenerateRequest,
    ChatGenerateResponse,
    HealthCheckResult,
    ProviderModelInfo,
)
from app.providers.services.url_resolution import resolve_provider_base_url


class OpenCodeGoProvider:
    """OpenCode Go cloud inference provider using the new inference API."""

    provider_name = "opencode-zen"
    supports_tool_calling = True
    DEFAULT_BASE_URL = "https://opencode.ai/inference/v1"
    _MODEL_DISCOVERY_ATTEMPTS = 3
    _MODEL_DISCOVERY_RETRY_DELAYS = (0.25, 0.5)

    # OpenCode Go routes models through OpenAI-compatible chat completions
    _GPT_MODEL_PREFIXES: Final[tuple[str, ...]] = ("gpt-", "muse-", "grok-")
    _CLAUDE_MODEL_PREFIXES: Final[tuple[str, ...]] = ("claude-", "qwen")
    _GEMINI_MODEL_PREFIXES: Final[tuple[str, ...]] = ("gemini-",)
    _OPENAI_COMPATIBLE_MODEL_PREFIXES: Final[tuple[str, ...]] = (
        "minimax",
        "glm",
        "kimi",
        "big-pickle",
        "ling",
        "hy3",
        "nemotron",
    )
    _MODEL_NAME_KEYS: Final[tuple[str, ...]] = (
        "id",
        "modelKey",
        "model_key",
        "name",
        "displayName",
        "display_name",
    )
    _MODEL_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
        "data",
        "models",
        "loaded_models",
        "items",
    )
    _CONTEXT_WINDOW_KEYS: Final[tuple[str, ...]] = (
        "context_window",
        "context_length",
        "contextWindow",
        "contextLength",
        "max_context_length",
        "maxContextLength",
        "max_input_tokens",
        "maxInputTokens",
        "input_token_limit",
        "inputTokenLimit",
        "n_ctx",
        "max_model_len",
        "maxModelLen",
    )
    BASE62_CHARS: Final[str] = string.digits + string.ascii_uppercase + string.ascii_lowercase
    OPENCODE_SESSION_RE: Final[re.Pattern[str]] = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
    OPENCODE_REQUEST_RE: Final[re.Pattern[str]] = re.compile(r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")
    OPENCODE_UA: Final[str] = "opencode/1.18.31"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = resolve_provider_base_url(
            base_url or self.DEFAULT_BASE_URL,
            provider_type=self.provider_name,
        )
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")
        self.api_key = api_key
        self.session_id = self.generate_session_id()

    def bind(self, base_url: str, api_key: str | None = None) -> OpenCodeGoProvider:
        resolved = resolve_provider_base_url(base_url, provider_type=self.provider_name)
        self.base_url = (resolved or base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        return self

    @staticmethod
    def is_free_model(model_name: str | None) -> bool:
        return bool(model_name and "free" in model_name.strip().lower())

    @classmethod
    def generate_session_id(cls, timestamp: int | None = None) -> str:
        now_ms = int(time.time() * 1000) if timestamp is None else timestamp
        current = now_ms * 0x1000 + 1
        value = ~current
        time_hex = "".join(f"{((value >> (40 - 8 * i)) & 0xFF):02x}" for i in range(6))
        random_part = "".join(secrets.choice(cls.BASE62_CHARS) for _ in range(14))
        return f"ses_{time_hex}{random_part}"

    @classmethod
    def generate_request_id(cls, timestamp: int | None = None) -> str:
        now_ms = int(time.time() * 1000) if timestamp is None else timestamp
        current = now_ms * 0x1000 + 1
        time_hex = "".join(f"{((current >> (40 - 8 * i)) & 0xFF):02x}" for i in range(6))
        random_part = "".join(secrets.choice(cls.BASE62_CHARS) for _ in range(14))
        return f"msg_{time_hex}{random_part}"

    @classmethod
    def translate_session_id(cls, session_id: str | None) -> str:
        if session_id and cls.OPENCODE_SESSION_RE.match(session_id.strip()):
            return session_id.strip()
        raw = (session_id or "").strip()
        digest = hashlib.sha256(f"opencode\0averqel\0{raw}".encode()).digest()
        random_part = "".join(cls.BASE62_CHARS[value % 62] for value in digest[6:20])
        return f"ses_{digest[:6].hex()}{random_part}"

    @classmethod
    def _normalize_user_agent(cls, user_agent: str | None) -> str:
        if not user_agent:
            return cls.OPENCODE_UA
        match = re.search(r"opencode/(\d+)\.(\d+)(?:\.(\d+))?", user_agent, re.IGNORECASE)
        if not match:
            return cls.OPENCODE_UA
        major = int(match.group(1))
        minor = int(match.group(2))
        return user_agent if major > 1 or (major == 1 and minor >= 17) else cls.OPENCODE_UA

    @staticmethod
    def model_supports_reasoning(model_name: str) -> bool:
        return True

    def _httpx(self, request: ChatGenerateRequest | None = None) -> Any:
        if request is not None:
            injected = request.metadata.get("httpx_module")
            if injected is not None:
                return injected
        return lazy_import("httpx")

    def _headers(
        self,
        api_key: str | None,
        *,
        session_id: str | None = None,
        model: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, str]:
        is_free = self.is_free_model(model)
        token = "public" if is_free and (not api_key or api_key == "public") else api_key
        if not token and is_free:
            token = "public"
        if not token:
            raise ProviderRequestError(
                provider_name=self.provider_name,
                status_code=401,
                message="OpenCode Go API key is required. Get your key from https://opencode.ai/console",
            )
        # OpenCode Go uses oc_sk_... keys with Bearer auth
        auth_header = "Bearer public" if is_free else f"Bearer {token}"
        canonical_session = self.translate_session_id(session_id or self.session_id)
        ua = self._normalize_user_agent(user_agent)

        return {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "User-Agent": ua,
            "x-opencode-client": "desktop",
            "x-opencode-session": canonical_session,
            "x-opencode-request": self.generate_request_id(),
            "x-opencode-project": "global",
            "X-Session-ID": canonical_session,
        }

    def _request_headers(self, request: ChatGenerateRequest) -> dict[str, str]:
        session_id = str(request.metadata.get("conversation_id") or "").strip() or None
        extra = request.metadata.get("extra_headers") or {}
        user_agent = extra.get("User-Agent") or extra.get("user-agent")
        return self._headers(
            request.api_key,
            session_id=session_id,
            model=request.model,
            user_agent=user_agent,
        )

    @staticmethod
    def _extract_provider_error_message(
        *, payload: dict[str, Any] | None = None, text: str | None = None
    ) -> str | None:
        message: str | None = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = error.get("message")
                if isinstance(detail, str) and detail.strip():
                    message = detail.strip()
            detail = payload.get("message")
            if not message and isinstance(detail, str) and detail.strip():
                message = detail.strip()
        if not message:
            if isinstance(text, str) and text.strip():
                message = text.strip()
        return message

    @classmethod
    def _raise_provider_error(
        cls,
        response: Any,
        *,
        payload: dict[str, Any] | None = None,
        text: str | None = None,
    ) -> None:
        if payload is None and text is None:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = None
            try:
                response_text = getattr(response, "text", None)
            except Exception:  # noqa: BLE001
                response_text = None
            if isinstance(response_text, str):
                text = response_text
        raise ProviderRequestError(
            provider_name="opencode-zen",
            status_code=int(response.status_code),
            message=cls._extract_provider_error_message(payload=payload, text=text),
        )

    @classmethod
    def _model_family(
        cls,
        model_name: str,
    ) -> Literal["responses", "anthropic", "google", "openai-compatible"]:
        lowered = model_name.strip().lower()
        if any(lowered.startswith(prefix) for prefix in cls._GPT_MODEL_PREFIXES):
            return "responses"
        if any(lowered.startswith(prefix) for prefix in cls._CLAUDE_MODEL_PREFIXES):
            return "anthropic"
        if any(lowered.startswith(prefix) for prefix in cls._GEMINI_MODEL_PREFIXES):
            return "google"
        return "openai-compatible"

    def _resolve_base_url(self, base_url: str | None = None) -> str:
        resolved = resolve_provider_base_url(
            base_url or self.base_url,
            provider_type=self.provider_name,
        )
        if not resolved:
            raise ProviderCapabilityError("OpenCode Go provider requires a configured base URL")
        return resolved.rstrip("/")

    @staticmethod
    def _is_retryable_model_discovery_error(httpx_module: Any, exc: Exception) -> bool:
        retryable_types = tuple(
            error_type
            for name in (
                "ConnectError",
                "TimeoutException",
                "NetworkError",
                "RemoteProtocolError",
            )
            if isinstance(error_type := getattr(httpx_module, name, None), type)
        )
        return bool(retryable_types) and isinstance(exc, retryable_types)

    def _get_model_discovery_response(self, *, base_url: str, httpx_module: Any) -> Any:
        for attempt in range(3):
            try:
                return httpx_module.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                    timeout=8.0,
                )
            except Exception as exc:  # noqa: BLE001
                is_last_attempt = attempt == 2
                if is_last_attempt or not self._is_retryable_model_discovery_error(
                    httpx_module, exc
                ):
                    raise
                time.sleep((0.25, 0.5)[attempt])

        raise RuntimeError("OpenCode Go model discovery exhausted its retry budget")

    @classmethod
    def _extract_model_name(cls, item: dict[str, Any]) -> str | None:
        for key in ("id", "modelKey", "model_key", "name", "displayName", "display_name"):
            name = item.get(key)
            if isinstance(name, str) and name.strip():
                return name.strip()
        details = item.get("details")
        if isinstance(details, dict):
            for key in ("id", "modelKey", "model_key", "name", "displayName", "display_name"):
                name = details.get(key)
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None

    @classmethod
    def _extract_context_window(cls, item: dict[str, Any]) -> int | None:
        return extract_context_window(
            item,
            candidate_keys=(
                "context_window",
                "context_length",
                "contextWindow",
                "contextLength",
                "max_context_length",
                "maxContextLength",
                "max_input_tokens",
                "maxInputTokens",
                "input_token_limit",
                "inputTokenLimit",
                "n_ctx",
                "max_model_len",
                "maxModelLen",
            ),
        )

    @classmethod
    def _extract_model_items(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "models", "loaded_models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(
            isinstance(payload.get(key), str | int | float | bool)
            for key in ("id", "modelKey", "model_key", "name", "displayName", "display_name")
        ):
            return [payload]
        return []

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                nested = item.get("content")
                if isinstance(nested, str):
                    parts.append(nested)
            return "".join(parts)
        return ""

    @classmethod
    def _extract_response_text(cls, payload: dict[str, Any]) -> str:
        # OpenAI-compatible response format
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str) and content.strip():
                            return content.strip()
        # Responses API format
        for key in ("output_text", "text", "content"):
            text = payload.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").lower()
                text = item.get("text") if isinstance(item.get("text"), str) else None
                if not text and isinstance(item.get("content"), list):
                    text = "".join(
                        str(part.get("text", ""))
                        for part in item["content"]
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                if item_type in {"message", "assistant_message", "output_text", "text"}:
                    if text and text.strip():
                        parts.append(text.strip())
            if parts:
                return "".join(parts).strip()
        return ""

    @classmethod
    def _extract_response_thinking(cls, payload: dict[str, Any]) -> str | None:
        # Check for reasoning content in OpenAI-compatible format
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        reasoning = message.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning.strip():
                            return reasoning.strip()
        # Responses API format
        for key in (
            "reasoning_content",
            "reasoning",
            "thinking",
            "reasoning_summary",
            "reasoning_summary_text",
        ):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list):
                parts = []
                for item in val:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                if parts:
                    return "".join(parts).strip()
        return None

    @classmethod
    def _normalize_arguments(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return str(value)

    @classmethod
    def _extract_response_tool_calls(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        tool_calls: list[dict[str, Any]] = []

        def append_from_item(item: dict[str, Any]) -> None:
            item_type = str(item.get("type") or "").lower()
            if "function_call" not in item_type and "tool_call" not in item_type:
                return
            name = item.get("name") or item.get("function_name") or item.get("tool_name")
            call_id = item.get("id") or item.get("call_id") or item.get("item_id")
            arguments = item.get("arguments")
            if arguments is None:
                arguments = item.get("input") or item.get("parameters")
            if not isinstance(name, str) or not name.strip():
                return
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = f"call_{len(tool_calls)}"
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name.strip(),
                        "arguments": cls._normalize_arguments(arguments),
                    },
                }
            )

        # OpenAI-compatible format
        raw_tool_calls = payload.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for item in raw_tool_calls:
                if isinstance(item, dict):
                    append_from_item(item)

        # Responses API format
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    append_from_item(item)
        return tool_calls

    @classmethod
    def _convert_messages_to_chat(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Convert messages to OpenAI-compatible format."""
        converted: list[dict[str, Any]] = []
        system_parts: list[str] = []

        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content", "")

            if role == "system":
                text = (
                    content if isinstance(content, str) else cls._extract_text_from_content(content)
                )
                if text.strip():
                    system_parts.append(text.strip())
                continue

            if role == "tool":
                call_id = message.get("tool_call_id") or message.get("id") or message.get("call_id")
                if not call_id or not str(call_id).strip():
                    continue
                output = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False) if content is not None else ""
                )
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call_id).strip(),
                        "content": output,
                    }
                )
                continue

            if role == "assistant":
                text = (
                    content if isinstance(content, str) else cls._extract_text_from_content(content)
                )
                if text.strip():
                    converted.append({"role": "assistant", "content": text})
                raw_tool_calls = message.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    for call in raw_tool_calls:
                        if not isinstance(call, dict):
                            continue
                        call_id = call.get("id") or call.get("call_id")
                        function = call.get("function")
                        source = function if isinstance(function, dict) else call
                        name = (
                            source.get("name")
                            or source.get("function_name")
                            or source.get("tool_name")
                        )
                        if not isinstance(name, str) or not name.strip():
                            continue
                        arguments = source.get("arguments")
                        if arguments is None:
                            arguments = source.get("input") or source.get("parameters")
                        call_id_str = (
                            str(call_id).strip()
                            if call_id and str(call_id).strip()
                            else f"call_{len(converted)}"
                        )
                        converted.append(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": call_id_str,
                                        "type": "function",
                                        "function": {
                                            "name": name.strip(),
                                            "arguments": cls._normalize_arguments(arguments),
                                        },
                                    }
                                ],
                            }
                        )
                raw_fn_call = message.get("function_call")
                if isinstance(raw_fn_call, dict) and not raw_tool_calls:
                    name = (
                        raw_fn_call.get("name")
                        or raw_fn_call.get("function_name")
                        or raw_fn_call.get("tool_name")
                    )
                    if isinstance(name, str) and name.strip():
                        call_id = (
                            message.get("tool_call_id")
                            or message.get("id")
                            or message.get("call_id")
                            or raw_fn_call.get("id")
                        )
                        call_id_str = (
                            str(call_id).strip()
                            if call_id and str(call_id).strip()
                            else f"call_{len(converted)}"
                        )
                        arguments = raw_fn_call.get("arguments")
                        if arguments is None:
                            arguments = raw_fn_call.get("input") or raw_fn_call.get("parameters")
                        converted.append(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": call_id_str,
                                        "type": "function",
                                        "function": {
                                            "name": name.strip(),
                                            "arguments": cls._normalize_arguments(arguments),
                                        },
                                    }
                                ],
                            }
                        )
                continue

            text = (
                content
                if isinstance(content, str)
                else (
                    cls._extract_text_from_content(content)
                    if isinstance(content, list)
                    else json.dumps(content, ensure_ascii=False)
                )
            )
            if not text.strip():
                continue
            converted.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "content": text,
                }
            )

        system_prompt = "\n\n".join(part for part in system_parts if part).strip() or None
        if system_prompt:
            converted.insert(0, {"role": "system", "content": system_prompt})

        return converted, None

    @classmethod
    def _convert_messages_to_input(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert shared chat messages to the OpenCode Responses input shape."""
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            if role == "system":
                text = (
                    content if isinstance(content, str) else cls._extract_text_from_content(content)
                )
                if text.strip():
                    instructions.append(text.strip())
                continue
            if role == "tool":
                call_id = message.get("tool_call_id") or message.get("id") or message.get("call_id")
                if not call_id or not str(call_id).strip():
                    continue
                output = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False) if content is not None else ""
                )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call_id).strip(),
                        "output": output,
                    }
                )
                continue

            text = content if isinstance(content, str) else cls._extract_text_from_content(content)
            if text.strip():
                input_items.append(
                    {"role": "assistant" if role == "assistant" else "user", "content": text}
                )

            if role != "assistant":
                continue
            raw_tool_calls = message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for call in raw_tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    source = function if isinstance(function, dict) else call
                    name = (
                        source.get("name") or source.get("function_name") or source.get("tool_name")
                    )
                    if not isinstance(name, str) or not name.strip():
                        continue
                    call_id = call.get("id") or call.get("call_id")
                    call_id_str = (
                        str(call_id).strip()
                        if call_id and str(call_id).strip()
                        else f"call_{len(input_items)}"
                    )
                    arguments = source.get("arguments")
                    if arguments is None:
                        arguments = source.get("input") or source.get("parameters")
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id_str,
                            "name": name.strip(),
                            "arguments": cls._normalize_arguments(arguments),
                        }
                    )
            raw_function_call = message.get("function_call")
            if isinstance(raw_function_call, dict) and not raw_tool_calls:
                name = (
                    raw_function_call.get("name")
                    or raw_function_call.get("function_name")
                    or raw_function_call.get("tool_name")
                )
                if isinstance(name, str) and name.strip():
                    call_id = (
                        message.get("tool_call_id")
                        or message.get("id")
                        or message.get("call_id")
                        or raw_function_call.get("id")
                    )
                    call_id_str = (
                        str(call_id).strip()
                        if call_id and str(call_id).strip()
                        else f"call_{len(input_items)}"
                    )
                    arguments = raw_function_call.get("arguments")
                    if arguments is None:
                        arguments = raw_function_call.get("input") or raw_function_call.get(
                            "parameters"
                        )
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id_str,
                            "name": name.strip(),
                            "arguments": cls._normalize_arguments(arguments),
                        }
                    )
        return "\n\n".join(instructions).strip() or None, input_items

    @classmethod
    def _convert_tools_to_responses(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return cls._convert_tools_to_openai(tools)

    @classmethod
    def _build_responses_payload(
        cls,
        request: ChatGenerateRequest,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        instructions, input_items = cls._convert_messages_to_input(request.messages)
        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_tokens is not None:
            payload["max_output_tokens"] = request.max_tokens
        if instructions:
            payload["instructions"] = instructions
        tools = unique_tool_definitions(request.tools)
        if tools:
            payload["tools"] = cls._convert_tools_to_responses(tools)
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice
        if (
            request.reasoning_enabled
            and request.tool_choice != "required"
            and cls.model_supports_reasoning(request.model)
        ):
            payload["reasoning"] = {"effort": request.reasoning_effort or "medium"}
        return payload

    @classmethod
    def _convert_tools_to_openai(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate the shared chat-tool shape to OpenAI-compatible format."""
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function")
            source = function if isinstance(function, dict) else tool
            name = source.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            response_tool: dict[str, Any] = {
                "type": "function",
                "name": name.strip(),
            }
            for key in ("description", "parameters", "strict"):
                value = source.get(key)
                if value is not None:
                    response_tool[key] = value
            converted.append(response_tool)
        return converted

    def _build_chat_payload(self, request: ChatGenerateRequest) -> dict[str, Any]:
        messages, _ = self._convert_messages_to_chat(request.messages)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        tools = unique_tool_definitions(request.tools)
        if tools:
            payload["tools"] = self._convert_tools_to_openai(tools)
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice
        if request.reasoning_enabled and request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        return payload

    async def _stream_chat_events(
        self,
        request: ChatGenerateRequest,
        *,
        base_url: str,
    ) -> AsyncIterator[dict[str, Any]]:
        httpx_module = self._httpx(request)
        payload = self._build_chat_payload(request)
        timeout = httpx_module.Timeout(
            timeout=float(request.metadata.get("timeout_seconds", 8.0)),
            read=float(request.metadata.get("read_timeout_seconds", 300.0)),
        )
        tool_call_index_by_id: dict[str, int] = {}
        tool_call_name_by_id: dict[str, str] = {}
        tool_call_args_by_id: dict[str, list[str]] = {}

        async with httpx_module.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=self._request_headers(request),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    error_payload: dict[str, Any] | None = None
                    error_text: str | None = None
                    try:
                        error_bytes = await response.aread()
                    except Exception:  # noqa: BLE001
                        error_bytes = None
                    if isinstance(error_bytes, bytes | bytearray):
                        error_text = bytes(error_bytes).decode("utf-8", errors="replace")
                        try:
                            decoded = json.loads(error_text)
                        except json.JSONDecodeError:
                            decoded = None
                        if isinstance(decoded, dict):
                            error_payload = decoded
                    self._raise_provider_error(response, payload=error_payload, text=error_text)

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line.split(":", 1)[1].strip()
                    if data == "[DONE]":
                        return
                    try:
                        payload_obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = payload_obj.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue

                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue

                    # Content delta
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield {"type": "delta", "text": content}

                    # Reasoning delta
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield {"type": "thinking", "text": reasoning}

                    # Tool calls
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            index = tc.get("index")
                            if not isinstance(index, int):
                                continue
                            tc_id = tc.get("id")
                            if isinstance(tc_id, str) and tc_id:
                                if tc_id not in tool_call_index_by_id:
                                    tool_call_index_by_id[tc_id] = index
                                function = tc.get("function")
                                if isinstance(function, dict):
                                    name = function.get("name")
                                    if isinstance(name, str) and name:
                                        tool_call_name_by_id[tc_id] = name
                                    arguments = function.get("arguments")
                                    if isinstance(arguments, str):
                                        tool_call_args_by_id.setdefault(tc_id, []).append(arguments)
                                    elif isinstance(arguments, dict):
                                        tool_call_args_by_id.setdefault(tc_id, []).append(
                                            json.dumps(arguments)
                                        )

                    finish_reason = choice.get("finish_reason")
                    if finish_reason == "tool_calls":
                        for tc_id in tool_call_name_by_id:
                            name = tool_call_name_by_id[tc_id]
                            args = "".join(tool_call_args_by_id.get(tc_id, []))
                            yield {
                                "type": "tool_calls_delta",
                                "tool_calls": [
                                    {
                                        "index": tool_call_index_by_id.get(tc_id, 0),
                                        "id": tc_id,
                                        "function": {
                                            "name": name,
                                            "arguments": self._normalize_arguments(args or "{}"),
                                        },
                                    }
                                ],
                            }

                    if finish_reason in ("stop", "length", "tool_calls", "content_filter"):
                        yield {"type": "completed"}
                        return

    async def _stream_responses_events(
        self,
        request: ChatGenerateRequest,
        *,
        base_url: str,
    ) -> AsyncIterator[dict[str, Any]]:
        httpx_module = self._httpx(request)
        payload = self._build_responses_payload(request, stream=True)
        timeout = httpx_module.Timeout(
            timeout=float(request.metadata.get("timeout_seconds", 8.0)),
            read=float(request.metadata.get("read_timeout_seconds", 300.0)),
        )
        tool_names: dict[str, str] = {}
        tool_indices: dict[str, int] = {}
        async with httpx_module.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url}/responses",
                headers=self._request_headers(request),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    self._raise_provider_error(response)
                current_event = ""
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip().lower()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line.split(":", 1)[1].strip()
                    if data == "[DONE]":
                        return
                    try:
                        item = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event_name = current_event or str(item.get("type") or "").lower()
                    if "output_text.delta" in event_name:
                        delta = item.get("delta") or item.get("text")
                        if isinstance(delta, str) and delta:
                            yield {"type": "delta", "text": delta}
                    elif "reasoning" in event_name or "think" in event_name:
                        delta = item.get("delta") or item.get("text") or item.get("summary")
                        if isinstance(delta, str) and delta:
                            yield {"type": "thinking", "text": delta}
                    elif "output_item.added" in event_name or "item.added" in event_name:
                        output_item = item.get("item") or item.get("output_item") or item
                        if isinstance(output_item, dict) and "function_call" in str(
                            output_item.get("type", "")
                        ):
                            call_id = str(
                                output_item.get("id")
                                or output_item.get("call_id")
                                or f"call_{len(tool_indices)}"
                            )
                            tool_indices.setdefault(call_id, len(tool_indices))
                            name = output_item.get("name") or output_item.get("function_name")
                            if isinstance(name, str):
                                tool_names[call_id] = name
                    elif (
                        "function_call_arguments.delta" in event_name
                        or "arguments.delta" in event_name
                    ):
                        call_id = str(
                            item.get("item_id")
                            or item.get("call_id")
                            or f"call_{len(tool_indices)}"
                        )
                        tool_indices.setdefault(call_id, len(tool_indices))
                        arguments = item.get("delta") or item.get("arguments")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        if isinstance(arguments, str) and arguments:
                            yield {
                                "type": "tool_calls_delta",
                                "tool_calls": [
                                    {
                                        "index": tool_indices[call_id],
                                        "id": call_id,
                                        "function": {
                                            "name": tool_names.get(call_id, ""),
                                            "arguments": arguments,
                                        },
                                    }
                                ],
                            }
                    current_event = ""

    def _chat_response(
        self, request: ChatGenerateRequest, *, base_url: str
    ) -> ChatGenerateResponse:
        httpx_module = self._httpx(request)
        payload = self._build_chat_payload(request)
        payload["stream"] = False
        timeout = httpx_module.Timeout(
            timeout=float(request.metadata.get("timeout_seconds", 8.0)),
            read=float(request.metadata.get("read_timeout_seconds", 300.0)),
        )

        with httpx_module.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers=self._request_headers(request),
                json=payload,
            )
            if response.status_code >= 400:
                self._raise_provider_error(response)
            payload_obj = response.json()

            content = self._extract_response_text(payload_obj)
            thinking_content = self._extract_response_thinking(payload_obj)
            tool_calls = self._extract_response_tool_calls(payload_obj)
            usage = payload_obj.get("usage", {})
            return ChatGenerateResponse(
                content=content,
                thinking_content=thinking_content,
                tool_calls=tool_calls if tool_calls else None,
                usage=usage if isinstance(usage, dict) else {},
            )

    def _family_provider(self, request: ChatGenerateRequest, *, base_url: str) -> tuple[
        str,
        AnthropicProvider | GoogleProvider | OpenAICompatibleProvider | None,
    ]:
        family = self._model_family(request.model)
        if family == "anthropic":
            return family, AnthropicProvider().bind(base_url, request.api_key)
        if family == "google":
            return family, GoogleProvider().bind(base_url, request.api_key)
        if family == "responses":
            return family, None
        return family, OpenAICompatibleProvider(base_url=base_url, api_key=request.api_key)

    def generate(self, request: ChatGenerateRequest) -> ChatGenerateResponse:
        base_url = self._resolve_base_url(request.base_url)
        family, provider = self._family_provider(request, base_url=base_url)
        if family == "responses":
            return self._chat_response(request, base_url=base_url)
        if family == "openai-compatible":
            return self._chat_response(request, base_url=base_url)
        if provider is None:
            raise ProviderCapabilityError("OpenCode Go provider routing failed")
        headers = self._request_headers(request)
        api_key = (
            headers["Authorization"].split(" ", 1)[1]
            if "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
            else request.api_key
        )
        adapted_request = replace(
            request,
            api_key=api_key,
            base_url=base_url,
            metadata={
                **dict(request.metadata),
                "provider_type": self.provider_name,
                "extra_headers": {
                    **dict(request.metadata.get("extra_headers") or {}),
                    **headers,
                },
            },
        )
        return provider.generate(adapted_request)

    async def stream_generate(self, request: ChatGenerateRequest) -> AsyncIterator[str]:
        async for event in self.stream_generate_events(request):
            if event.get("type") == "delta" and isinstance(event.get("text"), str):
                yield event["text"]

    async def stream_generate_events(
        self, request: ChatGenerateRequest
    ) -> AsyncIterator[dict[str, Any]]:
        base_url = self._resolve_base_url(request.base_url)
        family, provider = self._family_provider(request, base_url=base_url)
        if family == "responses":
            async for event in self._stream_responses_events(request, base_url=base_url):
                yield event
            return
        if family == "openai-compatible":
            async for event in self._stream_chat_events(request, base_url=base_url):
                yield event
            return
        if provider is None:
            raise ProviderCapabilityError("OpenCode Go provider routing failed")
        headers = self._request_headers(request)
        api_key = (
            headers["Authorization"].split(" ", 1)[1]
            if "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
            else request.api_key
        )
        adapted_request = replace(
            request,
            api_key=api_key,
            base_url=base_url,
            metadata={
                **dict(request.metadata),
                "provider_type": self.provider_name,
                "extra_headers": {
                    **dict(request.metadata.get("extra_headers") or {}),
                    **headers,
                },
            },
        )
        async for event in provider.stream_generate_events(adapted_request):
            yield event

    def stream_generate_sync(self, request: ChatGenerateRequest) -> Iterator[str]:
        base_url = self._resolve_base_url(request.base_url)
        family, provider = self._family_provider(request, base_url=base_url)
        headers = self._request_headers(request)
        api_key = (
            headers["Authorization"].split(" ", 1)[1]
            if "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
            else request.api_key
        )
        adapted_request = replace(
            request,
            api_key=api_key,
            base_url=base_url,
            metadata={
                **dict(request.metadata),
                "provider_type": self.provider_name,
                "extra_headers": {
                    **dict(request.metadata.get("extra_headers") or {}),
                    **headers,
                },
            },
        )
        if family in {"responses", "openai-compatible"}:
            result = self._chat_response(request, base_url=base_url)
            if result.content:
                yield from self._chunk_text(result.content)
            return
        if provider is None:
            raise ProviderCapabilityError("OpenCode Go provider routing failed")
        yield from provider.stream_generate_sync(adapted_request)

    def list_models(self) -> Sequence[ProviderModelInfo]:
        base_url = self._resolve_base_url()
        httpx_module = self._httpx()
        response = self._get_model_discovery_response(
            base_url=base_url,
            httpx_module=httpx_module,
        )
        if response.status_code >= 400:
            self._raise_provider_error(response)
        payload_obj: dict[str, Any] = response.json()
        discovered = self._extract_model_items(payload_obj)
        infos: list[ProviderModelInfo] = []
        for item in discovered:
            model_name = self._extract_model_name(item)
            if not model_name:
                continue
            live_context_window = self._extract_context_window(item)
            max_output_tokens = extract_max_output_tokens(item)
            verified_context_window = resolve_verified_context_window(
                model_name,
                provider_type=self.provider_name,
            )
            context_window = live_context_window or verified_context_window.context_window
            context_window_source = (
                "live_model" if live_context_window is not None else verified_context_window.source
            )
            infos.append(
                ProviderModelInfo(
                    name=model_name,
                    kind="chat",
                    context_window=context_window,
                    context_window_source=context_window_source,
                    max_output_tokens=max_output_tokens,
                    display_name=(
                        item.get("display_name")
                        if isinstance(item.get("display_name"), str)
                        else (
                            item.get("displayName")
                            if isinstance(item.get("displayName"), str)
                            else model_name
                        )
                    ),
                    capabilities={
                        "runtime": self.provider_name,
                        "family": self._model_family(model_name),
                        **(
                            {"context_window_source": context_window_source}
                            if context_window_source
                            else {}
                        ),
                        **(
                            {"max_output_tokens": max_output_tokens}
                            if max_output_tokens is not None
                            else {}
                        ),
                        "endpoint": f"{self.base_url}/chat/completions",
                        **reasoning_capabilities(
                            self.provider_name, model_name, base_url=self.base_url
                        ),
                    },
                )
            )
        return infos

    def list_embedding_models(self) -> Sequence[ProviderModelInfo]:
        return []

    def list_reranker_models(self) -> Sequence[ProviderModelInfo]:
        return []

    def health_check(self) -> HealthCheckResult:
        start = time.monotonic()
        try:
            models = self.list_models()
        except Exception as exc:  # noqa: BLE001
            return HealthCheckResult(
                status="unhealthy",
                latency_ms=int((time.monotonic() - start) * 1000),
                error_code="provider_health_failed",
                error_message_redacted=str(exc),
            )
        return HealthCheckResult(
            status="healthy" if models else "degraded",
            latency_ms=int((time.monotonic() - start) * 1000),
            metadata={"result_count": len(models)},
        )

    @staticmethod
    def _chunk_text(text: str, *, size: int = 48) -> Sequence[str]:
        normalized = text.replace("\r\n", "\n")
        chunks: list[str] = []
        cursor = 0
        while cursor < len(normalized):
            end = min(len(normalized), cursor + size)
            chunk = normalized[cursor:end]
            if chunk:
                chunks.append(chunk)
            cursor = end
        return chunks


# Preserve the established import and provider class name for existing callers.
OpenCodeZenProvider = OpenCodeGoProvider
