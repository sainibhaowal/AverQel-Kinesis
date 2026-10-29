"""DeepSpace stdio JSON-RPC server.

Provides a stdio-based JSON-RPC server for DeepSpace SDK clients.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.deepspace.sdk.protocol import (
    InitializeResult,
    JsonRpcRequest,
    JsonRpcResponse,
    RpcErrorCode,
    RpcMethod,
    make_error,
)
from app.deepspace.services.chat_service import DeepSpaceChatService

logger = logging.getLogger(__name__)


class StdioServer:
    """stdio JSON-RPC server for DeepSpace."""

    def __init__(self, chat_service: DeepSpaceChatService):
        self.chat_service = chat_service
        self._methods: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._initialized = False
        self._register_methods()

    def _register_methods(self) -> None:
        """Register all RPC methods."""
        self._methods[RpcMethod.INITIALIZE] = self._handle_initialize
        self._methods[RpcMethod.SHUTDOWN] = self._handle_shutdown
        self._methods[RpcMethod.CONVERSATION_CREATE] = self._handle_conversation_create
        self._methods[RpcMethod.CONVERSATION_GET] = self._handle_conversation_get
        self._methods[RpcMethod.CONVERSATION_LIST] = self._handle_conversation_list
        self._methods[RpcMethod.CONVERSATION_DELETE] = self._handle_conversation_delete
        self._methods[RpcMethod.TURN_RUN] = self._handle_turn_run
        self._methods[RpcMethod.TURN_CANCEL] = self._handle_turn_cancel
        self._methods[RpcMethod.TOOL_CALL] = self._handle_tool_call
        self._methods[RpcMethod.SUBAGENT_SPAWN] = self._handle_subagent_spawn
        self._methods[RpcMethod.SUBAGENT_SEND] = self._handle_subagent_send
        self._methods[RpcMethod.SUBAGENT_INTERRUPT] = self._handle_subagent_interrupt
        self._methods[RpcMethod.SUBAGENT_LIST] = self._handle_subagent_list
        self._methods[RpcMethod.FILE_READ] = self._handle_file_read
        self._methods[RpcMethod.FILE_WRITE] = self._handle_file_write
        self._methods[RpcMethod.FILE_LIST] = self._handle_file_list

    async def _handle_initialize(self, params: dict) -> dict:
        """Handle initialize request."""
        self._initialized = True
        return (
            InitializeResult().to_dict()
            if hasattr(InitializeResult, "to_dict")
            else InitializeResult().__dict__
        )

    async def _handle_shutdown(self, params: dict) -> dict:
        """Handle shutdown request."""
        return {"status": "ok"}

    async def _handle_conversation_create(self, params: dict) -> dict:
        """Create a new conversation."""
        # This would need to be implemented with proper auth context
        # For now, return a placeholder
        return {
            "id": "00000000-0000-0000-0000-000000000000",
            "title": params.get("title", "New Conversation"),
            "preset": params.get("preset", "standard"),
            "created_at": "2024-01-01T00:00:00Z",
        }

    async def _handle_conversation_get(self, params: dict) -> dict:
        """Get conversation details."""
        return {}

    async def _handle_conversation_list(self, params: dict) -> dict:
        """List conversations."""
        return []

    async def _handle_conversation_delete(self, params: dict) -> dict:
        """Delete a conversation."""
        return {"status": "ok"}

    async def _handle_turn_run(self, params: dict) -> dict:
        """Run a turn."""
        return {}

    async def _handle_turn_cancel(self, params: dict) -> dict:
        """Cancel a turn."""
        return {"status": "ok"}

    async def _handle_tool_call(self, params: dict) -> dict:
        """Execute a tool call."""
        return {}

    async def _handle_subagent_spawn(self, params: dict) -> dict:
        """Spawn a subagent."""
        return {}

    async def _handle_subagent_send(self, params: dict) -> dict:
        """Send message to subagent."""
        return {}

    async def _handle_subagent_interrupt(self, params: dict) -> dict:
        """Interrupt subagent."""
        return {}

    async def _handle_subagent_list(self, params: dict) -> dict:
        """List subagents."""
        return []

    async def _handle_file_read(self, params: dict) -> dict:
        """Read a file."""
        return {}

    async def _handle_file_write(self, params: dict) -> dict:
        """Write a file."""
        return {}

    async def _handle_file_list(self, params: dict) -> dict:
        """List files."""
        return []

    async def handle_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        """Handle a JSON-RPC request."""
        method = request.method
        params = request.params or {}
        req_id = request.id

        handler = self._methods.get(method)
        if not handler:
            return JsonRpcResponse(
                id=req_id,
                error=make_error(RpcErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}"),
            )

        try:
            result = await handler(params)
            return JsonRpcResponse(id=req_id, result=result)
        except Exception as e:
            logger.error("Error handling %s: %s", method, e, exc_info=True)
            return JsonRpcResponse(
                id=req_id,
                error=make_error(RpcErrorCode.INTERNAL_ERROR, str(e)),
            )

    async def run(self) -> None:
        """Run the stdio server loop."""
        logger.info("DeepSpace stdio server started")

        while True:
            try:
                # Read Content-Length header
                header = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                if not header:
                    break

                header = header.strip()
                if not header.startswith("Content-Length:"):
                    continue

                content_length = int(header.split(":")[1].strip())

                # Read empty line
                await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

                # Read content
                content = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.read, content_length
                )

                try:
                    request_data = json.loads(content)
                    request = JsonRpcRequest(**request_data)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Invalid request: %s", e)
                    continue

                response = await self.handle_request(request)
                self._write_response(response)

            except Exception as e:
                logger.error("Server error: %s", e, exc_info=True)

    def _write_response(self, response: JsonRpcResponse) -> None:
        """Write a JSON-RPC response to stdout."""
        content = json.dumps(response.__dict__ if hasattr(response, "__dict__") else response)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        sys.stdout.write(header + content)
        sys.stdout.flush()


async def main() -> int:
    """Main entry point for stdio server."""
    parser = argparse.ArgumentParser(description="DeepSpace stdio JSON-RPC server")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = get_settings()

    # Initialize chat service
    from app.platform.database.session import get_db

    db = next(get_db())
    chat_service = DeepSpaceChatService(db=db, settings=settings)

    server = StdioServer(chat_service)
    await server.run()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
