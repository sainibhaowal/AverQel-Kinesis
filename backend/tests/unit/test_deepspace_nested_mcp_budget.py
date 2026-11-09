from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.mcp_tool_broker import MCPToolBroker

pytestmark = pytest.mark.unit_no_db


def test_nested_mcp_catalog_calls_use_the_shared_turn_budget() -> None:
    service = object.__new__(DeepSpaceChatService)
    service.mcp_broker = MCPToolBroker()
    service.mcp_max_calls_per_turn = 5
    service.mcp_max_discovery_calls_per_turn = 1
    service.mcp_max_result_chars_per_turn = 10_000
    budget = {"calls": 0, "discovery": 0, "result_chars": 0}
    binding = SimpleNamespace(
        server_name="Calendar",
        raw_name="find_events",
        exposed_name="mcp_calendar_find_events",
        catalog={
            "description": "Find calendar events.",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    )
    common = {
        "auth": SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        "conversation_id": uuid.uuid4(),
        "bindings": {binding.exposed_name: binding},
        "permission_mode": "read-only",
        "budget": budget,
    }

    first = asyncio.run(
        service._execute_nested_mcp_broker_call(
            arguments={"operation": "search", "query": "calendar events"},
            **common,
        )
    )
    second = asyncio.run(
        service._execute_nested_mcp_broker_call(
            arguments={"operation": "search", "query": "calendar events"},
            **common,
        )
    )

    assert first["status"] == "ok"
    assert budget["discovery"] == 1
    assert budget["result_chars"] > 0
    assert second["status"] == "budget_exhausted"
