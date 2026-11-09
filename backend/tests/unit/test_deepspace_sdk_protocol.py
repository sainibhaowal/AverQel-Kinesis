"""Regression tests for the slotted DeepSpace SDK wire models."""

import json
from datetime import datetime
from io import StringIO
from typing import Any, cast
from uuid import UUID

import pytest

from app.deepspace.sdk.client import Conversation
from app.deepspace.sdk.protocol import (
    ConversationCreateParams,
    InitializeParams,
    InitializeResult,
    JsonRpcRequest,
    JsonRpcResponse,
    make_notification,
    make_response,
)
from app.deepspace.sdk.server import StdioServer

pytestmark = pytest.mark.unit_no_db


def test_slotted_protocol_models_serialize_without_instance_dict() -> None:
    """Wire serialization handles the SDK's frozen, slotted dataclasses."""
    conversation_id = UUID("6b1f17a0-ff11-4e11-9da0-037b88935d30")

    assert InitializeParams().to_dict() == {
        "protocol_version": 1,
        "client_info": {},
        "capabilities": {},
    }
    assert InitializeResult().to_dict() == {
        "protocol_version": 1,
        "server_info": {"name": "averqel-deepspace", "version": "1.0"},
        "capabilities": {},
    }
    assert ConversationCreateParams().to_dict() == {"preset": "standard"}
    assert JsonRpcRequest(method="initialize", id=1).to_dict() == {
        "method": "initialize",
        "jsonrpc": "2.0",
        "id": 1,
    }
    assert make_response(1, result={"ok": True}) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"ok": True},
    }
    assert make_notification("events/subscribe", {"conversation_id": str(conversation_id)}) == {
        "jsonrpc": "2.0",
        "method": "events/subscribe",
        "params": {"conversation_id": str(conversation_id)},
    }
    assert JsonRpcResponse(id=None, result={"ok": True}).to_dict() == {
        "jsonrpc": "2.0",
        "id": None,
        "result": {"ok": True},
    }
    assert JsonRpcResponse(id=2, error={"code": -32603, "message": "failed"}).to_dict() == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32603, "message": "failed"},
    }


def test_conversation_string_uses_uuid_text() -> None:
    """Conversation handles format their UUID without string slicing errors."""
    conversation = Conversation(
        id=UUID("6b1f17a0-ff11-4e11-9da0-037b88935d30"),
        title="Example",
        preset="standard",
        created_at=datetime(2026, 1, 1),
    )

    assert str(conversation) == "Conversation(6b1f17a0: Example)"


@pytest.mark.asyncio
async def test_stdio_server_serializes_initialize_and_response(monkeypatch) -> None:
    """The Content-Length transport writes ordinary JSON-RPC objects."""
    server = StdioServer(cast(Any, object()))
    initialize = await server._handle_initialize({})
    assert initialize["protocol_version"] == 1

    output = StringIO()
    monkeypatch.setattr("app.deepspace.sdk.server.sys.stdout", output)
    server._write_response(JsonRpcResponse(id=None, result=initialize))

    frame = output.getvalue()
    header, payload = frame.split("\r\n\r\n", maxsplit=1)
    assert int(header.removeprefix("Content-Length: ")) == len(payload.encode("utf-8"))
    assert json.loads(payload) == {
        "jsonrpc": "2.0",
        "id": None,
        "result": initialize,
    }
