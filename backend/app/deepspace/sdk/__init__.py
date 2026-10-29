"""DeepSpace SDK package."""

from __future__ import annotations

from app.deepspace.sdk.client import (
    Conversation,
    DeepSpaceClient,
    DeepSpaceHttpClient,
    DeepSpaceSdkError,
    DeepSpaceStdioClient,
    Turn,
    connect,
    run_task,
)
from app.deepspace.sdk.protocol import (
    ConversationCreateParams,
    ConversationCreateResult,
    InitializeParams,
    InitializeResult,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    RpcErrorCode,
    RpcMethod,
    RpcNotification,
    SessionEvent,
    SubagentSpawnParams,
    SubagentSpawnResult,
    ToolCallParams,
    ToolCallResult,
    TurnRunParams,
    TurnRunResult,
    make_error,
    make_notification,
    make_response,
)

__all__ = [
    "DeepSpaceClient",
    "DeepSpaceHttpClient",
    "DeepSpaceStdioClient",
    "DeepSpaceSdkError",
    "Conversation",
    "Turn",
    "connect",
    "run_task",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcNotification",
    "RpcMethod",
    "RpcNotification",
    "InitializeParams",
    "InitializeResult",
    "ConversationCreateParams",
    "ConversationCreateResult",
    "TurnRunParams",
    "TurnRunResult",
    "ToolCallParams",
    "ToolCallResult",
    "SubagentSpawnParams",
    "SubagentSpawnResult",
    "SessionEvent",
    "RpcErrorCode",
    "make_error",
    "make_response",
    "make_notification",
]

__version__ = "1.0.0"
