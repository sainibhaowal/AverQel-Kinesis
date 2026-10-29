"""Focused tests for the sdk-minimal headless wire profile."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from app.deepspace.cli.headless import run_sdk_minimal_stdio

pytestmark = pytest.mark.unit_no_db


def test_sdk_minimal_profile_completes_one_turn_without_content_length_framing() -> None:
    """The profile speaks the newline protocol expected by SDK subagents."""
    result = SimpleNamespace(
        success=True,
        content="SDK_PROFILE_OK",
        tool_calls=[{"name": "workspace_list"}],
        error=None,
        metadata={"conversation_id": "conversation-1"},
    )

    class Runner:
        async def run_turn(self, prompt: str, *, model: str | None = None):
            assert prompt == "reply"
            assert model == "test-model"
            return result

    input_stream = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"turn","params":{"prompt":"reply","model":"test-model"}}\n'
    )
    output_stream = io.StringIO()

    assert (
        asyncio.run(
            run_sdk_minimal_stdio(
                runner=Runner(), input_stream=input_stream, output_stream=output_stream
            )
        )
        == 0
    )
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[0]["result"]["serverInfo"]["profile"] == "sdk-minimal"
    assert messages[1]["method"] == "turn/update"
    assert messages[1]["params"]["text"] == "SDK_PROFILE_OK"
    assert messages[-1]["id"] == 2
    assert messages[-1]["result"]["status"] == "completed"


def test_sdk_minimal_profile_rejects_turn_before_initialize() -> None:
    """The profile refuses a turn until the protocol handshake completed."""
    output_stream = io.StringIO()
    status = asyncio.run(
        run_sdk_minimal_stdio(
            runner=object(),
            input_stream=io.StringIO(
                '{"jsonrpc":"2.0","id":2,"method":"turn","params":{"prompt":"reply"}}\n'
            ),
            output_stream=output_stream,
        )
    )
    assert status == 1
    assert json.loads(output_stream.getvalue())["error"]["code"] == -32006
