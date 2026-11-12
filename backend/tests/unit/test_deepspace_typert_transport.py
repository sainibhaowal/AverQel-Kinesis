from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import AuthContext
from app.deepspace.typert import api
from app.deepspace.typert import remote as remote_client
from app.deepspace.typert.codecs import JsonCodec
from app.deepspace.typert.descriptors import RemoteContribution, RemoteDescriptor, RemoteParameter
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.invocation import RemoteInvocation
from app.deepspace.typert.principal import Principal
from app.deepspace.typert.registry import TypertRegistry
from app.deepspace.typert.remote import TypertRemoteClient
from app.deepspace.typert.resources import GLOBAL, ResourceRef
from app.deepspace.typert.streams import RemoteStream

pytestmark = pytest.mark.unit_no_db


def _auth(*, permissions: frozenset[str] = frozenset()) -> AuthContext:
    return AuthContext(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=frozenset({"user"}),
        token_id="test-token",
        permissions=permissions,
    )


def _registry(descriptor: RemoteDescriptor) -> TypertRegistry:
    registry = TypertRegistry()
    registry.register(RemoteContribution(package="test.capabilities", descriptors=(descriptor,)))
    return registry


def test_remote_unary_invocation_requires_declared_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = RemoteDescriptor(
        id="test/read",
        service="test",
        namespace="test",
        method="read",
        implementation=lambda: {"ok": True},
        scope="queries:run",
        resources=(GLOBAL,),
    )
    registry = _registry(descriptor)
    monkeypatch.setattr(api, "get_typert_service", lambda: SimpleNamespace(registry=registry))

    auth = _auth()
    assert api._authorize_invocation(auth, "test/read") is descriptor
    result = asyncio.run(
        api.invoke_typert_capability(
            {"endpoint": "test/read", "arguments": {}},
            auth,
        )
    )
    assert result == {"result": {"ok": True}}


def test_remote_invocation_fails_closed_for_unscoped_or_resource_bound_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unscoped = RemoteDescriptor(
        id="test/unscoped",
        service="test",
        namespace="test",
        method="unscoped",
        implementation=lambda: None,
        resources=(GLOBAL,),
    )
    resource_bound = RemoteDescriptor(
        id="test/workspace",
        service="test",
        namespace="test",
        method="workspace",
        implementation=lambda: None,
        scope="queries:run",
        resources=(ResourceRef("workspace", "workspace-1"),),
    )
    registry = TypertRegistry()
    registry.register(
        RemoteContribution(
            package="test.capabilities",
            descriptors=(unscoped, resource_bound),
        )
    )
    monkeypatch.setattr(api, "get_typert_service", lambda: SimpleNamespace(registry=registry))

    with pytest.raises(RemoteError) as unscoped_error:
        api._authorize_invocation(_auth(), "test/unscoped")
    assert unscoped_error.value.code == "gateway/forbidden"
    with pytest.raises(RemoteError) as resource_error:
        api._authorize_invocation(_auth(permissions=frozenset({"queries:run"})), "test/workspace")
    assert resource_error.value.code == "resource/denied"

    secured = RemoteDescriptor(
        id="test/admin",
        service="test",
        namespace="test",
        method="admin",
        implementation=lambda: None,
        scope="admin:metrics:read",
        resources=(GLOBAL,),
    )
    secured_registry = _registry(secured)
    monkeypatch.setattr(
        api,
        "get_typert_service",
        lambda: SimpleNamespace(registry=secured_registry),
    )
    with pytest.raises(RemoteError) as permission_error:
        api._authorize_invocation(_auth(), "test/admin")
    assert permission_error.value.code == "gateway/forbidden"


