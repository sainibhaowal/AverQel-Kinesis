from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from app.deepspace.api import codex_runtime

pytestmark = pytest.mark.unit_no_db


class _FakeRedis:
    def __init__(self) -> None:
        self.eval_calls: list[tuple[Any, ...]] = []
        self.deleted: list[str] = []
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.closed = False

    async def eval(self, *args: Any) -> str:
        self.eval_calls.append(args)
        return json.dumps(
            {
                "user_id": "00000000-0000-0000-0000-000000000002",
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "roles": ["user"],
                "permissions": ["queries:run"],
                "token_id": "test-token",
            }
        )

    async def delete(self, *keys: str) -> None:
        self.deleted.extend(keys)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.set_calls.append((key, value, ex))

    async def blpop(self, _key: str, timeout: int) -> None:
        await asyncio.sleep(timeout)
        return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeWebSocket:
    def __init__(self) -> None:
        self.query_params = {"ticket": "single-use-ticket"}
        self.state = SimpleNamespace()
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, packet: dict[str, Any]) -> None:
        self.sent.append(packet)

    async def receive_json(self) -> dict[str, str]:
        if not any(packet.get("event") == "pong" for packet in self.sent):
            return {"event": "ping"}
        raise WebSocketDisconnect(code=1000)

    async def close(self, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_memory_relay_keeps_authenticated_codex_socket_online_without_redis_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = _FakeRedis()
    websocket = _FakeWebSocket()
    registrations: list[tuple[str, str, Any, str]] = []
    unregistrations: list[tuple[str, str, Any, str]] = []

    async def register(tenant_id: str, user_id: str, ws: Any, *, channel: str) -> None:
        registrations.append((tenant_id, user_id, ws, channel))

    def unregister(tenant_id: str, user_id: str, *, channel: str, websocket: Any) -> None:
        unregistrations.append((tenant_id, user_id, websocket, channel))

    monkeypatch.setattr(
        codex_runtime,
        "get_settings",
        lambda: SimpleNamespace(redis_url="memory://"),
    )
    monkeypatch.setattr(
        codex_runtime.aioredis,
        "from_url",
        lambda *_args, **_kwargs: redis_client,
    )
    monkeypatch.setattr(
        codex_runtime.client_proxy_registry,
        "register_client",
        register,
    )
    monkeypatch.setattr(
        codex_runtime.client_proxy_registry,
        "unregister_client",
        unregister,
    )
    monkeypatch.setattr(
        codex_runtime.client_proxy_registry,
        "get_client_connection",
        lambda *_args, **_kwargs: websocket,
    )

    await codex_runtime.codex_runtime_websocket(websocket)  # type: ignore[arg-type]

    assert len(redis_client.eval_calls) == 1  # Consume the one-use ticket only.
    request_key = (
        "averqel:codex-runtime:request:00000000-0000-0000-0000-000000000001:"
        "00000000-0000-0000-0000-000000000002"
    )
    online_key = (
        "averqel:codex-runtime:online:00000000-0000-0000-0000-000000000001:"
        "00000000-0000-0000-0000-000000000002"
    )
    assert redis_client.deleted == [request_key, online_key, request_key]
    assert len(redis_client.set_calls) == 2
    assert all(call[0] == online_key for call in redis_client.set_calls)
    assert all(call[2] == codex_runtime.CODEX_ONLINE_TTL_SECONDS for call in redis_client.set_calls)
    assert [packet["event"] for packet in websocket.sent] == [
        "codex_runtime_ready",
        "pong",
    ]
    assert registrations == [
        (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            websocket,
            "codex",
        )
    ]
    assert unregistrations == registrations
    assert redis_client.closed
