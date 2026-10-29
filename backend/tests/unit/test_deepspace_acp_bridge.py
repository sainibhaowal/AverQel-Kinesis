"""Tests for the DeepSpace Agent Client Protocol bridge.

The check covers the protocol layer itself: the prompt it accepts, the
updates it emits from a turn's frames, and the errors it reports.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.acp_bridge import (
    AGENT_NAME,
    JSONRPC_INVALID_PARAMS,
    PROTOCOL_VERSION,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    UPDATE_KIND_AGENT_MESSAGE,
    UPDATE_KIND_TOOL_CALL,
    UPDATE_KIND_TOOL_CALL_UPDATE,
    AcpBridge,
    AcpError,
    prompt_text,
    updates_from_frame,
)

pytestmark = pytest.mark.unit_no_db


def _bridge() -> AcpBridge:
    """Build a bridge for one authenticated caller."""
    return AcpBridge(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
    )


def _frame(event: str, data: dict[str, Any]) -> str:
    """Build one SSE frame the way the turn emits it."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_a_prompt_joins_its_text_blocks() -> None:
    prompt = prompt_text(
        [
            {"type": "text", "text": "First question."},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "Second part."},
        ]
    )

    assert prompt == "First question.\n\nSecond part."


def test_a_prompt_without_text_is_refused() -> None:
    with pytest.raises(AcpError) as excinfo:
        prompt_text([{"type": "image", "data": "..."}])

    assert excinfo.value.code == JSONRPC_INVALID_PARAMS
    assert "no text" in excinfo.value.message


def test_a_prompt_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(AcpError):
        prompt_text("just a string")


def test_an_oversized_prompt_is_refused() -> None:
    with pytest.raises(AcpError, match="character limit"):
        prompt_text([{"type": "text", "text": "x" * 100_001}])


def test_a_session_belongs_to_the_caller_that_opened_it() -> None:
    bridge = _bridge()
    session = bridge.new_session(uuid.uuid4())

    assert bridge.session(session.session_id).session_id == session.session_id

    other = _bridge()
    with pytest.raises(AcpError, match="unknown session"):
        other.session(session.session_id)


def test_closing_a_session_removes_it() -> None:
    bridge = _bridge()
    session = bridge.new_session(uuid.uuid4())

    bridge.close_session(session.session_id)

    with pytest.raises(AcpError, match="unknown session"):
        bridge.session(session.session_id)


def test_an_assistant_message_becomes_an_agent_message_update() -> None:
    updates = updates_from_frame(
        _frame("delta", {"text": "Here is the answer."}),
        turn_index=0,
    )

    assert updates == [
        {
            "sessionUpdate": UPDATE_KIND_AGENT_MESSAGE,
            "content": {"type": "text", "text": "Here is the answer."},
        }
    ]


def test_a_tool_start_becomes_a_tool_call_update() -> None:
    updates = updates_from_frame(
        _frame(
            "tool_start",
            {
                "tool_name": "web_search",
                "tool_id": "call-1",
                "tool_input": {"query": "deep space"},
            },
        ),
        turn_index=0,
    )

    assert updates[0]["sessionUpdate"] == UPDATE_KIND_TOOL_CALL
    assert updates[0]["toolCallId"] == "call-1"
    assert updates[0]["status"] == "in_progress"
    assert updates[0]["rawInput"] == {"query": "deep space"}


def test_a_tool_result_becomes_a_completed_update() -> None:
    updates = updates_from_frame(
        _frame(
            "tool_result",
            {"tool_name": "web_search", "tool_id": "call-1", "output": "3 results"},
        ),
        turn_index=0,
    )

    assert updates[0]["sessionUpdate"] == UPDATE_KIND_TOOL_CALL_UPDATE
    assert updates[0]["status"] == "completed"
    assert updates[0]["content"][0]["content"]["text"] == "3 results"


def test_a_tool_error_becomes_a_failed_update() -> None:
    updates = updates_from_frame(
        _frame(
            "tool_error",
            {"tool_name": "web_search", "tool_id": "call-1", "error": "blocked"},
        ),
        turn_index=0,
    )

    assert updates[0]["status"] == "failed"


def test_a_frame_the_bridge_does_not_carry_produces_no_update() -> None:
    updates = updates_from_frame(
        _frame("metrics", {"contextUsedTokens": 10}),
        turn_index=0,
    )

    assert updates == []


def test_the_protocol_version_and_agent_name_are_stable() -> None:
    assert PROTOCOL_VERSION == 1
    assert AGENT_NAME == "averqel-deepspace-acp"


def test_the_stop_reasons_cover_the_outcomes_a_turn_reaches() -> None:
    assert STOP_REASON_END_TURN == "end_turn"
    assert STOP_REASON_CANCELLED == "cancelled"
    assert STOP_REASON_REFUSAL == "refusal"
