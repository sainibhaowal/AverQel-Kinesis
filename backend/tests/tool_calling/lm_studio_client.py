"""LM Studio OpenAI-compatible client for tool-calling tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class Choice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str | None = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletion(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class LMStudioClient:
    """Async client for LM Studio's OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        model: str = "spark-x2.6-1.7b",
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LMStudioClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")
        return self._client

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatCompletion:
        """Send a chat completion request with tool calling support."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = await self.client.post("/chat/completions", json=payload)
                response.raise_for_status()
                data = response.json()
                return ChatCompletion(**data)
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code >= 500:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            except (TimeoutError, httpx.RequestError) as e:
                last_error = e
                await asyncio.sleep(2**attempt)
                continue

        raise last_error or RuntimeError("Max retries exceeded")

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completion (for long-running tool calls)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        async with self.client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue

    async def health_check(self) -> bool:
        """Check if LM Studio is reachable and model is loaded."""
        try:
            response = await self.client.get("/models")
            return response.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """List available models."""
        response = await self.client.get("/models")
        response.raise_for_status()
        data = response.json()
        return [m["id"] for m in data.get("data", [])]


@dataclass
class ToolCallTrace:
    """Complete trace of a tool-calling interaction."""

    scenario_id: str
    prompt: str
    tools_available: list[str]
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_response: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "scenario_id": self.scenario_id,
                "prompt": self.prompt,
                "tools_available": self.tools_available,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "tool_results": self.tool_results,
                "final_response": self.final_response,
                "errors": self.errors,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "metadata": self.metadata,
            },
            ensure_ascii=False,
        )


class ToolCallingOrchestrator:
    """Orchestrates the full tool-calling loop: LLM -> Tool -> LLM -> Tool -> ..."""

    def __init__(
        self,
        lm_studio: LMStudioClient,
        backend_client: Any,  # Will be BackendTestClient
        tools: list[dict[str, Any]],
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
    ):
        self.lm_studio = lm_studio
        self.backend = backend_client
        self.tools = tools
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn

    async def run_scenario(
        self,
        scenario_id: str,
        prompt: str,
        tools_available: list[str] | None = None,
    ) -> ToolCallTrace:
        """Run a complete tool-calling scenario and return the trace."""

        # Filter tools to only those available in this scenario
        available_tool_names = (
            set(tools_available) if tools_available else {t["function"]["name"] for t in self.tools}
        )
        scenario_tools = [t for t in self.tools if t["function"]["name"] in available_tool_names]

        trace = ToolCallTrace(
            scenario_id=scenario_id,
            prompt=prompt,
            tools_available=list(available_tool_names),
        )

        messages = [{"role": "user", "content": prompt}]

        for turn_idx in range(self.max_turns):
            turn_start = time.time()

            # 1. Call LM Studio
            try:
                response = await self.lm_studio.chat_completion(
                    messages=messages,
                    tools=scenario_tools,
                    tool_choice="auto",
                )
            except Exception as e:
                trace.errors.append(
                    {
                        "turn": turn_idx,
                        "type": "llm_error",
                        "error": str(e),
                    }
                )
                break

            choice = response.choices[0]
            message = choice.message

            # Record the turn
            trace.turns.append(
                {
                    "turn": turn_idx,
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [tc.model_dump() for tc in message.tool_calls],
                    "finish_reason": choice.finish_reason,
                    "duration_ms": (time.time() - turn_start) * 1000,
                }
            )

            # Add assistant message to history
            messages.append(message.model_dump(exclude_none=True))

            # 2. Check for tool calls
            tool_calls = message.tool_calls
            if not tool_calls:
                # No tool calls - model gave final response
                trace.final_response = message.content
                trace.completed_at = time.time()
                break

            # 3. Execute each tool call
            if len(tool_calls) > self.max_tool_calls_per_turn:
                trace.errors.append(
                    {
                        "turn": turn_idx,
                        "type": "too_many_tool_calls",
                        "count": len(tool_calls),
                        "max_allowed": self.max_tool_calls_per_turn,
                    }
                )
                tool_calls = tool_calls[: self.max_tool_calls_per_turn]

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as e:
                    trace.errors.append(
                        {
                            "turn": turn_idx,
                            "tool_call_id": tc.id,
                            "tool_name": tool_name,
                            "type": "invalid_arguments",
                            "error": str(e),
                        }
                    )
                    # Send error back to model
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": f"Invalid JSON arguments: {e}"}),
                        }
                    )
                    continue

                trace.tool_calls.append(
                    {
                        "turn": turn_idx,
                        "tool_call_id": tc.id,
                        "tool_name": tool_name,
                        "arguments": args,
                    }
                )

                # Execute against REAL backend
                tool_start = time.time()
                try:
                    result = await self.backend.execute_tool(tool_name, args)
                    trace.tool_results.append(
                        {
                            "turn": turn_idx,
                            "tool_call_id": tc.id,
                            "tool_name": tool_name,
                            "result": result,
                            "duration_ms": (time.time() - tool_start) * 1000,
                            "status": "success",
                        }
                    )
                except Exception as e:
                    trace.tool_results.append(
                        {
                            "turn": turn_idx,
                            "tool_call_id": tc.id,
                            "tool_name": tool_name,
                            "result": {"error": str(e), "status": "error"},
                            "duration_ms": (time.time() - tool_start) * 1000,
                            "status": "error",
                        }
                    )
                    trace.errors.append(
                        {
                            "turn": turn_idx,
                            "tool_call_id": tc.id,
                            "tool_name": tool_name,
                            "type": "tool_execution_error",
                            "error": str(e),
                        }
                    )
                    result = {"error": str(e), "status": "error"}

                # Add tool result to messages for next turn
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

        else:
            # Max turns reached
            trace.errors.append(
                {
                    "type": "max_turns_exceeded",
                    "max_turns": self.max_turns,
                }
            )

        trace.completed_at = trace.completed_at or time.time()
        return trace


def create_lm_studio_client(
    base_url: str | None = None,
    model: str | None = None,
) -> LMStudioClient:
    """Factory function with environment variable overrides."""
    return LMStudioClient(
        base_url=base_url or os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
        model=model or os.getenv("LM_STUDIO_MODEL", "spark-x2.6-1.7b"),
        timeout=float(os.getenv("LM_STUDIO_TIMEOUT", "60")),
    )
