"""The Agent Client Protocol bridge for DeepSpace.

The Agent Client Protocol lets a trusted program drive an agent without a
human in the loop.  This bridge exposes DeepSpace conversations through
that protocol over JSON-RPC: a client creates a session, sends prompts,
reads the semantic updates the turn produces, and cancels work it no longer
wants.

The bridge is a thin protocol layer.  It owns the session-to-conversation
mapping, the prompt content it accepts, and the update shapes it emits; the
turn itself is the ordinary DeepSpace turn, so the same tools, policy,
durable log, and projections apply.  A client that drives DeepSpace through
this bridge gets exactly the behavior the browser gets.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.auth.dependencies import AuthContext
from app.deepspace.services.chat_service import DeepSpaceChatService, sse

#: The protocol version this bridge implements.
PROTOCOL_VERSION = 1

#: The agent name reported in the initialize response.
AGENT_NAME = "averqel-deepspace-acp"

#: The largest number of characters one prompt may carry.
MAX_PROMPT_CHARS = 100_000

#: The session-update kinds this bridge emits.
UPDATE_KIND_USER_MESSAGE = "user_message_chunk"
UPDATE_KIND_AGENT_MESSAGE = "agent_message_chunk"
UPDATE_KIND_TOOL_CALL = "tool_call"
UPDATE_KIND_TOOL_CALL_UPDATE = "tool_call_update"
UPDATE_KIND_PLAN = "plan"

#: The stop reasons this bridge reports.
STOP_REASON_END_TURN = "end_turn"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_REFUSAL = "refusal"
STOP_REASON_CANCELLED = "cancelled"

#: The JSON-RPC error codes this bridge uses.
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class AcpError(Exception):
    """One protocol failure, reported as a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        """Create the error.

        Args:
            code: The JSON-RPC error code.
            message: The human-readable detail.
        """
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class AcpSession:
    """One bridge session and the DeepSpace conversation behind it."""

    session_id: str
    conversation_id: uuid.UUID
    created_at: str


@dataclass(slots=True)
class AcpBridge:
    """The bridge state for one authenticated caller.

    Attributes:
        auth: The authenticated tenant and user.
        sessions: The sessions this caller has opened, by session id.
    """

    auth: AuthContext
    sessions: dict[str, AcpSession] = field(default_factory=dict)

    def session(self, session_id: str) -> AcpSession:
        """Return one session of this caller.

        Args:
            session_id: The session the client named.

        Returns:
            The session.

        Raises:
            AcpError: When the session is unknown to this caller.
        """
        record = self.sessions.get(str(session_id))
        if record is None:
            raise AcpError(
                JSONRPC_INVALID_PARAMS,
                f"unknown session: {session_id}",
            )
        return record

    def new_session(self, conversation_id: uuid.UUID) -> AcpSession:
        """Record a new session for one conversation.

        Args:
            conversation_id: The conversation the session drives.

        Returns:
            The new session.
        """
        session = AcpSession(
            session_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            created_at=uuid.uuid4().hex,
        )
        self.sessions[session.session_id] = session
        return session

    def close_session(self, session_id: str) -> None:
        """Close one session.

        Args:
            session_id: The session the client named.
        """
        self.sessions.pop(str(session_id), None)


def prompt_text(blocks: Any) -> str:
    """Read the text of one ACP prompt.

    The protocol carries a list of content blocks.  Only text blocks carry
    a DeepSpace prompt, so a prompt with no text block is rejected instead
    of being run with an empty instruction.

    Args:
        blocks: The prompt's content blocks.

    Returns:
        The prompt text.

    Raises:
        AcpError: When the blocks carry no text.
    """
    if not isinstance(blocks, list):
        raise AcpError(JSONRPC_INVALID_PARAMS, "prompt content must be a list of blocks")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "text":
            continue
        text = str(block.get("text") or "")
        if text.strip():
            parts.append(text)
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        raise AcpError(JSONRPC_INVALID_PARAMS, "the prompt carries no text")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AcpError(
            JSONRPC_INVALID_PARAMS,
            f"the prompt exceeds the {MAX_PROMPT_CHARS} character limit",
        )
    return prompt


def _frame_data(frame: str) -> dict[str, Any]:
    """Read the data payload of one SSE frame.

    Args:
        frame: The frame the turn emitted.

    Returns:
        The frame's data, or an empty mapping when it carries none.
    """
    for line in frame.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[5:].strip())
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _frame_name(frame: str) -> str:
    """Read the event name of one SSE frame.

    Args:
        frame: The frame the turn emitted.

    Returns:
        The event name, or an empty string.
    """
    for line in frame.splitlines():
        if line.startswith("event:"):
            return line[6:].strip()
    return ""