def test_stream_invocation_keeps_ownership_until_stream_close() -> None:
    stream = RemoteStream[dict[str, bool], dict[str, str]](window_bytes=128)
    descriptor = RemoteDescriptor(
        id="test/duplex",
        service="test",
        namespace="test",
        method="duplex",
        implementation=lambda: stream,
        mode="stream",
        uplink_codec=JsonCodec(),
        result_codec=JsonCodec(),
        scope="queries:run",
        resources=(GLOBAL,),
    )
    registry = _registry(descriptor)
    invocation = RemoteInvocation(principal=Principal(kind="user", id="user-1"))

    async def exercise() -> None:
        live_stream = await registry.ainvoke("test/duplex", invocation=invocation)
        assert live_stream is stream
        assert invocation.cancellation.cancelled is False
        await live_stream.handle().send({"text": "hello"})
        assert await live_stream.receive_uplink() == {"text": "hello"}
        live_stream.publish({"ok": True})
        assert await live_stream.receive() == {"ok": True}
        await live_stream.close()
        assert invocation.cancellation.cancelled is True

    asyncio.run(exercise())


def test_authenticated_websocket_carries_both_stream_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _auth()
    streams: list[RemoteStream[dict[str, object], dict[str, object]]] = []
    cancellation_tokens = []

    async def create_echo_stream(*, signal):
        stream: RemoteStream[dict[str, object], dict[str, object]] = RemoteStream(
            out_codec=JsonCodec(),
            in_codec=JsonCodec(),
        )
        streams.append(stream)
        cancellation_tokens.append(signal)

        async def echo() -> None:
            value = await stream.receive_uplink()
            stream.publish({"echo": value})
            stream.finish()

        asyncio.create_task(echo())
        return stream

    descriptor = RemoteDescriptor(
        id="test/echo-stream",
        service="test",
        namespace="test",
        method="echo_stream",
        implementation=create_echo_stream,
        mode="stream",
        uplink_codec=JsonCodec(),
        cancellation_parameter="signal",
        result_codec=JsonCodec(),
        scope="queries:run",
        resources=(GLOBAL,),
    )
    registry = _registry(descriptor)
    monkeypatch.setattr(api, "get_typert_service", lambda: SimpleNamespace(registry=registry))

    from app.core import config
    from app.deepspace.api import chats
    from app.platform.database import session as database_session

    class FakeDb:
        def close(self) -> None:
            return None

    async def fake_auth(*_args, **_kwargs):
        return auth

    monkeypatch.setattr(chats, "_authenticate_websocket_auth_context", fake_auth)
    monkeypatch.setattr(chats, "_require_websocket_permissions", lambda _auth: None)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(database_session, "SessionLocal", FakeDb)

    app = FastAPI()
    app.include_router(api.stream_router)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/deepspace/typert/invoke/stream",
            headers={"Authorization": "Bearer test"},
        ) as socket:
            socket.send_json({"endpoint": "test/echo-stream", "arguments": {}})
            assert socket.receive_json() == {"event": "ready", "endpoint": "test/echo-stream"}
            socket.send_json({"event": "uplink", "value": {"text": "hello"}})
            assert socket.receive_json() == {
                "event": "downlink",
                "value": {"echo": {"text": "hello"}},
            }
            assert socket.receive_json() == {"event": "complete"}
            socket.send_json({"event": "end"})

    assert streams[0].closed is True
    assert cancellation_tokens[0].cancelled is True


def test_python_remote_client_sends_bearer_header_and_decodes_unary_result() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {"ok": True}})

    async def exercise() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client = TypertRemoteClient(
            "https://example.test/api/v1",
            "secret-token",
            http_client=http,
        )
        try:
            result = await client.invoke("test/read", {"limit": 3})
        finally:
            await client.close()
            await http.aclose()
        assert result == {"ok": True}

    asyncio.run(exercise())

    assert requests[0].url.path == "/api/v1/deepspace/typert/invoke"
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    assert requests[0].url.query == b""


