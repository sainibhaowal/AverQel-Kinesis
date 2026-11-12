from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from app.providers.services import codex_cli_runtime as runtime_module
from app.providers.services.codex_cli_runtime import (
    CODEX_CLI_PERMISSION_PROFILE,
    CodexAppServerClient,
    CodexCliRuntime,
    codex_cli_dynamic_tools,
    codex_cli_input,
    create_restricted_codex_cli_app_server_args,
    sanitize_codex_process_env,
)

pytestmark = pytest.mark.unit_no_db


class _FakeAppServer:
    def __init__(
        self,
        executable: str,
        *,
        cwd: str,
        env: dict[str, str],
        on_notification: Any = None,
        on_server_request: Any = None,
        on_server_request_complete: Any = None,
        on_closed: Any = None,
        **_kwargs: Any,
    ) -> None:
        self.executable = executable
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.on_server_request_complete = on_server_request_complete
        self.on_closed = on_closed
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def start(self) -> None:
        return None

    async def notify(self, _method: str, _params: dict[str, Any]) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "permissionProfile/list":
            return {
                "data": [
                    {"id": CODEX_CLI_PERMISSION_PROFILE, "allowed": True},
                ]
            }
        if method == "account/read":
            return {
                "account": {
                    "type": "chatgpt",
                    "email": "codex-user@example.test",
                    "planType": "plus",
                }
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "gpt-5-codex",
                        "displayName": "GPT-5 Codex",
                        "hidden": False,
                        "supportedReasoningEfforts": ["low", "high", "xhigh"],
                    }
                ],
                "nextCursor": None,
            }
        if method == "thread/start":
            return {
                "activePermissionProfile": {"id": CODEX_CLI_PERMISSION_PROFILE},
                "thread": {"id": "thread-test"},
            }
        if method == "turn/start":
            if self.on_notification:
                await self.on_notification("item/agentMessage/delta", {"delta": "Answer."})
            if self.on_server_request:
                params = {
                    "tool": "lookup",
                    "arguments": {"query": "weather"},
                    "callId": "call-1",
                }
                await self.on_server_request("item/tool/call", params)
                if self.on_server_request_complete:
                    await self.on_server_request_complete("item/tool/call", params)
            return {}
        return {}

    async def close(self) -> None:
        self.closed = True


def _runtime() -> CodexCliRuntime:
    async def login_status(_executable: str, **_kwargs: Any) -> str:
        return "chatgpt"

    return CodexCliRuntime(
        executable_resolver=lambda **_kwargs: "/usr/bin/codex",
        app_server_factory=_FakeAppServer,
        login_status_runner=login_status,
        env={"PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex-cli-test"},
        home="/tmp/codex-home-test",
    )


def test_codex_app_server_profile_disables_local_agent_capabilities() -> None:
    args = create_restricted_codex_cli_app_server_args()

    assert args[:3] == ["app-server", "--listen", "stdio://"]
    assert "features.shell_tool=false" in args
    assert "features.multi_agent=false" in args
    assert "mcp_servers={}" in args
    assert "web_search='disabled'" in args
    assert "history.persistence='none'" in args
    assert any("filesystem" in value and "':root'='deny'" in value for value in args)


def test_codex_child_environment_drops_credentials() -> None:
    result = sanitize_codex_process_env(
        {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/home/test/.codex",
            "OPENAI_API_KEY": "secret",
            "SERVICE_TOKEN": "secret",
        }
    )

    assert result == {
        "PATH": "/usr/bin",
        "CODEX_HOME": "/home/test/.codex",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }


