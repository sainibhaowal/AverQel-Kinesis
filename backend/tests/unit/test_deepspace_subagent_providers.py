"""Tests for subagent providers."""

from __future__ import annotations

import json

import pytest

from app.deepspace.services.subagent_providers import (
    AcpSubagentProvider,
    ClaudeCodeSubagentProvider,
    CodexSubagentProvider,
    SubagentProvider,
    SubagentResult,
    SubagentRunSpec,
    get_provider,
    initialize_providers_from_settings,
    list_providers,
    register_provider,
)


class MockProvider(SubagentProvider):
    name = "mock"

    def __init__(self, result: SubagentResult):
        self._result = result

    async def run(self, spec: SubagentRunSpec, cancel_check) -> SubagentResult:
        return self._result


class _FakeSubprocessHandle:
    def __init__(self, process):
        self.process = process

    def terminate(self):
        self.process.terminate()

    async def terminate_and_wait(self):
        self.terminate()


def test_subagent_run_spec():
    spec = SubagentRunSpec(
        task="Test task",
        persona="Test persona",
        model="test-model",
        provider="test-provider",
        tool_allow=["read", "write"],
        tool_deny=["delete"],
        cwd="/tmp",
        env={"TEST": "value"},
        depth=1,
    )
    assert spec.task == "Test task"
    assert spec.persona == "Test persona"
    assert spec.model == "test-model"
    assert spec.provider == "test-provider"
    assert spec.tool_allow == ["read", "write"]
    assert spec.tool_deny == ["delete"]
    assert spec.cwd == "/tmp"
    assert spec.env == {"TEST": "value"}
    assert spec.depth == 1


def test_subagent_result():
    result = SubagentResult(
        status="completed",
        answer="Test answer",
        tool_calls=[{"name": "read", "arguments": {}}],
    )
    assert result.status == "completed"
    assert result.answer == "Test answer"
    assert len(result.tool_calls) == 1


def test_provider_registry():
    # Clear any existing providers
    import app.deepspace.services.subagent_providers as providers_module

    providers_module._PROVIDERS.clear()

    mock_result = SubagentResult(status="completed", answer="Mock result")
    mock_provider = MockProvider(mock_result)

    register_provider(mock_provider)
    assert "mock" in list_providers()

    provider = get_provider("mock")
    assert provider is mock_provider

    # Test get_provider for non-existent
    assert get_provider("nonexistent") is None


def test_initialize_providers_from_settings(caplog):
    # Clear any existing providers
    import app.deepspace.services.subagent_providers as providers_module

    providers_module._PROVIDERS.clear()

    class FakeSettings:
        deepspace_subagent_providers = {
            "acp": {
                "command": "dsh",
                "args": ["--profile", "acp"],
                "env": {},
            },
            "claude-code": {
                "command": "claude",
                "args": ["-p"],
                "permission_mode": "dontAsk",
            },
            "codex": {
                "command": "codex",
                "args": ["exec", "--json"],
                "ask_for_approval": "never",
                "sandbox_mode": "read-only",
            },
            "dsh-sdk": {
                "command": "dsh",
                "profile": "sdk",
                "dsh_home": "/tmp/test-dsh-home",
            },
            "kinesis-sdk": {
                "command": "kinesis",
                "profile": "sdk",
                "kinesis_home": "/tmp/test-kinesis-home",
            },
            "neosis-sdk": {
                "command": "neosis",
                "args": ["headless", "--profile", "sdk-minimal"],
                "env": {},
            },
        }
        deepspace_subagent_default_backend = "in-process"

    # This should not raise even if commands don't exist
    initialize_providers_from_settings(FakeSettings())

    # Providers should be registered (though they may fail to start later)
    # The registration itself should succeed
    assert (
        "acp" in list_providers()
        or "claude-code" in list_providers()
        or "codex" in list_providers()
    )
    assert get_provider("neosis-sdk") is None
    assert get_provider("dsh-sdk") is not None
    assert get_provider("kinesis-sdk") is not None
    codex_provider = get_provider("codex")
    assert isinstance(codex_provider, CodexSubagentProvider)
    assert codex_provider.base_args == ["exec", "--json"]
    assert codex_provider.ask_for_approval == "never"
    assert codex_provider.sandbox_mode == "read-only"
    claude_provider = get_provider("claude-code")
    assert isinstance(claude_provider, ClaudeCodeSubagentProvider)
    assert claude_provider.max_turns == 10


