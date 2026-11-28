"""Tests for OpenCode-style unbounded turns, model-driven stopping, and MAX_STEPS_PROMPT."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.deepspace.services.chat_service.core import DeepSpaceChatService
from app.deepspace.services.chat_service.tools import _ToolsMixin
from app.deepspace.services.chat_service.turns.tool_calls import (
    MAX_STEPS_PROMPT,
    _TurnToolCallsMixin,
)
from app.deepspace.services.chat_service.utils import _TurnState
from app.deepspace.services.repeat_tool_reminder import RepeatToolReminderConfig

pytestmark = pytest.mark.unit_no_db


def test_default_settings_are_unbounded_and_model_driven() -> None:
    settings = Settings()
    assert settings.deepspace_loop_guard_enabled is False
    assert settings.deepspace_repeat_tool_reminder_enabled is False
    assert settings.deepspace_agent_max_runtime_seconds == 0
    assert settings.deepspace_agent_max_steps is None

    reminder_cfg = RepeatToolReminderConfig.from_settings(settings)
    assert reminder_cfg.enabled is False


def test_tool_call_budget_handles_infinite_deadline() -> None:
    class _Tools(_ToolsMixin):
        settings = Settings(deepspace_tool_timeout_seconds=45.0)

    budget = _Tools()._tool_call_budget(float("inf"))
    assert budget == 45.0


def test_loop_guard_refusal_is_disabled_by_default() -> None:
    class _Tools(_ToolsMixin):
        settings = Settings()

    tools = _Tools()
    conv_id = uuid.uuid4()
    for _ in range(100):
        assert (
            tools._loop_guard_refusal(
                tool_name="read",
                arguments={"path": "app/main.py"},
                conversation_id=conv_id,
            )
            is None
        )


def test_request_model_final_answer_without_tools_uses_max_steps_prompt() -> None:
    class _Dummy(_TurnToolCallsMixin):
        @staticmethod
        def _set_dynamic_system_instruction(messages, *, key, content):
            messages.append({"role": "system", "__key": key, "content": content})

    state = _TurnState(
        auth=SimpleNamespace(),
        conversation_id=uuid.uuid4(),
        prompt="test",
        assistant_message=SimpleNamespace(id=uuid.uuid4()),
    )
    state.conversation_messages = []

    _Dummy()._request_model_final_answer_without_tools(
        state,
        reason="Configured step limit (10) reached.",
        mark_blocked=False,
    )

    assert state.disable_tools_for_final_answer is True
    assert state.terminal_status != "blocked"
    assert any(MAX_STEPS_PROMPT in str(m.get("content") or "") for m in state.conversation_messages)


def test_chat_service_exposes_max_steps_prompt() -> None:
    assert "CRITICAL - MAXIMUM STEPS REACHED" in MAX_STEPS_PROMPT
    assert hasattr(DeepSpaceChatService, "_request_model_final_answer_without_tools")
