"""Tests for the DeepSpace command-line interface."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from app.deepspace.cli import main
from app.deepspace.cli.client import DeepSpaceApiClient, DeepSpaceApiError

pytestmark = pytest.mark.unit_no_db


CHATS_PAYLOAD: dict[str, Any] = {
    "items": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "First conversation",
            "kind": "deepspace",
            "created_at": "2026-10-01T10:00:00+00:00",
            "updated_at": "2026-10-02T11:30:00+00:00",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "title": "Second conversation",
            "kind": "deepspace",
            "created_at": "2026-10-03T10:00:00+00:00",
            "updated_at": "2026-10-04T09:15:00+00:00",
        },
    ],
    "total": 2,
}

STREAM_BODY = (
    'event: start\ndata: {"conversation_id": "22222222-2222-2222-2222-222222222222"}\n\n'
    'event: delta\ndata: {"text": "Hello "}\n\n'
    'event: delta\ndata: {"text": "world"}\n\n'
    'event: done\ndata: {"conversation_id": "22222222-2222-2222-2222-222222222222",'
    ' "message_id": "33333333-3333-3333-3333-333333333333", "status": "ready"}\n\n'
)


class _FakeResponse:
    """The object ``urlopen`` returns when a fake transport answers."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        """Return the body the fake transport holds."""
        return self._payload

    def close(self) -> None:
        """Close the fake response."""


class _FakeTransport:
    """Answers API calls from a fixed route table and records every request.

    Args:
        routes: The response body each route path answers with.  A value
            that is an exception is raised instead of returned.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self,
        request: urllib.request.Request,
        *,
        timeout: float | None = None,
    ) -> _FakeResponse:
        """Answer one request.

        Args:
            request: The request the client sent.
            timeout: The timeout the client asked for.

        Returns:
            The fake response.

        Raises:
            AssertionError: When no route answers the request.
        """
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.full_url).path
        answer = self.routes.get(path)
        if answer is None:
            raise AssertionError(f"Unexpected request: {path}")
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)


def _json_body(payload: dict[str, Any]) -> bytes:
    """Return the encoded body of a JSON response.

    Args:
        payload: The response payload.

    Returns:
        The encoded body.
    """
    return json.dumps(payload).encode("utf-8")


def _http_error(status: int, message: str, code: str = "NOT_FOUND") -> urllib.error.HTTPError:
    """Return the error a refused request raises.

    Args:
        status: The HTTP status of the refusal.
        message: The message the server sent.
        code: The error code the server sent.

    Returns:
        The error to raise.
    """
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    return urllib.error.HTTPError(
        "http://api.test/api/v1/deepspace/chats",
        status,
        "Error",
        None,
        io.BytesIO(body),
    )


def _header(request: urllib.request.Request, name: str) -> str | None:
    """Return one request header, ignoring the case of its name.

    Args:
        request: The request the client sent.
        name: The header name.

    Returns:
        The header value, or ``None`` when the request does not carry it.
    """
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> _FakeTransport:
    """Point the client at a fake transport and a known base URL.

    Args:
        monkeypatch: The pytest fixture that restores the environment.
        routes: The routes the fake transport answers.

    Returns:
        The fake transport.
    """
    transport = _FakeTransport(routes)
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    monkeypatch.setenv("AKS_API_URL", "http://api.test")
    monkeypatch.setenv("AKS_API_TOKEN", "test-token")
    return transport