def test_python_remote_client_invokes_authenticated_fastapi_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = RemoteDescriptor(
        id="test/remote-read",
        service="test",
        namespace="test",
        method="remote_read",
        implementation=lambda value: {"value": value},
        parameters=(RemoteParameter(name="value"),),
        scope="queries:run",
        resources=(GLOBAL,),
    )
    registry = _registry(descriptor)
    monkeypatch.setattr(api, "get_typert_service", lambda: SimpleNamespace(registry=registry))
    app = FastAPI()
    auth = _auth(permissions=frozenset({"queries:run"}))
    app.dependency_overrides[api.get_auth_context] = lambda: auth
    app.dependency_overrides[api.get_db] = lambda: object()
    app.include_router(api.router, prefix="/api/v1")

    async def exercise() -> None:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://deepspace.test",
        )
        client = TypertRemoteClient(
            "http://deepspace.test/api/v1",
            "test-token",
            http_client=http,
        )
        try:
            result = await client.invoke("test/remote-read", {"value": "round-trip"})
        finally:
            await http.aclose()
        assert result == {"value": "round-trip"}

    asyncio.run(exercise())


def test_python_remote_client_raises_typed_unary_error() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "gateway/forbidden",
                    "message": "Denied",
                    "details": {"scope": "admin"},
                }
            },
        )

    async def exercise() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client = TypertRemoteClient("https://example.test", "token", http_client=http)
        with pytest.raises(RemoteError) as error:
            await client.invoke("test/private")
        assert error.value.code == "gateway/forbidden"
        assert error.value.details == {"scope": "admin"}
        await http.aclose()

    asyncio.run(exercise())


def test_python_remote_client_bidirectional_stream_uses_header_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.incoming = [
                '{"event":"ready","endpoint":"test/echo"}',
                '{"event":"downlink","value":{"echo":"hi"}}',
                '{"event":"complete"}',
            ]
            self.closed = False

        async def send(self, value: str) -> None:
            self.sent.append(value)

        async def recv(self) -> str:
            return self.incoming.pop(0)

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    connect_args: list[tuple[str, dict[str, object]]] = []

    async def fake_connect(uri: str, **kwargs: object) -> FakeConnection:
        connect_args.append((uri, kwargs))
        return connection

    monkeypatch.setattr(remote_client, "connect", fake_connect)

    async def exercise() -> None:
        client = TypertRemoteClient("https://example.test/api/v1", "secret-token")
        stream = await client.open_stream("test/echo", {"mode": "duplex"})
        await stream.send("hi")
        await stream.end()
        assert await stream.receive() == {"echo": "hi"}
        with pytest.raises(RemoteError) as completed:
            await stream.receive()
        assert completed.value.code == "stream/closed"
        await stream.close()
        await client.close()

    asyncio.run(exercise())

    assert connect_args[0][0] == "wss://example.test/api/v1/deepspace/typert/invoke/stream"
    assert connect_args[0][1]["additional_headers"] == {"Authorization": "Bearer secret-token"}
    assert json.loads(connection.sent[0]) == {
        "endpoint": "test/echo",
        "arguments": {"mode": "duplex"},
    }
    assert json.loads(connection.sent[1]) == {"event": "uplink", "value": "hi"}
    assert connection.sent[2] == '{"event":"end"}'
    assert connection.closed is True


def test_shared_websocket_auth_accepts_bearer_header() -> None:
    from app.deepspace.api.chats import _authenticate_websocket_auth_context

    websocket = SimpleNamespace(
        headers={"authorization": "Bearer local-token"},
        query_params={},
    )
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))

    auth = asyncio.run(
        _authenticate_websocket_auth_context(
            websocket,
            db=db,
            settings=SimpleNamespace(),
        )
    )

    assert auth.auth_type == "local"
    assert auth.token_id == "local-token"


