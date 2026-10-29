"""Tests for subagent providers."""

from __future__ import annotations

import pytest

from app.deepspace.services.subagent_providers import (
    AcpSubagentProvider,
    ClaudeCodeSubagentProvider,
    CodexSubagentProvider,
    NeosisSdkSubagentProvider,
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


def test_initialize_providers_from_settings():
    # Clear any existing providers
    import app.deepspace.services.subagent_providers as providers_module

    providers_module._PROVIDERS.clear()

    class FakeSettings:
        deepspace_subagent_providers = {
            "acp": {
                "command": "neosis-acp-child",
                "args": [],
                "env": {},
            },
            "claude-code": {
                "command": "claude",
                "args": ["--dangerously-skip-permissions", "-p"],
                "permission_mode": "dontAsk",
            },
            "codex": {
                "command": "codex",
                "args": ["exec", "--stdio"],
                "permission_mode": "auto",
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
        or "neosis-sdk" in list_providers()
    )


@pytest.mark.asyncio
async def test_acp_provider_basic():
    provider = AcpSubagentProvider(
        command="echo",
        args=["test"],
    )
    # Just test instantiation
    assert provider.name == "acp"


@pytest.mark.asyncio
async def test_claude_code_provider_basic():
    provider = ClaudeCodeSubagentProvider(
        command="echo",
        args=["test"],
    )
    assert provider.name == "claude-code"


@pytest.mark.asyncio
async def test_codex_provider_basic():
    provider = CodexSubagentProvider(
        command="echo",
        args=["test"],
    )
    assert provider.name == "codex"


@pytest.mark.asyncio
async def test_neosis_sdk_provider_basic():
    provider = NeosisSdkSubagentProvider(
        command="echo",
        args=["test"],
    )
    assert provider.name == "neosis-sdk"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
