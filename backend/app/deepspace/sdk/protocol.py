"""DeepSpace SDK protocol definitions.

Ported from NeoSIS @averqel/neosis-sdk-protocol.
Defines the JSON-RPC wire protocol for SDK clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID


class RpcMethod(str, Enum):
    """JSON-RPC method names."""

    # Session management
    INITIALIZE = "initialize"
    SHUTDOWN = "shutdown"

    # Conversation management
    CONVERSATION_CREATE = "conversation/create"
    CONVERSATION_GET = "conversation/get"
    CONVERSATION_LIST = "conversation/list"
    CONVERSATION_DELETE = "conversation/delete"
    CONVERSATION_FORK = "conversation/fork"

    # Turn execution
    TURN_RUN = "turn/run"
    TURN_CANCEL = "turn/cancel"
    TURN_WAIT = "turn/wait"

    # Tool execution
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"

    # Subagent management
    SUBAGENT_SPAWN = "subagent/spawn"
    SUBAGENT_SEND = "subagent/send"
    SUBAGENT_INTERRUPT = "subagent/interrupt"
    SUBAGENT_LIST = "subagent/list"

    # Session events
    EVENTS_SUBSCRIBE = "events/subscribe"
    EVENTS_UNSUBSCRIBE = "events/unsubscribe"

    # File operations
    FILE_READ = "file/read"
    FILE_WRITE = "file/write"
    FILE_LIST = "file/list"

    # Settings
    SETTINGS_GET = "settings/get"
    SETTINGS_SET = "settings/set"


class RpcNotification(str, Enum):
    """Server-to-client notification types."""

    TURN_STARTED = "turn/started"
    TURN_COMPLETED = "turn/completed"
    TURN_FAILED = "turn/failed"
    TOOL_CALL_STARTED = "tool/call/started"
    TOOL_CALL_COMPLETED = "tool/call/completed"
    SUBAGENT_SPAWNED = "subagent/spawned"
    SUBAGENT_COMPLETED = "subagent/completed"
    MESSAGE_RECEIVED = "message/received"
    SESSION_EVENT = "session/event"


@dataclass(frozen=True, slots=True)
class InitializeParams:
    """Initialize request parameters."""

    protocol_version: int = 1
    client_info: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class InitializeResult:
    """Initialize response result."""

    protocol_version: int = 1
    server_info: dict[str, str] = field(
        default_factory=lambda: {"name": "averqel-deepspace", "version": "1.0"}
    )
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert the initialize response to its JSON wire representation."""
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InitializeResult:
        return cls(
            protocol_version=data.get("protocol_version", 1),
            server_info=data.get("server_info", {"name": "averqel-deepspace", "version": "1.0"}),
            capabilities=data.get("capabilities", {}),
        )


@dataclass(frozen=True, slots=True)
class ConversationCreateParams:
    """Create conversation parameters."""

    title: str | None = None
    preset: str = "standard"
    workspace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class ConversationCreateResult:
    """Create conversation result."""

    id: UUID
    title: str
    preset: str
    created_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationCreateResult:
        return cls(
            id=UUID(data["id"]),
            title=data["title"],
            preset=data["preset"],
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if isinstance(data["created_at"], str)
                else data["created_at"]
            ),
        )


@dataclass(frozen=True, slots=True)
class TurnRunParams:
    """Run turn parameters."""

    conversation_id: UUID
    prompt: str
    stream: bool = False
    model: str | None = None
    preset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_dict(self)
        if "conversation_id" in d:
            d["conversation_id"] = str(d["conversation_id"])
        return d


@dataclass(frozen=True, slots=True)
class TurnRunResult:
    """Run turn result."""

    turn_id: UUID
    conversation_id: UUID
    status: Literal["running", "completed", "failed", "cancelled"]
    answer: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TurnRunResult:
        return cls(
            turn_id=UUID(data["turn_id"]),
            conversation_id=UUID(data["conversation_id"]),
            status=data["status"],
            answer=data.get("answer"),
            tool_calls=data.get("tool_calls", []),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class ToolCallParams:
    """Tool call parameters."""

    conversation_id: UUID
    turn_id: UUID
    tool_name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_dict(self)
        d["conversation_id"] = str(d["conversation_id"])
        d["turn_id"] = str(d["turn_id"])
        return d


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Tool call result."""

    tool_call_id: UUID
    status: Literal["ok", "error", "cancelled"]
    result: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCallResult:
        return cls(
            tool_call_id=UUID(data["tool_call_id"]),
            status=data["status"],
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class SubagentSpawnParams:
    """Spawn subagent parameters."""

    conversation_id: UUID
    task: str
    description: str
    mode: Literal["one-shot", "continuable"] = "continuable"
    backend: Literal["in-process", "acp", "claude-code", "codex", "neosis-sdk"] = "in-process"
    model: str | None = None
    persona: str | None = None
    tool_allow: list[str] | None = None
    tool_deny: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_dict(self)
        d["conversation_id"] = str(d["conversation_id"])
        return d


@dataclass(frozen=True, slots=True)
class SubagentSpawnResult:
    """Spawn subagent result."""

    agent_id: UUID
    conversation_id: UUID
    status: Literal["running", "idle", "completed", "failed"]
    answer: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubagentSpawnResult:
        return cls(
            agent_id=UUID(data["agent_id"]),
            conversation_id=UUID(data["conversation_id"]),
            status=data["status"],
            answer=data.get("answer"),
        )


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Session event for notifications."""

    id: UUID
    conversation_id: UUID
    type: str
    turn: int | None = None
    step: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_dict(self)
        d["id"] = str(d["id"])
        d["conversation_id"] = str(d["conversation_id"])
        if d["timestamp"] is not None:
            d["timestamp"] = d["timestamp"].isoformat()
        return d


# JSON-RPC base types
def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass to a dict, omitting None values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            item.name: value
            for item in fields(obj)
            if not item.name.startswith("_") and (value := getattr(obj, item.name)) is not None
        }
    if isinstance(obj, dict):
        return dict(obj)
    raise TypeError(f"Expected a dataclass or dict, got {type(obj).__name__}")


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """JSON-RPC 2.0 request."""

    method: str
    jsonrpc: Literal["2.0"] = "2.0"
    params: dict[str, Any] | None = None
    id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return _dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """JSON-RPC 2.0 response."""

    id: int | str | None = None
    jsonrpc: Literal["2.0"] = "2.0"
    result: Any | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert the response while preserving JSON-RPC's required null ID."""
        response: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            response["error"] = self.error
        else:
            response["result"] = self.result
        return response

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JsonRpcResponse:
        """Create from a dict."""
        return cls(
            id=data.get("id"),
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """JSON-RPC 2.0 notification."""

    method: str
    jsonrpc: Literal["2.0"] = "2.0"
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return _dataclass_to_dict(self)


# Error codes
class RpcErrorCode(int, Enum):
    """JSON-RPC error codes."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # Application errors
    CONVERSATION_NOT_FOUND = -32000
    TURN_NOT_FOUND = -32001
    TOOL_NOT_FOUND = -32002
    SUBAGENT_NOT_FOUND = -32003
    PERMISSION_DENIED = -32004
    WORKSPACE_NOT_FOUND = -32005
    INVALID_STATE = -32006


def make_error(code: RpcErrorCode, message: str, data: Any = None) -> dict[str, Any]:
    """Create a JSON-RPC error object."""
    error = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = data
    return error


def make_response(
    id: int | str | None,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a JSON-RPC response."""
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def make_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a JSON-RPC notification."""
    notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        notification["params"] = params
    return notification