def test_workspaces_prints_the_workspaces_the_account_owns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workspace command prints every workspace the API returned."""
    payload = {
        "workspaces": [
            {
                "id": "default",
                "name": "Default",
                "path": "/home/user/workspaces/default",
                "is_default": True,
            },
            {
                "id": "billing",
                "name": "Billing",
                "path": "/home/user/workspaces/billing",
                "is_default": False,
            },
        ],
        "default": "default",
    }
    _install(monkeypatch, {"/api/v1/deepspace/chats/workspaces": _json_body(payload)})

    exit_code = main(["workspaces"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "Default" in printed
    assert "Billing" in printed
    assert "(default)" in printed


def test_chats_prints_the_conversations_it_fetched(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The chat list command prints every conversation the API returned."""
    _install(monkeypatch, {"/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD)})

    exit_code = main(["chats"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "First conversation" in printed
    assert "Second conversation" in printed
    assert "11111111-1111-1111-1111-111111111111" in printed
    assert "2 conversation(s)." in printed


def test_json_flag_prints_parseable_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON flag replaces the text with the response the API returned."""
    _install(monkeypatch, {"/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD)})

    exit_code = main(["--json", "chats"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(printed) == CHATS_PAYLOAD


def test_json_flag_after_the_command_also_prints_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON flag is accepted after the command as well as before it."""
    _install(monkeypatch, {"/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD)})

    exit_code = main(["chats", "--json"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(printed) == CHATS_PAYLOAD


def test_json_flag_before_the_command_survives_the_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A JSON flag given before the command is not lost to the subcommand."""
    _install(monkeypatch, {"/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD)})

    exit_code = main(["--json", "chats"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(printed) == CHATS_PAYLOAD


def test_api_error_exits_one_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused call reports the server's message and exits with one."""
    _install(
        monkeypatch,
        {
            "/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/messages": _http_error(
                404, "Conversation not found"
            )
        },
    )

    exit_code = main(["chat", "22222222-2222-2222-2222-222222222222"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Conversation not found" in captured.err
    assert "404" in captured.err


def test_api_error_before_the_json_flag_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused call reports the failure in JSON mode as well."""
    _install(
        monkeypatch,
        {"/api/v1/deepspace/chats": _http_error(503, "The service is restarting")},
    )

    exit_code = main(["--json", "chats"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "The service is restarting" in captured.err
    assert captured.out == ""


def test_unreachable_api_exits_one_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An API that cannot be reached reports the address and exits with one."""
    _install(
        monkeypatch,
        {"/api/v1/deepspace/chats": urllib.error.URLError("connection refused")},
    )

    exit_code = main(["chats"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "http://api.test" in captured.err
    assert "connection refused" in captured.err


def test_missing_conversation_id_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A command without its conversation id is a usage error."""
    exit_code = main(["chat"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "conversation_id" in captured.err


def test_unknown_command_exits_two() -> None:
    """A command the parser does not declare is a usage error."""
    assert main(["teleport"]) == 2


def test_run_prints_the_answer_of_the_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command prints the answer the message stream produced."""
    _install(monkeypatch, {"/api/v1/deepspace/chats/stream": STREAM_BODY.encode("utf-8")})

    exit_code = main(["run", "22222222-2222-2222-2222-222222222222", "say hello"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert printed.strip() == "Hello world"


def test_run_reports_a_failed_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stream that reports a failure exits with one."""
    body = (
        'event: error\ndata: {"code": "LLM_PROVIDER_UNAVAILABLE",'
        ' "message": "The provider is unavailable."}\n\n'
    )
    _install(monkeypatch, {"/api/v1/deepspace/chats/stream": body.encode("utf-8")})

    exit_code = main(["run", "22222222-2222-2222-2222-222222222222", "say hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "The provider is unavailable." in captured.err


def test_chat_prints_the_messages_of_one_conversation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The chat command prints the messages the API returned."""
    payload = {
        "messages": [
            {"id": "1", "role": "user", "content": "What is DeepSpace?"},
            {"id": "2", "role": "assistant", "content": "A productivity agent."},
        ],
    }
    _install(
        monkeypatch,
        {
            "/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/messages": _json_body(
                payload
            )
        },
    )

    exit_code = main(["chat", "22222222-2222-2222-2222-222222222222"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "user:" in printed
    assert "What is DeepSpace?" in printed
    assert "assistant:" in printed
    assert "A productivity agent." in printed


def test_client_builds_the_url_and_headers_for_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client sends the documented URL and the configured bearer token."""
    transport = _install(monkeypatch, {"/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD)})

    client = DeepSpaceApiClient()
    client.list_chats()

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == "http://api.test/api/v1/deepspace/chats"
    assert request.get_method() == "GET"
    assert _header(request, "Authorization") == "Bearer test-token"
    assert _header(request, "Accept") == "application/json"
    assert request.data is None


def test_client_posts_the_message_to_the_stream_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client sends the message as the body the stream route reads."""
    transport = _install(
        monkeypatch,
        {"/api/v1/deepspace/chats/stream": STREAM_BODY.encode("utf-8")},
    )

    client = DeepSpaceApiClient()
    answer = client.send_message("22222222-2222-2222-2222-222222222222", "say hello")

    request = transport.requests[0]
    assert request.full_url == "http://api.test/api/v1/deepspace/chats/stream"
    assert request.get_method() == "POST"
    assert _header(request, "Content-Type") == "application/json"
    assert _header(request, "Accept") == "text/event-stream"
    body = json.loads((request.data or b"{}").decode("utf-8"))
    assert body["message"] == "say hello"
    assert body["conversation_id"] == "22222222-2222-2222-2222-222222222222"
    assert answer["content"] == "Hello world"
    assert answer["status"] == "ready"
    assert answer["message_id"] == "33333333-3333-3333-3333-333333333333"


def test_client_calls_the_route_of_each_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every command calls the route the server serves for it."""
    transport = _install(
        monkeypatch,
        {
            "/api/v1/deepspace/chats": _json_body(CHATS_PAYLOAD),
            "/api/v1/deepspace/sandbox/backend": _json_body({"selected": "bwrap"}),
            "/api/v1/deepspace/chats/plugins": _json_body({"plugins": []}),
            "/api/v1/deepspace/chats/presets": _json_body({"presets": []}),
            "/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/terminals": _json_body(
                {"sessions": []}
            ),
            "/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/plan": _json_body(
                {"active": False, "plan": None}
            ),
        },
    )

    client = DeepSpaceApiClient()
    client.list_chats()
    client.get_sandbox_status()
    client.list_plugins()
    client.list_presets()
    client.list_terminals("22222222-2222-2222-2222-222222222222")
    client.list_plans("22222222-2222-2222-2222-222222222222")

    assert [request.full_url for request in transport.requests] == [
        "http://api.test/api/v1/deepspace/chats",
        "http://api.test/api/v1/deepspace/sandbox/backend",
        "http://api.test/api/v1/deepspace/chats/plugins",
        "http://api.test/api/v1/deepspace/chats/presets",
        "http://api.test/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/terminals",
        "http://api.test/api/v1/deepspace/chats/22222222-2222-2222-2222-222222222222/plan",
    ]


def test_the_client_uses_the_environment_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client reads its base URL and token from the environment."""
    monkeypatch.setenv("AKS_API_URL", "http://environment.test/")
    monkeypatch.setenv("AKS_API_TOKEN", "environment-token")

    client = DeepSpaceApiClient()

    assert client.base_url == "http://environment.test"
    assert client.token == "environment-token"


def test_the_client_falls_back_to_the_local_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client uses the local API when the environment names none."""
    monkeypatch.delenv("AKS_API_URL", raising=False)
    monkeypatch.delenv("AKS_API_TOKEN", raising=False)

    client = DeepSpaceApiClient()

    assert client.base_url == "http://127.0.0.1:8000"
    assert client.token == ""


def test_a_refused_call_raises_an_error_with_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused call raises the error that carries the status and message."""
    _install(
        monkeypatch,
        {"/api/v1/deepspace/chats": _http_error(403, "Insufficient permissions")},
    )

    client = DeepSpaceApiClient()
    with pytest.raises(DeepSpaceApiError) as raised:
        client.list_chats()

    assert raised.value.status == 403
    assert raised.value.code == "NOT_FOUND"
    assert raised.value.message == "Insufficient permissions"
    assert "403" in str(raised.value)
