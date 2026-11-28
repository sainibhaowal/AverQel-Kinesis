"""Tests for the DeepSpace structured compaction pipeline and background precomputer."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.deepspace.services.compaction_pipeline import (
    COMPACTION_END,
    COMPACTION_START,
    COMPACTION_SUMMARY,
    SUMMARY_TEMPLATE,
    CompactionPrecomputer,
    PrecomputedSummary,
    build_summary_prompt,
    compact_request_full,
    summarize_history,
)
from app.providers.services.types import ChatGenerateRequest, ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


class _FakeProvider:
    """Sync ChatProvider double recording ChatGenerateRequest calls."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ChatGenerateRequest] = []

    def generate(self, request: ChatGenerateRequest) -> ChatGenerateResponse:
        self.requests.append(request)
        return ChatGenerateResponse(content=self.text)


class _FailingProvider:
    def generate(self, request: ChatGenerateRequest) -> ChatGenerateResponse:
        raise RuntimeError("provider unavailable")


class _FakeEventStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _long_history(turns: int = 8) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "System policy"}]
    for idx in range(turns):
        messages.append({"role": "user", "content": f"User request {idx} about app/main.py"})
        messages.append(
            {"role": "assistant", "content": f"Assistant reply {idx} inspecting app/main.py"}
        )
    return messages


def test_summary_template_contains_opencode_sections() -> None:
    for heading in (
        "## Objective",
        "## Requirements",
        "## Decisions",
        "## Work State",
        "### Completed",
        "### Active",
        "### Blocked",
        "## Next Move",
        "## Relevant Files",
        "## Important Context",
    ):
        assert heading in SUMMARY_TEMPLATE


def test_build_summary_prompt_variants() -> None:
    initial = build_summary_prompt(update=False)
    assert "summarize the conversation above" in initial
    assert "Reconcile Work State" not in initial

    incremental = build_summary_prompt(update=True)
    assert "Update the existing checkpoint" in incremental
    assert "Reconcile Work State" in incremental

    legacy = build_summary_prompt(update=True, legacy=True)
    assert "earlier format" in legacy


def test_summarize_history_uses_chat_generate_request_and_supports_incremental() -> None:
    structured = (
        "## Objective\n- Build feature X\n\n"
        "## Work State\n### Completed\n- Step 1\n### Active\n- Step 2\n### Blocked\n- (none)"
    )
    provider = _FakeProvider(structured)
    settings = Settings(deepspace_compaction_llm_enabled=True, llm_model="test-model")
    history = _long_history(8)

    compacted, meta = asyncio.run(
        summarize_history(
            history,
            settings=settings,
            provider=provider,
            existing_summary="## Objective\n- Build feature X",
            model_name="deepseek-chat",
        )
    )

    assert len(provider.requests) == 1
    assert isinstance(provider.requests[0], ChatGenerateRequest)
    assert provider.requests[0].model == "deepseek-chat"
    assert meta["incremental_update"] is True
    assert meta["summary"] == structured
    assert any(
        m.get("role") == "system" and structured in str(m.get("content") or "") for m in compacted
    )


def test_summarize_history_respects_disabled_and_cancel_signal() -> None:
    provider = _FakeProvider("## Objective\n- Never called")
    disabled_settings = Settings(deepspace_compaction_llm_enabled=False)
    history = _long_history(8)

    out, meta = asyncio.run(
        summarize_history(history, settings=disabled_settings, provider=provider)
    )
    assert out == history
    assert meta == {}
    assert provider.requests == []

    signal = asyncio.Event()
    signal.set()
    enabled_settings = Settings(deepspace_compaction_llm_enabled=True)
    out2, meta2 = asyncio.run(
        summarize_history(history, settings=enabled_settings, provider=provider, signal=signal)
    )
    assert out2 == history
    assert meta2 == {}


def test_compaction_precomputer_schedules_holds_and_consumes() -> None:
    async def _run() -> None:
        precomputer = CompactionPrecomputer()
        conv_id = uuid.uuid4()
        provider = _FakeProvider("## Objective\n- Precomputed checkpoint")
        settings = Settings(
            deepspace_compaction_llm_enabled=True,
            deepspace_compaction_background_precompute_enabled=True,
            deepspace_compaction_precompute_lead_tokens=20_000,
        )
        history = _long_history(8)

        # Below threshold: should not schedule
        assert (
            precomputer.maybe_schedule(
                conversation_id=conv_id,
                history=history,
                used_tokens=50_000,
                context_limit=100_000,
                settings=settings,
                provider=provider,
            )
            is False
        )

        # Within lead window: should schedule
        assert (
            precomputer.maybe_schedule(
                conversation_id=conv_id,
                history=history,
                used_tokens=85_000,
                context_limit=100_000,
                settings=settings,
                provider=provider,
            )
            is True
        )
        await asyncio.sleep(0.05)

        held = precomputer.take(conv_id, min_source_messages=len(history))
        assert held is not None
        assert "Precomputed checkpoint" in held.summary
        assert precomputer.take(conv_id) is None

    asyncio.run(_run())


def test_compaction_precomputer_stops_after_max_failures() -> None:
    async def _run() -> None:
        precomputer = CompactionPrecomputer()
        conv_id = uuid.uuid4()
        settings = Settings(
            deepspace_compaction_llm_enabled=True,
            deepspace_compaction_background_precompute_enabled=True,
            deepspace_compaction_precompute_lead_tokens=20_000,
        )
        history = _long_history(8)
        failing = _FailingProvider()

        for _ in range(2):
            assert (
                precomputer.maybe_schedule(
                    conversation_id=conv_id,
                    history=history,
                    used_tokens=90_000,
                    context_limit=100_000,
                    settings=settings,
                    provider=failing,
                )
                is True
            )
            await asyncio.sleep(0.05)

        # Third attempt should be suppressed after PRECOMPUTE_MAX_FAILURES
        assert (
            precomputer.maybe_schedule(
                conversation_id=conv_id,
                history=history,
                used_tokens=90_000,
                context_limit=100_000,
                settings=settings,
                provider=failing,
            )
            is False
        )

    asyncio.run(_run())


def test_compact_request_full_uses_precomputed_and_emits_events() -> None:
    conv_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    event_store = _FakeEventStore()
    provider = _FakeProvider("## Objective\n- Should not be called")
    settings = Settings(deepspace_compaction_llm_enabled=True)
    precomputed = PrecomputedSummary(
        conversation_id=conv_id,
        source_message_count=17,
        summary="## Objective\n- Cached summary",
        metadata={"provider": "fake", "model": "test-model"},
    )

    outcome = asyncio.run(
        compact_request_full(
            _long_history(8),
            auth=auth,  # type: ignore[arg-type]
            conversation_id=conv_id,
            run_id=run_id,
            turn=3,
            settings=settings,
            event_store=event_store,  # type: ignore[arg-type]
            db=None,
            provider=provider,
            precomputed_summary=precomputed,
        )
    )

    assert provider.requests == []
    assert [e["event_type"] for e in event_store.events] == [
        COMPACTION_START,
        COMPACTION_SUMMARY,
        COMPACTION_END,
    ]
    assert any(
        "Cached summary" in str(m.get("content") or "")
        for m in outcome.messages
        if m.get("role") == "system"
    )