def test_codex_app_server_transport_uses_json_lines_and_answers_server_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStdin:
        def __init__(self, stdout: asyncio.StreamReader) -> None:
            self.stdout = stdout
            self.packets: list[dict[str, Any]] = []
            self.server_response_sent = asyncio.Event()

        def write(self, raw: bytes) -> None:
            packet = json.loads(raw)
            self.packets.append(packet)
            if "id" in packet and "method" in packet:
                self.stdout.feed_data(
                    json.dumps({"id": packet["id"], "result": {}}).encode() + b"\n"
                )
            elif packet.get("id") == "approval-1":
                self.server_response_sent.set()

        async def drain(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdin = FakeStdin(self.stdout)
            self.stderr = None
            self.returncode = None

        def terminate(self) -> None:
            self.returncode = -15
            self.stdout.feed_eof()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.feed_eof()

        async def wait(self) -> int:
            return int(self.returncode or 0)

    process_holder: dict[str, FakeProcess] = {}

    async def create_process(*_args: Any, **_kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        process_holder["process"] = process
        return process

    monkeypatch.setattr(runtime_module.asyncio, "create_subprocess_exec", create_process)
    server_requests: list[tuple[str, dict[str, Any]]] = []

    async def answer_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        server_requests.append((method, params))
        return {"decision": "decline"}

    async def run() -> None:
        client = CodexAppServerClient(
            "/usr/bin/codex",
            cwd="/tmp",
            on_server_request=answer_request,
        )
        await client.start()
        process = process_holder["process"]
        process.stdout.feed_data(
            b'{"id":"approval-1","method":"item/permissions/requestApproval","params":{}}\n'
        )
        await asyncio.wait_for(process.stdin.server_response_sent.wait(), timeout=1)
        assert [packet.get("method") for packet in process.stdin.packets] == [
            "initialize",
            "initialized",
            None,
        ]
        assert process.stdin.packets[-1] == {
            "id": "approval-1",
            "result": {"decision": "decline"},
        }
        await client.close()

    asyncio.run(run())
    assert server_requests == [("item/permissions/requestApproval", {})]


def test_codex_responses_input_and_tools_are_translated_for_app_server() -> None:
    text, images = codex_cli_input(
        [
            {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
            {"type": "function_call", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "Sunny"},
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
            },
        ]
    )
    tools = codex_cli_dynamic_tools(
        [
            {
                "type": "namespace",
                "name": "averqel",
                "description": "AverQel tools",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "description": "Look up a value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                ],
            }
        ]
    )

    assert "USER:\nHello" in text
    assert "ASSISTANT TOOL CALL (lookup):" in text
    assert "TOOL RESULT (call-1):\nSunny" in text
    assert "[Image attached]" in text
    assert images == ["data:image/png;base64,AAAA"]
    assert tools[0]["tools"][0]["name"] == "lookup"
    assert tools[0]["tools"][0]["inputSchema"]["properties"]["query"]["type"] == "string"


def test_codex_backend_status_uses_cli_app_server_catalog_and_opaque_account_id() -> None:
    runtime = _runtime()

    result = asyncio.run(runtime.status())

    expected_payload = "codex-cli\0/usr/bin/codex\0/tmp/codex-cli-test\0codex-user@example.test"
    expected_id = hashlib.sha256(expected_payload.encode()).hexdigest()[:32]
    assert result["available"] is True
    assert result["accounts"] == [
        {
            "id": expected_id,
            "email": None,
            "name": "Codex CLI ChatGPT login",
            "connected": True,
            "source": "codex_cli",
            "planType": "plus",
        }
    ]
    assert result["models"] == [
        {
            "id": "gpt-5-codex",
            "name": "GPT-5 Codex",
            "kind": "chat",
            "supportedReasoningEfforts": ["low", "high", "very_high"],
            "reasoningEffortValues": {"low": "low", "high": "high", "very_high": "xhigh"},
        }
    ]


def test_codex_backend_status_fails_closed_without_restricted_profile() -> None:
    class UnsupportedProfileServer(_FakeAppServer):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "permissionProfile/list":
                return {"data": []}
            return await super().request(method, params)

    runtime = _runtime()
    runtime.app_server_factory = UnsupportedProfileServer

    result = asyncio.run(runtime.status())

    assert result["available"] is False
    assert result["accounts"] == []
    assert "restricted inference profile" in result["message"]


def test_codex_local_backend_mode_requires_standalone_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVERQEL_HOME", raising=False)
    monkeypatch.delenv("AVERQEL_DESKTOP_LOCAL_RUNTIME", raising=False)
    assert runtime_module.local_codex_cli_backend_enabled("memory://") is False
    monkeypatch.setenv("AVERQEL_HOME", "/tmp/kinesis-test-home")
    assert runtime_module.local_codex_cli_backend_enabled("memory://") is False
    monkeypatch.setenv("AVERQEL_DESKTOP_LOCAL_RUNTIME", "1")
    assert runtime_module.local_codex_cli_backend_enabled("memory://") is True
    assert runtime_module.local_codex_cli_backend_enabled("redis://localhost:6379/4") is False


def test_codex_cli_stream_uses_ephemeral_restricted_turn_and_returns_tool_call() -> None:
    runtime = _runtime()
    app_servers: list[_FakeAppServer] = []
    original_factory = runtime.app_server_factory

    def capture_app_server(*args: Any, **kwargs: Any) -> _FakeAppServer:
        client = original_factory(*args, **kwargs)
        app_servers.append(client)
        return client

    runtime.app_server_factory = capture_app_server  # type: ignore[method-assign]
    runtime.matches_current_cli_account = lambda _account_id: True  # type: ignore[method-assign]
    account_id = runtime_module._account_id(
        "/usr/bin/codex",
        "codex-user@example.test",
        env=runtime.env,
        home=runtime.home,
    )
    payload = {
        "model": "gpt-5-codex",
        "instructions": "Use the app policy.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Check weather."}]}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "tool_choice": "required",
        "tools": [
            {
                "type": "namespace",
                "name": "averqel",
                "description": "Available tools",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "description": "Look up weather.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
    }

    async def collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in runtime.stream_responses(account_id=account_id, payload=payload)
        ]

    events = asyncio.run(collect())

    assert [event["type"] for event in events] == [
        "response.output_text.delta",
        "response.output_item.added",
        "response.function_call_arguments.done",
        "response.completed",
    ]
    assert events[1]["item"]["name"] == "lookup"
    assert events[2]["arguments"] == '{"query":"weather"}'
    thread_params = next(
        params for method, params in app_servers[0].requests if method == "thread/start"
    )
    assert "disposable inference scratch directory" in thread_params["developerInstructions"]
    assert "not the conversation workspace" in thread_params["developerInstructions"]
    assert "supplied AverQel tools" in thread_params["developerInstructions"]
    assert runtime.active_streams == 0
