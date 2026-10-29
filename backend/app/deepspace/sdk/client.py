"""DeepSpace Python SDK client.

Ported from NeoSIS @averqel/neosis-sdk-client.
Provides async client for driving DeepSpace via HTTP or stdio JSON-RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx

from app.deepspace.sdk.protocol import (
    ConversationCreateParams,
    InitializeParams,
    InitializeResult,
    JsonRpcRequest,
    RpcErrorCode,
    RpcMethod,
    SessionEvent,
    SubagentSpawnParams,
    SubagentSpawnResult,
    TurnRunParams,
)

logger = logging.getLogger(__name__)


class DeepSpaceSdkError(Exception):
    """SDK error with optional error code."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class Conversation:
    """Conversation handle."""

    id: UUID
    title: str
    preset: str
    created_at: datetime

    def __str__(self) -> str:
        return f"Conversation({self.id[:8]}: {self.title})"


@dataclass
class Turn:
    """Turn handle."""

    id: UUID
    conversation_id: UUID
    status: Literal["running", "completed", "failed", "cancelled"]
    answer: str | None = None
    tool_calls: list[dict] = None
    error: str | None = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []


class DeepSpaceHttpClient:
    """HTTP-based DeepSpace client."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DeepSpaceHttpClient:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make HTTP request."""
        if not self._client:
            raise DeepSpaceSdkError("Client not initialized. Use async with.")
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def create_conversation(
        self,
        title: str | None = None,
        preset: str = "standard",
        workspace: str | None = None,
    ) -> Conversation:
        """Create a new conversation."""
        data = await self._request(
            "POST",
            "/api/v1/deepspace/conversations",
            json={
                "title": title,
                "preset": preset,
                "workspace": workspace,
            },
        )
        return Conversation(
            id=UUID(data["id"]),
            title=data["title"],
            preset=data["preset"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        """Get conversation details."""
        data = await self._request("GET", f"/api/v1/deepspace/conversations/{conversation_id}")
        return Conversation(
            id=UUID(data["id"]),
            title=data["title"],
            preset=data["preset"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def list_conversations(self, limit: int = 50, offset: int = 0) -> list[Conversation]:
        """List conversations."""
        data = await self._request(
            "GET",
            "/api/v1/deepspace/conversations",
            params={
                "limit": limit,
                "offset": offset,
            },
        )
        return [
            Conversation(
                id=UUID(c["id"]),
                title=c["title"],
                preset=c["preset"],
                created_at=datetime.fromisoformat(c["created_at"]),
            )
            for c in data
        ]

    async def run_turn(
        self,
        conversation_id: UUID,
        prompt: str,
        stream: bool = False,
        model: str | None = None,
        preset: str | None = None,
    ) -> Turn:
        """Run a turn in a conversation."""
        data = await self._request(
            "POST",
            f"/api/v1/deepspace/conversations/{conversation_id}/turns",
            json={
                "prompt": prompt,
                "stream": stream,
                "model": model,
                "preset": preset,
            },
        )
        return Turn(
            id=UUID(data["turn_id"]),
            conversation_id=conversation_id,
            status=data["status"],
            answer=data.get("answer"),
            tool_calls=data.get("tool_calls", []),
            error=data.get("error"),
        )

    async def spawn_subagent(
        self,
        conversation_id: UUID,
        task: str,
        description: str,
        mode: Literal["one-shot", "continuable"] = "continuable",
        backend: Literal["in-process", "acp", "claude-code", "codex", "neosis-sdk"] = "in-process",
        model: str | None = None,
        persona: str | None = None,
        tool_allow: list[str] | None = None,
        tool_deny: list[str] | None = None,
    ) -> SubagentSpawnResult:
        """Spawn a subagent."""
        data = await self._request(
            "POST",
            f"/api/v1/deepspace/conversations/{conversation_id}/subagents",
            json={
                "task": task,
                "description": description,
                "mode": mode,
                "backend": backend,
                "model": model,
                "persona": persona,
                "tool_allow": tool_allow,
                "tool_deny": tool_deny,
            },
        )
        return SubagentSpawnResult(
            agent_id=UUID(data["agent_id"]),
            conversation_id=conversation_id,
            status=data["status"],
            answer=data.get("answer"),
        )

    async def send_subagent_message(
        self,
        conversation_id: UUID,
        agent_id: UUID,
        message: str,
    ) -> dict:
        """Send a message to a continuable subagent."""
        return await self._request(
            "POST",
            f"/api/v1/deepspace/conversations/{conversation_id}/subagents/{agent_id}/messages",
            json={
                "message": message,
            },
        )

    async def interrupt_subagent(
        self,
        conversation_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Interrupt a subagent."""
        return await self._request(
            "POST",
            f"/api/v1/deepspace/conversations/{conversation_id}/subagents/{agent_id}/interrupt",
        )


class DeepSpaceStdioClient:
    """stdio-based DeepSpace client (JSON-RPC over stdin/stdout)."""

    def __init__(
        self,
        command: list[str] = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command or ["deepspace", "serve-stdio"]
        self.cwd = cwd
        self.env = env or os.environ.copy()
        self._process: subprocess.Popen | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False

    async def start(self) -> None:
        """Start the stdio server process."""
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

        # Send initialize
        result = await self._send_request(RpcMethod.INITIALIZE, InitializeParams().to_dict())
        self._initialized = True
        return InitializeResult.from_dict(result)

    async def stop(self) -> None:
        """Stop the stdio server process."""
        if self._initialized:
            try:
                await self._send_request(RpcMethod.SHUTDOWN, {})
            except Exception:
                pass

        if self._process:
            self._process.terminate()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._process.wait(timeout=5)
                )
            except subprocess.TimeoutExpired:
                self._process.kill()
                await asyncio.get_event_loop().run_in_executor(None, self._process.wait)

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
        """Read responses from stdout."""
        if not self._process or not self._process.stdout:
            return

        buffer = b""
        while self._process.poll() is None:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self._process.stdout.readline
                )
                if not line:
                    break

                buffer += line
                if buffer.endswith(b"\r\n\r\n"):
                    # Parse headers
                    header_end = buffer.find(b"\r\n\r\n")
                    headers = buffer[:header_end].decode("ascii")
                    content_length = 0
                    for header_line in headers.split("\r\n"):
                        if header_line.lower().startswith("content-length:"):
                            content_length = int(header_line.split(":")[1].strip())

                    content = buffer[header_end + 4 :]
                    while len(content) < content_length:
                        more = await asyncio.get_event_loop().run_in_executor(
                            None, self._process.stdout.read, content_length - len(content)
                        )
                        if not more:
                            break
                        content += more

                    if len(content) >= content_length:
                        content = content[:content_length]
                        try:
                            response = json.loads(content.decode("utf-8"))
                            self._handle_response(response)
                        except json.JSONDecodeError:
                            logger.warning("Failed to parse JSON-RPC response")

                    buffer = buffer[header_end + 4 + content_length :]

            except Exception as e:
                logger.error("Error in stdio read loop: %s", e)
                break

    def _handle_response(self, response: dict) -> None:
        """Handle a JSON-RPC response."""
        req_id = response.get("id")
        if req_id is not None and req_id in self._pending:
            future = self._pending.pop(req_id)
            if "error" in response:
                error = response["error"]
                future.set_exception(
                    DeepSpaceSdkError(
                        error.get("message", "Unknown error"),
                        code=error.get("code"),
                        data=error.get("data"),
                    )
                )
            else:
                future.set_result(response.get("result"))

    async def _send_request(self, method: str, params: dict = None) -> Any:
        """Send a request and wait for response."""
        if not self._process or not self._process.stdin:
            raise DeepSpaceSdkError("Process not running")

        self._request_id += 1
        req_id = self._request_id

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        request = JsonRpcRequest(
            method=method,
            params=params,
            id=req_id,
        )

        self._write_message(request)

        try:
            return await asyncio.wait_for(future, timeout=60.0)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise DeepSpaceSdkError(f"Request {method} timed out", code=RpcErrorCode.INTERNAL_ERROR)

    def _write_message(self, message: JsonRpcRequest) -> None:
        """Write a JSON-RPC message to stdin."""
        content = json.dumps(message.to_dict() if hasattr(message, "to_dict") else message)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        self._process.stdin.write(header.encode("ascii") + content_bytes)
        self._process.stdin.flush()

    async def create_conversation(
        self,
        title: str | None = None,
        preset: str = "standard",
        workspace: str | None = None,
    ) -> Conversation:
        result = await self._send_request(
            RpcMethod.CONVERSATION_CREATE,
            ConversationCreateParams(title=title, preset=preset, workspace=workspace).__dict__,
        )
        return Conversation(
            id=UUID(result["id"]),
            title=result["title"],
            preset=result["preset"],
            created_at=datetime.fromisoformat(result["created_at"]),
        )

    async def run_turn(
        self,
        conversation_id: UUID,
        prompt: str,
        stream: bool = False,
        model: str | None = None,
        preset: str | None = None,
    ) -> Turn:
        result = await self._send_request(
            RpcMethod.TURN_RUN,
            TurnRunParams(
                conversation_id=conversation_id,
                prompt=prompt,
                stream=stream,
                model=model,
                preset=preset,
            ).__dict__,
        )
        return Turn(
            id=UUID(result["turn_id"]),
            conversation_id=conversation_id,
            status=result["status"],
            answer=result.get("answer"),
            tool_calls=result.get("tool_calls", []),
            error=result.get("error"),
        )

    async def spawn_subagent(self, **kwargs) -> SubagentSpawnResult:
        result = await self._send_request(
            RpcMethod.SUBAGENT_SPAWN, SubagentSpawnParams(**kwargs).to_dict()
        )
        return SubagentSpawnResult(
            agent_id=UUID(result["agent_id"]),
            conversation_id=UUID(result["conversation_id"]),
            status=result["status"],
            answer=result.get("answer"),
        )

    async def subscribe_events(
        self,
        conversation_id: UUID,
    ) -> AsyncIterator[SessionEvent]:
        """Subscribe to session events."""
        # This would use a notification-based approach
        # For now, return an empty iterator
        yield  # Make it an async generator
        return


class DeepSpaceClient:
    """Unified DeepSpace client supporting both HTTP and stdio transports."""

    def __init__(
        self,
        transport: Literal["http", "stdio"] = "http",
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        stdio_command: list[str] | None = None,
        stdio_cwd: str | None = None,
        timeout: float = 300.0,
    ):
        self.transport = transport
        self._http_client: DeepSpaceHttpClient | None = None
        self._stdio_client: DeepSpaceStdioClient | None = None

        if transport == "http":
            self._http_client = DeepSpaceHttpClient(base_url, api_key, timeout)
        elif transport == "stdio":
            self._stdio_client = DeepSpaceStdioClient(
                command=stdio_command,
                cwd=stdio_cwd,
            )
        else:
            raise ValueError(f"Unknown transport: {transport}")

    async def __aenter__(self) -> DeepSpaceClient:
        if self._http_client:
            await self._http_client.__aenter__()
        if self._stdio_client:
            await self._stdio_client.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._http_client:
            await self._http_client.__aexit__(exc_type, exc_val, exc_tb)
        if self._stdio_client:
            await self._stdio_client.stop()

    # Delegate to appropriate client
    async def create_conversation(self, **kwargs) -> Conversation:
        if self._http_client:
            return await self._http_client.create_conversation(**kwargs)
        return await self._stdio_client.create_conversation(**kwargs)

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        if self._http_client:
            return await self._http_client.get_conversation(conversation_id)
        raise NotImplementedError("get_conversation not implemented for stdio transport")

    async def list_conversations(self, **kwargs) -> list[Conversation]:
        if self._http_client:
            return await self._http_client.list_conversations(**kwargs)
        raise NotImplementedError("list_conversations not implemented for stdio transport")

    async def run_turn(self, **kwargs) -> Turn:
        if self._http_client:
            return await self._http_client.run_turn(**kwargs)
        return await self._stdio_client.run_turn(**kwargs)

    async def spawn_subagent(self, **kwargs) -> SubagentSpawnResult:
        if self._http_client:
            return await self._http_client.spawn_subagent(**kwargs)
        return await self._stdio_client.spawn_subagent(**kwargs)

    async def send_subagent_message(self, **kwargs) -> dict:
        if self._http_client:
            return await self._http_client.send_subagent_message(**kwargs)
        raise NotImplementedError("send_subagent_message not implemented for stdio transport")

    async def interrupt_subagent(self, **kwargs) -> dict:
        if self._http_client:
            return await self._http_client.interrupt_subagent(**kwargs)
        raise NotImplementedError("interrupt_subagent not implemented for stdio transport")


# High-level API
@asynccontextmanager
async def connect(
    transport: Literal["http", "stdio"] = "http",
    **kwargs,
) -> AsyncIterator[DeepSpaceClient]:
    """Create and connect a DeepSpace client."""
    client = DeepSpaceClient(transport=transport, **kwargs)
    async with client:
        yield client


async def run_task(
    task: str,
    transport: Literal["http", "stdio"] = "http",
    preset: str = "standard",
    **kwargs,
) -> str:
    """Run a single task and return the answer."""
    async with connect(transport=transport, **kwargs) as client:
        conv = await client.create_conversation(preset=preset)
        turn = await client.run_turn(conversation_id=conv.id, prompt=task)
        if turn.status == "completed":
            return turn.answer or ""
        raise DeepSpaceSdkError(f"Turn failed: {turn.error or turn.status}")