@pytest.mark.parametrize(
    ("base_url", "token", "timeout"),
    [
        ("file:///tmp/api", "token", 1.0),
        ("https://user:password@example.test", "token", 1.0),
        ("https://example.test/api?token=secret", "token", 1.0),
        ("https://example.test", " ", 1.0),
        ("https://example.test", "token", 0.0),
        ("https://example.test", "token", float("inf")),
    ],
)
def test_python_remote_client_rejects_unsafe_configuration(
    base_url: str,
    token: str,
    timeout: float,
) -> None:
    with pytest.raises(ValueError):
        TypertRemoteClient(base_url, token, timeout=timeout)


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(200, content=b"not-json"), "gateway/internal"),
        (httpx.Response(200, json={"unexpected": True}), "gateway/internal"),
        (
            httpx.Response(
                403,
                json={"detail": {"code": "extension/private", "message": "unknown"}},
            ),
            "gateway/internal",
        ),
    ],
)
def test_python_remote_client_rejects_malformed_or_unknown_responses(
    response: httpx.Response,
    expected_code: str,
) -> None:
    async def exercise() -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: response),
            base_url="https://example.test",
        )
        client = TypertRemoteClient("https://example.test", "token", http_client=http)
        with pytest.raises(RemoteError) as error:
            await client.invoke("test/read")
        assert error.value.code == expected_code
        await http.aclose()

    asyncio.run(exercise())


def test_python_remote_stream_rejects_invalid_and_oversized_frames() -> None:
    with pytest.raises(RemoteError, match="invalid JSON"):
        remote_client._decode_frame("{")
    with pytest.raises(RemoteError, match="not an object"):
        remote_client._decode_frame("[]")
    with pytest.raises(RemoteError, match="1 MB"):
        remote_client._decode_frame('{"text":"' + ("é" * 600_000) + '"}')


def test_python_remote_stream_rejects_oversized_or_non_json_uplink() -> None:
    class FakeConnection:
        sent: list[str] = []

        async def send(self, _value: str) -> None:
            self.sent.append(_value)

        async def close(self) -> None:
            return None

    stream = remote_client.RemoteTypertStream(FakeConnection())

    async def exercise() -> None:
        with pytest.raises(RemoteError, match="1 MB"):
            await stream.send("x" * 1_000_001)
        with pytest.raises(RemoteError, match="JSON-compatible"):
            await stream.send(object())
        assert stream._connection.sent == []
        await stream.end()
        with pytest.raises(RemoteError, match="ended"):
            await stream.send("late")
        await stream.close()
        await stream.close()
        with pytest.raises(RemoteError, match="closed"):
            await stream.receive()

    asyncio.run(exercise())


def test_python_remote_stream_propagates_errors_and_rejects_unknown_frames() -> None:
    class FakeConnection:
        def __init__(self, frames: list[str]) -> None:
            self.frames = iter(frames)

        async def recv(self) -> str:
            return next(self.frames)

        async def close(self) -> None:
            return None

        async def send(self, _value: str) -> None:
            return None

    async def assert_error(frame: str, expected_code: str) -> None:
        stream = remote_client.RemoteTypertStream(FakeConnection([frame]))
        with pytest.raises(RemoteError) as error:
            await stream.receive()
        assert error.value.code == expected_code

    async def exercise() -> None:
        await assert_error(
            '{"event":"error","code":"gateway/forbidden","message":"denied"}',
            "gateway/forbidden",
        )
        await assert_error('{"event":"other"}', "gateway/internal")
        stream = remote_client.RemoteTypertStream(FakeConnection([]))
        with pytest.raises(RemoteError) as disconnected:
            await stream.receive()
        assert disconnected.value.code == "gateway/unavailable"
        await stream.cancel()

    asyncio.run(exercise())


def test_python_remote_stream_handshake_error_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        closed = False

        async def send(self, _value: str) -> None:
            return None

        async def recv(self) -> str:
            return '{"event":"error","code":"gateway/forbidden","message":"denied"}'

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    async def fake_connect(_uri: str, **_kwargs: object) -> FakeConnection:
        return connection

    monkeypatch.setattr(remote_client, "connect", fake_connect)

    async def exercise() -> None:
        client = TypertRemoteClient("https://example.test", "token")
        with pytest.raises(RemoteError) as error:
            await client.open_stream("test/private")
        assert error.value.code == "gateway/forbidden"
        assert connection.closed is True
        await client.close()

    asyncio.run(exercise())