def updates_from_frame(frame: str, *, turn_index: int) -> list[dict[str, Any]]:
    """Translate one turn frame into the protocol updates it produces.

    Args:
        frame: The frame the turn emitted.
        turn_index: The turn the frame belongs to.

    Returns:
        The updates the frame produces.
    """
    name = _frame_name(frame)
    data = _frame_data(frame)
    if name == "delta":
        text = str(data.get("text") or "")
        if not text:
            return []
        return [
            {
                "sessionUpdate": UPDATE_KIND_AGENT_MESSAGE,
                "content": {"type": "text", "text": text},
            }
        ]
    if name == "model_message":
        text = str(data.get("text") or "")
        if not text:
            return []
        return [
            {
                "sessionUpdate": UPDATE_KIND_AGENT_MESSAGE,
                "content": {"type": "text", "text": text},
            }
        ]
    if name == "tool_start":
        tool_name = str(data.get("tool_name") or "")
        if not tool_name:
            return []
        return [
            {
                "sessionUpdate": UPDATE_KIND_TOOL_CALL,
                "toolCallId": str(data.get("tool_id") or ""),
                "title": tool_name,
                "kind": "other",
                "status": "in_progress",
                "rawInput": (
                    data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
                ),
            }
        ]
    if name in {"tool_result", "tool_error"}:
        tool_name = str(data.get("tool_name") or "")
        if not tool_name:
            return []
        return [
            {
                "sessionUpdate": UPDATE_KIND_TOOL_CALL_UPDATE,
                "toolCallId": str(data.get("tool_id") or ""),
                "status": "failed" if name == "tool_error" else "completed",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": str(data.get("output") or "")},
                    }
                ],
            }
        ]
    if name == "plan_submitted":
        return [
            {
                "sessionUpdate": UPDATE_KIND_PLAN,
                "plan": str(data.get("content") or ""),
            }
        ]
    return []


async def run_prompt(
    *,
    bridge: AcpBridge,
    session: AcpSession,
    prompt: str,
    service: DeepSpaceChatService,
    thinking_enabled: bool = False,
    reasoning_effort: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Run one prompt and collect the updates it produces.

    Args:
        bridge: The bridge state.
        session: The session the prompt belongs to.
        prompt: The prompt text.
        service: The chat service that runs the turn.
        thinking_enabled: Whether the model may show its reasoning.
        reasoning_effort: The selected reasoning effort.

    Returns:
        The updates the turn produced, and the stop reason.
    """
    updates: list[dict[str, Any]] = []
    stop_reason = STOP_REASON_END_TURN
    turn_index = 0
    async for frame in service.stream_turn(
        auth=bridge.auth,
        conversation_id=session.conversation_id,
        prompt=prompt,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        request=None,
    ):
        name = _frame_name(frame)
        data = _frame_data(frame)
        if name == "error":
            stop_reason = STOP_REASON_REFUSAL
            updates.append(
                {
                    "sessionUpdate": UPDATE_KIND_AGENT_MESSAGE,
                    "content": {
                        "type": "text",
                        "text": str(data.get("message") or "The turn failed safely."),
                    },
                }
            )
            continue
        if name == "done":
            status = str(data.get("status") or "ready")
            if status == "cancelled":
                stop_reason = STOP_REASON_CANCELLED
            elif status == "failed":
                stop_reason = STOP_REASON_REFUSAL
            continue
        updates.extend(updates_from_frame(frame, turn_index=turn_index))
    return updates, stop_reason


__all__ = [
    "AGENT_NAME",
    "PROTOCOL_VERSION",
    "STOP_REASON_CANCELLED",
    "STOP_REASON_END_TURN",
    "STOP_REASON_MAX_TOKENS",
    "STOP_REASON_REFUSAL",
    "UPDATE_KIND_AGENT_MESSAGE",
    "UPDATE_KIND_PLAN",
    "UPDATE_KIND_TOOL_CALL",
    "UPDATE_KIND_TOOL_CALL_UPDATE",
    "UPDATE_KIND_USER_MESSAGE",
    "AcpBridge",
    "AcpError",
    "AcpSession",
    "prompt_text",
    "run_prompt",
    "sse",
]