def test_unconfigured_default_backend_fails_closed_with_clear_log(caplog):
    import app.deepspace.services.subagent_providers as providers_module

    providers_module._PROVIDERS.clear()

    class FakeSettings:
        deepspace_subagent_providers = {}
        deepspace_subagent_default_backend = "missing-backend"

    initialize_providers_from_settings(FakeSettings())

    assert get_provider("missing-backend") is None
    assert "will fail closed" in caplog.text


@pytest.mark.asyncio
async def test_acp_provider_basic():
    provider = AcpSubagentProvider(
        command="echo",
        args=["test"],
    )
    # Just test instantiation
    assert provider.name == "acp"
    assert provider.args == ["test"]


def test_acp_provider_defaults_to_dsh_acp_profile():
    provider = AcpSubagentProvider()
    assert provider.command == "dsh"
    assert provider.args == ["--profile", "acp"]


def test_subagent_provider_defaults_are_fresh_values() -> None:
    """Explicit empty arguments and environment maps are fresh values."""
    first = AcpSubagentProvider(args=[])
    second = AcpSubagentProvider(args=[])

    assert first.args == []
    assert first.base_env == {}
    first.args.append("--example")
    first.base_env["EXAMPLE"] = "value"
    assert second.args == []
    assert second.base_env == {}

    claude_first = ClaudeCodeSubagentProvider(args=[])
    claude_second = ClaudeCodeSubagentProvider(args=[])
    assert claude_first.base_args == []
    claude_first.base_args.append("--example")
    assert claude_second.base_args == []

    codex_first = CodexSubagentProvider(args=[])
    codex_second = CodexSubagentProvider(args=[])
    codex_first.base_args.append("--example")
    assert codex_second.base_args == []

    from app.deepspace.services.subagent_provider_dsh_sdk import DshSdkProvider

    sdk_first = DshSdkProvider()
    sdk_second = DshSdkProvider()
    sdk_first.args.append("--example")
    sdk_first.patches.append("patch.json")
    assert sdk_second.args == []
    assert sdk_second.patches == []


@pytest.mark.asyncio
async def test_claude_code_provider_basic():
    provider = ClaudeCodeSubagentProvider(
        command="echo",
        args=["test"],
    )
    assert provider.name == "claude-code"


@pytest.mark.asyncio
async def test_codex_provider_basic():
    provider = CodexSubagentProvider(command="echo", args=["exec", "--json"])
    assert provider.name == "codex"


@pytest.mark.asyncio
async def test_codex_provider_uses_supported_jsonl_cli_and_extracts_final_answer(monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "cmd-1", "type": "command_execution", "command": "pwd"},
        },
        {
            "type": "item.completed",
            "item": {"id": "msg-1", "type": "agent_message", "text": "Found it."},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 12}},
    ]
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            captured["input"] = input
            return "\n".join(json.dumps(event) for event in events).encode(), b""

    async def fake_spawn(argv, **kwargs):
        captured["command"] = argv[0]
        captured["args"] = list(argv[1:])
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return _FakeSubprocessHandle(FakeProcess())

    monkeypatch.setattr(
        "app.deepspace.services.subagent_providers.spawn_with_stdio",
        fake_spawn,
    )
    provider = CodexSubagentProvider()
    result = await provider.run(
        SubagentRunSpec(
            task="Inspect the project",
            persona="Be concise",
            model="gpt-test",
            cwd="/tmp/project",
        ),
        lambda: False,
    )

    assert result.status == "completed"
    assert result.answer == "Found it."
    assert result.tool_calls[0]["type"] == "command_execution"
    assert captured["command"] == "codex"
    assert captured["args"] == [
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-test",
        "-",
    ]
    assert (
        captured["input"]
        == b"Child instructions (persona):\nBe concise\n\nTask:\nInspect the project"
    )
    assert captured["cwd"] == "/tmp/project"
    assert captured["env"] == {}


