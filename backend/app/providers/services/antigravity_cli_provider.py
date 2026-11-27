from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from app.core.config import get_settings
from app.providers.services.antigravity_cli_runtime import (
    AntigravityCliAccountMismatchError,
    AntigravityCliRuntimeError,
    AntigravityCliUnavailableError,
    antigravity_cli_runtime,
    local_antigravity_cli_backend_enabled,
)
from app.providers.services.base import (
    ChatProvider,
    ProviderCapabilityError,
    ProviderRequestError,
)
from app.providers.services.types import (
    ChatGenerateRequest,
    ChatGenerateResponse,
    HealthCheckResult,
    ProviderModelInfo,
)


class AntigravityCliProvider(ChatProvider):
    """Local Antigravity CLI model access through the Antigravity ACP runtime."""

    provider_name = "antigravity-cli"
    supports_tool_calling = True
    request_timeout_seconds = 300

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        antigravity_settings = self.metadata.get("antigravity_cli")
        self.antigravity_settings: dict[str, Any] = (
            antigravity_settings if isinstance(antigravity_settings, dict) else {}
        )
        self.tenant_id = tenant_id
        self.owner_user_id = owner_user_id

    def list_models(self) -> list[ProviderModelInfo]:
        models = self.antigravity_settings.get("models")
        if not isinstance(models, list) or not models:
            models = self._fetch_models_from_cli()
        result: list[ProviderModelInfo] = []
        seen: set[str] = set()
        for item in models[:500]:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("name")
            if not isinstance(model_id, str) or not model_id or len(model_id) > 255:
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            display_name = item.get("display_name") or item.get("name") or model_id
            capabilities: dict[str, Any] = {"source": "antigravity_cli"}
            result.append(
                ProviderModelInfo(
                    name=model_id,
                    display_name=(
                        display_name[:255]
                        if isinstance(display_name, str) and display_name.strip()
                        else model_id
                    ),
                    kind="chat",
                    capabilities=capabilities,
                )
            )
        return result

    def _fetch_models_from_cli(self) -> list[dict[str, Any]]:
        """Fetch models from the local antigravity CLI."""
        import subprocess

        try:
            result = subprocess.run(
                ["agy", "models", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "ANTIGRAVITY_CLI_PATH": os.environ.get("ANTIGRAVITY_CLI_PATH", ""),
                },
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        # Fallback models if CLI fails
        return [
            {"id": "gemini-4-neon-medium", "name": "Gemini 4 Neon (Medium)"},
            {"id": "gemini-4-carbon-medium", "name": "Gemini 4 Carbon (Medium)"},
            {"id": "gemini-4-argon-medium", "name": "Gemini 4 Argon (Medium)"},
            {"id": "gemini-4-hydrogen-medium", "name": "Gemini 4 Hydrogen (Medium)"},
            {"id": "claude-opus-5-5-medium", "name": "Claude Opus 5.5 (Medium)"},
            {"id": "claude-sonnet-5-5-medium", "name": "Claude Sonnet 5.5 (Medium)"},
            {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
        ]

    def list_embedding_models(self) -> list[ProviderModelInfo]:
        return []

    def list_reranker_models(self) -> list[ProviderModelInfo]:
        return []

    def health_check(self) -> HealthCheckResult:
        if not self.tenant_id or not self.owner_user_id:
            return HealthCheckResult(
                status="unhealthy",
                error_code="antigravity_runtime_identity_missing",
                error_message_redacted="The local provider owner could not be identified.",
            )
        started = time.monotonic()
        settings = get_settings()
        if local_antigravity_cli_backend_enabled(settings.redis_url):
            return HealthCheckResult(
                status="healthy",
                latency_ms=round((time.monotonic() - started) * 1000),
                metadata={"runtime": "local_antigravity_cli", "token_storage": "local"},
            )
        return HealthCheckResult(
            status="degraded",
            error_code="antigravity_account_unavailable",
            error_message_redacted="Antigravity CLI requires the standalone local Kinesis backend.",
            metadata={"runtime": "local_backend"},
        )

    def generate(self, request: ChatGenerateRequest) -> ChatGenerateResponse:
        raise ProviderCapabilityError("Antigravity CLI access requires streamed requests.")

    async def stream_generate(self, request: ChatGenerateRequest) -> AsyncIterator[str]:
        async for event in self.stream_generate_events(request):
            if event.get("type") == "delta" and isinstance(event.get("text"), str):
                yield event["text"]

    def stream_generate_sync(self, request: ChatGenerateRequest) -> Iterator[str]:
        raise ProviderCapabilityError(
            "Antigravity CLI access is available through DeepSpace streaming only."
        )

    async def stream_generate_events(
        self, request: ChatGenerateRequest
    ) -> AsyncIterator[dict[str, Any]]:
        settings = get_settings()
        if not local_antigravity_cli_backend_enabled(settings.redis_url):
            raise ProviderRequestError(
                self.provider_name,
                503,
                "Antigravity CLI requires the standalone local Kinesis backend.",
            )

        tenant_id = str(request.metadata.get("tenant_id") or self.tenant_id or "")
        user_id = str(request.metadata.get("user_id") or self.owner_user_id or "")
        account_id = str(self.antigravity_settings.get("account_id") or "")
        if not tenant_id or not user_id or not account_id:
            raise ProviderRequestError(
                self.provider_name,
                503,
                "Antigravity account details are missing. Reconnect the Antigravity CLI provider.",
            )
        if self.tenant_id != tenant_id or self.owner_user_id != user_id:
            raise ProviderRequestError(
                self.provider_name,
                403,
                "This local Antigravity account is not available to the selected provider owner.",
            )

        payload = self._acp_payload(request)
        try:
            tool_ids: dict[int, str] = {}
            tool_names: dict[int, str] = {}
            accumulated_arguments: dict[int, str] = {}
            async for response_event in antigravity_cli_runtime.stream_responses(
                account_id=account_id,
                payload=payload,
            ):
                for normalized_event in self._normalize_event(
                    response_event,
                    tool_ids=tool_ids,
                    tool_names=tool_names,
                    accumulated_arguments=accumulated_arguments,
                ):
                    yield normalized_event
            return
        except (AntigravityCliAccountMismatchError, AntigravityCliUnavailableError):
            pass
        except AntigravityCliRuntimeError as exc:
            raise ProviderRequestError(
                self.provider_name,
                exc.status_code,
                str(exc),
            ) from exc
        raise ProviderRequestError(
            self.provider_name,
            401,
            "The selected local Antigravity account is no longer connected. Sign in again.",
        )

    def _acp_payload(self, request: ChatGenerateRequest) -> dict[str, Any]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            if role in {"system", "developer"}:
                text = self._text_content(content)
                if text:
                    instructions.append(text)
                continue
            if role == "tool":
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str) and call_id:
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": self._text_content(content),
                        }
                    )
                continue
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                for call in message["tool_calls"][:128]:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    name = function.get("name")
                    call_id = call.get("id")
                    if not isinstance(name, str) or not isinstance(call_id, str):
                        continue
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, separators=(",", ":"))
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
            parts = self._content_parts(content)
            if parts:
                input_items.append(
                    {"role": "assistant" if role == "assistant" else "user", "content": parts}
                )

        images = request.images if isinstance(request.images, list) else []
        for image in images[:10]:
            if image.startswith("https://"):
                image_url = image
            else:
                try:
                    from app.providers.services.image_inputs import image_data_url

                    image_url = image_data_url(image)
                except ValueError as exc:
                    raise ProviderCapabilityError(str(exc)) from exc
            input_items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": image_url}],
                }
            )

        payload: dict[str, Any] = {"model": request.model, "input": input_items}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        reasoning_mode = str(request.metadata.get("reasoning_mode") or "").lower()
        requested_effort = self._normalize_reasoning_effort(request.reasoning_effort)
        effort_map = {
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "very_high": "very_high",
            "extreme_high": "extreme_high",
            "ultra": "ultra",
        }
        if reasoning_mode == "off":
            payload["reasoning"] = {"effort": "none"}
        elif request.reasoning_enabled and request.reasoning_effort:
            if requested_effort is None:
                raise ProviderCapabilityError("This Antigravity reasoning effort is not supported.")
            payload["reasoning"] = {
                "effort": effort_map.get(requested_effort, requested_effort),
                "summary": "auto",
            }

        tool_definitions = self._function_tools(request.tools)
        if tool_definitions:
            payload["tools"] = [
                {
                    "type": "namespace",
                    "name": "averqel",
                    "description": "Tools available to the AverQel agent.",
                    "tools": tool_definitions,
                }
            ]
            if request.tool_choice in {"auto", "required", "none"}:
                payload["tool_choice"] = request.tool_choice
        return payload

    def _normalize_reasoning_effort(self, effort: str | None) -> str | None:
        if not effort:
            return None
        normalized = effort.lower()
        effort_map = {
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "very_high": "very_high",
            "extreme_high": "extreme_high",
            "ultra": "ultra",
        }
        return effort_map.get(normalized, normalized)

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
        return ""

    @staticmethod
    def _content_parts(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "input_text", "text": content}] if content else []
        if not isinstance(content, list):
            text = AntigravityCliProvider._text_content(content)
            return [{"type": "input_text", "text": text}] if text else []
        result: list[dict[str, Any]] = []
        for item in content[:128]:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"text", "input_text"} and isinstance(item.get("text"), str):
                result.append({"type": "input_text", "text": item["text"]})
            elif item_type in {"image_url", "input_image"}:
                image = item.get("image_url")
                image_url = image.get("url") if isinstance(image, dict) else image
                if isinstance(image_url, str) and (
                    image_url.startswith("data:image/") or image_url.startswith("https://")
                ):
                    result.append({"type": "input_image", "image_url": image_url})
        return result

    @staticmethod
    def _function_tools(tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function_value = tool.get("function")
            function = function_value if isinstance(function_value, dict) else tool
            name = function.get("name")
            normalized_name = name.strip() if isinstance(name, str) else ""
            if not normalized_name or len(normalized_name) > 64 or normalized_name in seen:
                continue
            seen.add(normalized_name)
            parameters = function.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            result.append(
                {
                    "type": "function",
                    "name": normalized_name,
                    "description": str(function.get("description") or "")[:1000],
                    "parameters": parameters,
                }
            )
        return result

    @staticmethod
    def _normalize_event(
        event: dict[str, Any],
        *,
        tool_ids: dict[int, str],
        tool_names: dict[int, str],
        accumulated_arguments: dict[int, str],
    ) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            return [{"type": "delta", "text": delta}] if isinstance(delta, str) and delta else []
        if event_type in {
            "response.reasoning.delta",
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
            "response.thought.delta",
        }:
            delta = event.get("delta")
            return [{"type": "thinking", "text": delta}] if isinstance(delta, str) and delta else []
        if event_type == "response.output_item.added":
            item = event.get("item")
            index = event.get("output_index")
            if (
                not isinstance(item, dict)
                or item.get("type") != "function_call"
                or not isinstance(index, int)
            ):
                return []
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if isinstance(call_id, str):
                tool_ids[index] = call_id
            if isinstance(name, str):
                tool_names[index] = name
            raw_arguments = item.get("arguments")
            arguments = raw_arguments if isinstance(raw_arguments, str) else ""
            accumulated_arguments[index] = arguments
            return [
                {
                    "type": "tool_calls_delta",
                    "tool_calls": [
                        {
                            "index": index,
                            **({"id": tool_ids[index]} if index in tool_ids else {}),
                            "type": "function",
                            "function": {
                                **({"name": tool_names[index]} if index in tool_names else {}),
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            ]
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in {"reasoning", "thought"}:
                text = item.get("text") or item.get("summary")
                if isinstance(text, str) and text:
                    return [{"type": "thinking", "text": text}]
            return []
        if event_type == "response.function_call_arguments.delta":
            index = event.get("output_index")
            delta = event.get("delta")
            if not isinstance(index, int) or not isinstance(delta, str) or not delta:
                return []
            accumulated_arguments[index] = accumulated_arguments.get(index, "") + delta
            return [
                {
                    "type": "tool_calls_delta",
                    "tool_calls": [
                        {
                            "index": index,
                            **({"id": tool_ids[index]} if index in tool_ids else {}),
                            "type": "function",
                            "function": {
                                **({"name": tool_names[index]} if index in tool_names else {}),
                                "arguments": accumulated_arguments[index],
                            },
                        }
                    ],
                }
            ]
        if event_type == "response.completed":
            return [{"type": "completed"}]
        if event_type == "response.error":
            return [{"type": "error", "message": event.get("message", "Unknown error")}]
        return []
