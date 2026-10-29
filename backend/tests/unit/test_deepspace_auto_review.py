"""Behavior tests for model-backed auto-review of tool approvals."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.permission_presets import (
    AutoReviewEngine,
    PermissionPresetService,
)
from app.providers.services.types import ChatGenerateRequest, ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


def _settings(**overrides):
    """Return deployment settings with auto-review values replaced."""
    return get_settings().model_copy(update=overrides)


async def test_safe_tool_is_approved_without_a_model_call() -> None:
    """Read-only tools do not spend a model request."""
    calls: list[str] = []

    async def reviewer(system: str, user: str) -> str:
        calls.append(user)
        return "approve looked safe"

    engine = AutoReviewEngine(_settings(deepspace_auto_review_enabled=True), reviewer)

    decision = await engine.review("fs_read", {"path": "README.md"}, {})

    assert decision.approved is True
    assert decision.reviewed_by == "policy"
    assert calls == []


async def test_dangerous_tool_uses_the_model_verdict() -> None:
    """A write tool is approved only when the model says approve."""
    seen: list[tuple[str, str]] = []

    async def reviewer(system: str, user: str) -> str:
        seen.append((system, user))
        return "approve the path stays inside the workspace"

    engine = AutoReviewEngine(_settings(deepspace_auto_review_enabled=True), reviewer)

    decision = await engine.review("fs_write", {"path": "notes.md"}, {"round": 2})

    assert decision.approved is True
    assert decision.reviewed_by == "model"
    assert "notes.md" in seen[0][1]


async def test_deny_and_unclear_answers_deny() -> None:
    """Only an explicit approve opens the approval gate."""
    engine = AutoReviewEngine(
        _settings(deepspace_auto_review_enabled=True),
        lambda system, user: _answer("deny writes to system paths"),
    )
    assert (await engine.review("fs_write", {}, {})).approved is False

    unclear = AutoReviewEngine(
        _settings(deepspace_auto_review_enabled=True),
        lambda system, user: _answer("maybe, depends on the user"),
    )
    decision = await unclear.review("fs_write", {}, {})
    assert decision.approved is False
    assert "did not answer approve or deny" in decision.reason


async def _answer(text: str) -> str:
    """Return a fixed reviewer answer."""
    return text


async def test_reviewer_failure_and_timeout_deny(monkeypatch) -> None:
    """An unavailable reviewer never becomes an approval."""

    async def explode(system: str, user: str) -> str:
        raise RuntimeError("provider unavailable")

    engine = AutoReviewEngine(_settings(deepspace_auto_review_enabled=True), explode)
    failed = await engine.review("fs_write", {}, {})
    assert failed.approved is False
    assert failed.reviewed_by == "unavailable"

    async def slow(system: str, user: str) -> str:
        await asyncio.sleep(1)
        return "approve late"

    timed = AutoReviewEngine(
        _settings(
            deepspace_auto_review_enabled=True,
            deepspace_auto_review_timeout_ms=1,
        ),
        slow,
    )
    decision = await timed.review("fs_write", {}, {})
    assert decision.approved is False
    assert "did not answer" in decision.reason


async def test_disabled_review_denies_and_service_wires_the_engine() -> None:
    """Disabled review leaves the ordinary human approval path in charge."""
    service = PermissionPresetService(_settings(deepspace_auto_review_enabled=False))

    decision = await service.auto_review.review("fs_write", {}, {})

    assert decision.approved is False
    assert decision.reviewed_by == "unavailable"
    assert service.list_presets()


async def test_turn_reviewer_calls_the_selected_provider() -> None:
    """The chat service sends one non-streaming request to the turn's model."""
    requests: list[ChatGenerateRequest] = []

    class FakeProvider:
        def generate(self, request: ChatGenerateRequest) -> ChatGenerateResponse:
            requests.append(request)
            return ChatGenerateResponse(content="approve the command is read-only")

    class StubService:
        """Minimal service that borrows the real review method."""

        settings = _settings(deepspace_auto_review_enabled=True)
        _auto_review_tool_call = DeepSpaceChatService._auto_review_tool_call

    service = StubService()
    state = SimpleNamespace(
        candidate=SimpleNamespace(
            model_name="test-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="test-key",
        ),
        provider=FakeProvider(),
        conversation_id=uuid.uuid4(),
        round_index=3,
        risk_level="high",
    )

    decision = await service._auto_review_tool_call(state, "terminal", {"cmd": "ls"})

    assert decision.approved is True
    assert len(requests) == 1
    request = requests[0]
    assert request.model == "test-model"
    assert request.stream is False
    assert request.api_key == "test-key"
    assert request.messages[0]["role"] == "system"
    assert "ls" in request.messages[1]["content"]