@pytest.mark.asyncio
async def test_codex_provider_fails_when_jsonl_has_no_completed_turn(monkeypatch):
    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return (
                b'{"type":"item.completed","item":{"type":"agent_message","text":"Partial"}}\n',
                b"",
            )

    async def fake_spawn(_argv, **_kwargs):
        return _FakeSubprocessHandle(FakeProcess())

    monkeypatch.setattr(
        "app.deepspace.services.subagent_providers.spawn_with_stdio",
        fake_spawn,
    )
    result = await CodexSubagentProvider().run(SubagentRunSpec(task="task"), lambda: False)

    assert result.status == "failed"
    assert result.answer == "Partial"
    assert "turn.completed" in str(result.error)


@pytest.mark.asyncio
async def test_codex_provider_reaps_cancelled_process_and_keeps_partial_text(monkeypatch):
    import asyncio

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.stopped = asyncio.Event()

        async def communicate(self, input=None):
            await self.stopped.wait()
            return (
                b'{"type":"item.completed","item":{"type":"agent_message","text":"Partial answer"}}\n',
                b"",
            )

        def terminate(self):
            self.returncode = -15
            self.stopped.set()

        def kill(self):
            self.returncode = -9
            self.stopped.set()

    process = FakeProcess()

    async def fake_spawn(_argv, **_kwargs):
        return _FakeSubprocessHandle(process)

    monkeypatch.setattr(
        "app.deepspace.services.subagent_providers.spawn_with_stdio",
        fake_spawn,
    )
    result = await CodexSubagentProvider().run(
        SubagentRunSpec(task="Long task"),
        lambda: True,
    )

    assert result.status == "cancelled"
    assert result.answer == "Partial answer"
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_claude_code_provider_passes_model_and_persona_to_print_mode(monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b"Claude answer", b""

    async def fake_spawn(argv, **kwargs):
        captured["command"] = argv[0]
        captured["args"] = list(argv[1:])
        return _FakeSubprocessHandle(FakeProcess())

    monkeypatch.setattr(
        "app.deepspace.services.subagent_providers.spawn_with_stdio",
        fake_spawn,
    )
    result = await ClaudeCodeSubagentProvider().run(
        SubagentRunSpec(task="Do the task", persona="Be careful", model="claude-test"),
        lambda: False,
    )

    assert result.status == "completed"
    assert result.answer == "Claude answer"
    assert captured["command"] == "claude"
    assert captured["args"] == [
        "-p",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        "10",
        "--model",
        "claude-test",
        "Child instructions (persona):\nBe careful\n\nTask:\nDo the task",
    ]


@pytest.mark.asyncio
async def test_compatibility_provider_modules_delegate_to_registered_adapters(monkeypatch):
    from app.deepspace.services import subagent_providers
    from app.deepspace.services.subagent_provider_claude_code import (
        ClaudeCodeProvider as ClaudeCodeCompatibilityProvider,
    )
    from app.deepspace.services.subagent_provider_claude_code import ClaudeCodeRunSpec
    from app.deepspace.services.subagent_provider_codex import (
        CodexProvider as CodexCompatibilityProvider,
    )
    from app.deepspace.services.subagent_provider_codex import CodexRunSpec

    captured = []

    class FakeAdapter:
        def __init__(self, **config):
            captured.append(("config", config))

        async def run(self, spec, cancel_check):
            captured.append(("run", spec, cancel_check))
            return SubagentResult(status="completed", answer="adapter result")

    monkeypatch.setattr(subagent_providers, "ClaudeCodeSubagentProvider", FakeAdapter)
    claude = await ClaudeCodeCompatibilityProvider().run(
        ClaudeCodeRunSpec(
            task="claude task",
            model="claude-model",
            permission_mode="plan",
            max_turns=4,
        ),
        lambda: False,
    )
    assert claude.answer == "adapter result"
    assert captured[-2][1]["max_turns"] == 4
    assert captured[-1][1].task == "claude task"
    assert captured[-1][1].model == "claude-model"

    captured.clear()
    monkeypatch.setattr(subagent_providers, "CodexSubagentProvider", FakeAdapter)
    codex = await CodexCompatibilityProvider().run(
        CodexRunSpec(
            task="codex task",
            model="codex-model",
            sandbox_mode="workspace-write",
        ),
        lambda: False,
    )
    assert codex.answer == "adapter result"
    assert captured[-2][1]["sandbox_mode"] == "workspace-write"
    assert captured[-1][1].task == "codex task"
    assert captured[-1][1].model == "codex-model"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
