from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.deepspace.api import chats as chats_api_module
from app.deepspace.services import chat_service as chat_service_module
from app.deepspace.services.chat_service import (
    NATIVE_TOOL_REGISTRY,
    DeepSpaceChatService,
    _TimedProviderItem,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.runtime_store import DeepSpaceRuntimeStore
from app.deepspace.services.url_reader import validate_public_url
from app.providers.services.base import ProviderRequestError


@pytest.mark.asyncio
async def test_provider_stream_default_deadline_allows_full_provider_window() -> None:
    """DeepSpace must not truncate a model stream at the former 90s default."""
    service = object.__new__(DeepSpaceChatService)
    captured: dict[str, float] = {}

    async def stream_factory():
        yield {"type": "delta", "text": "ok"}

    async def capture_stream(_iterable, *, run_id, deadline, cancel_key=None):
        del run_id, cancel_key
        captured["remaining"] = deadline - time.monotonic()
        async for item in _iterable:
            yield item

    service._cancellable_provider_stream = capture_stream
    frames = [
        item
        async for item in service._provider_stream_with_retry(
            stream_factory,
            run_id=None,
            deadline=time.monotonic() + 600,
        )
    ]

    assert frames == [{"type": "delta", "text": "ok"}]
    assert 295 <= captured["remaining"] <= 300


@pytest.mark.asyncio
@pytest.mark.unit_no_db
async def test_provider_stream_stops_on_redis_cancel_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis marker stops a live provider even if database state lags or fails."""
    service = object.__new__(DeepSpaceChatService)
    service.settings = SimpleNamespace(redis_url="redis://cancel-test")
    client = _FakeCancellationRedis()
    monkeypatch.setattr(chat_service_module.aioredis, "from_url", lambda *_args, **_kwargs: client)
    provider_cancelled = asyncio.Event()

    async def never_finishes():
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            provider_cancelled.set()

    events = [
        event
        async for event in service._cancellable_provider_stream(
            never_finishes(),
            run_id=uuid.uuid4(),
            cancel_key="deepspace:cancel:test",
            deadline=time.monotonic() + 5,
        )
    ]

    assert events == [{"type": "runtime_cancelled"}]
    assert client.checked_keys == ["deepspace:cancel:test"]
    assert client.closed
    assert provider_cancelled.is_set()


class _FakeCancellationRedis:
    def __init__(self) -> None:
        self.checked_keys: list[str] = []
        self.closed = False

    async def exists(self, key: str) -> int:
        self.checked_keys.append(key)
        return 1

    async def aclose(self) -> None:
        self.closed = True


class _FakeQueryResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None


class _FakeCancellationSession:
    def __init__(self, *responses: list[object]) -> None:
        self.responses = list(responses)
        self.committed = False

    def execute(self, _statement):
        return _FakeQueryResult(self.responses.pop(0))

    def commit(self) -> None:
        self.committed = True


@pytest.mark.unit_no_db
def test_cancel_stops_a_paused_question_and_clears_its_pending_state() -> None:
    now = datetime.now(UTC)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
        status="awaiting_user",
        cancel_requested=False,
        heartbeat_at=now,
        updated_at=now,
        last_error=None,
    )
    message = SimpleNamespace(
        content="Which folder should I search?",
        metadata_json={
            "status": "awaiting_user",
            "pending_user_question": {"question_id": "question-1"},
            "agent_steps": [
                {"type": "ask_user_question", "status": "awaiting_approval"},
            ],
        },
    )
    session = _FakeCancellationSession([run], [message])

    cancelled = DeepSpaceRuntimeStore(session).request_cancel(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        conversation_id=run.conversation_id,
    )

    assert cancelled is True
    assert session.committed is True
    assert run.status == "cancelled"
    assert run.cancel_requested is True
    assert run.heartbeat_at is None
    assert message.metadata_json["status"] == "cancelled"
    assert "pending_user_question" not in message.metadata_json
    assert message.metadata_json["agent_steps"][0]["status"] == "completed"
    assert message.content.endswith("This task was cancelled before it continued.")


@pytest.mark.unit_no_db
def test_cancel_keeps_a_live_run_cooperative_until_the_worker_stops() -> None:
    now = datetime.now(UTC)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        assistant_message_id=None,
        status="running",
        cancel_requested=False,
        heartbeat_at=now,
        updated_at=now,
        last_error=None,
    )
    session = _FakeCancellationSession([run])

    cancelled = DeepSpaceRuntimeStore(session).request_cancel(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        conversation_id=run.conversation_id,
    )

    assert cancelled is True
    assert run.status == "cancelling"
    assert run.cancel_requested is True
    assert run.heartbeat_at == now


@pytest.mark.asyncio
@pytest.mark.unit_no_db
async def test_cancellation_marker_failure_does_not_reject_a_durable_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis is a fast signal, never the source of truth for a Stop request."""

    class BrokenCancellationRedis:
        closed = False

        async def set(self, *_args, **_kwargs) -> None:
            raise OSError("Redis is unavailable")

        async def aclose(self) -> None:
            self.closed = True

    client = BrokenCancellationRedis()
    monkeypatch.setattr(chats_api_module.aioredis, "from_url", lambda *_args, **_kwargs: client)

    await chats_api_module._record_cancellation_marker(
        settings=SimpleNamespace(redis_url="redis://cancel-test"),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        client_request_id="cancel-request",
    )

    assert client.closed


def test_deepspace_policy_blocks_ide_and_mcp_tools() -> None:
    policy = DeepSpaceToolPolicy()

    assert policy.decide("url_read", {}).allowed
    assert policy.decide("read", {"target": "library"}).allowed
    assert policy.decide("analyze", {"operation": "compare_documents"}).allowed
    assert policy.decide("todo_write", {"operation": "read"}).mode == "read"
    assert policy.decide("todo_write", {"operation": "check"}).mode == "read"
    assert policy.decide("todo_write", {"operation": "update"}).mode == "write"
    assert not policy.decide("document_read", {}).allowed
    assert policy.decide("sandbox_execute", {}).allowed
    assert policy.decide("artifact_create", {}).allowed
    assert policy.mode("write") == "write"
    assert policy.mode("workspace_write") is None
    assert policy.mode("memory_search") is None
    assert policy.mode("memory_read") is None
    assert policy.mode("memory_write") is None
    assert policy.mode("memory_forget") is None
    assert not policy.decide("workspace_write", {}).allowed
    assert not policy.decide("memory_search", {}).allowed
    assert not policy.decide("terminal", {}).allowed
    assert not policy.decide("mcp_call", {}).allowed


def test_deepspace_exposes_universal_workspace_tools() -> None:
    names = set(NATIVE_TOOL_REGISTRY.names())

    assert {"read", "find", "write", "edit", "delete"}.issubset(names)
    assert "save" not in names
    assert "workspace_write" not in names
    assert "memory_search" not in names


def test_url_reader_rejects_private_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.deepspace.services.url_reader.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 80))],
    )

    with pytest.raises(ProviderRequestError, match="Private"):
        validate_public_url("https://example.test/document")


@pytest.mark.asyncio
async def test_read_only_tools_run_concurrently() -> None:
    service = object.__new__(DeepSpaceChatService)
    service.tool_policy = DeepSpaceToolPolicy()
    service.settings = SimpleNamespace(deepspace_max_concurrent_tools=8)
    started: list[str] = []
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    conversation_id = uuid.uuid4()

    async def fake_execute(**kwargs):
        started.append(kwargs["tool_name"])
        await asyncio.sleep(0.05)
        return {"tool": kwargs["tool_name"]}

    service._execute_productivity_tool = fake_execute
    started_at = time.monotonic()
    results = await asyncio.gather(
        service._run_tool_call(
            tool_name="url_read",
            arguments={"url": "https://example.com"},
            auth=auth,
            conversation_id=conversation_id,
            web_provider=None,
            web_candidate=None,
            request=None,
            loop_deadline=time.monotonic() + 5,
            run_id=None,
            read_semaphore=asyncio.Semaphore(8),
            write_lock=asyncio.Lock(),
        ),
        service._run_tool_call(
            tool_name="image_read",
            arguments={"url": "https://example.com/image.png"},
            auth=auth,
            conversation_id=conversation_id,
            web_provider=None,
            web_candidate=None,
            request=None,
            loop_deadline=time.monotonic() + 5,
            run_id=None,
            read_semaphore=asyncio.Semaphore(8),
            write_lock=asyncio.Lock(),
        ),
    )

    assert time.monotonic() - started_at < 0.09
    assert started == ["url_read", "image_read"]
    assert all(result["success"] for result in results)


def test_runtime_store_bounds_retained_step_payload() -> None:
    result = DeepSpaceRuntimeStore._bounded_json({"content": "x" * 30_000})

    assert result["truncated"] is True
    assert len(str(result["preview"])) <= 19_500


def test_history_steps_include_multiple_runtime_attempts_for_one_assistant_message() -> None:
    first_run_id = uuid.uuid4()
    retry_run_id = uuid.uuid4()
    created_at = datetime(2026, 10, 10, tzinfo=UTC)
    steps = [
        SimpleNamespace(
            run_id=first_run_id,
            sequence=1,
            step_type="model_message",
            status="completed",
            result_json={"text": "First attempt"},
            input_json={},
            created_at=created_at,
            completed_at=created_at,
            tool_name=None,
            tool_call_id=None,
        ),
        SimpleNamespace(
            run_id=retry_run_id,
            sequence=1,
            step_type="model_message",
            status="completed",
            result_json={"text": "Retry attempt"},
            input_json={},
            created_at=created_at,
            completed_at=created_at,
            tool_name=None,
            tool_call_id=None,
        ),
    ]

    class FakeResult:
        def scalars(self) -> FakeResult:
            return self

        def all(self) -> list[SimpleNamespace]:
            return steps

    class FakeSession:
        def execute(self, _statement: object) -> FakeResult:
            return FakeResult()

    result = DeepSpaceRuntimeStore(FakeSession()).history_steps_for_message(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
    )

    assert [item["output"] for item in result] == ["First attempt", "Retry attempt"]
    assert result[0]["step_id"] == f"runtime_{first_run_id}_1"
    assert result[1]["step_id"] == f"runtime_{retry_run_id}_1"


@pytest.mark.asyncio
async def test_provider_retry_retries_pre_output_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(DeepSpaceChatService)
    attempts = 0

    async def stream_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderRequestError("test", 503, "temporary")
        yield {"type": "delta", "text": "ok"}

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.deepspace.services.chat_service.asyncio.sleep", no_sleep)
    stream_started = time.monotonic()
    frames = [
        item
        async for item in service._provider_stream_with_retry(
            stream_factory,
            run_id=None,
            deadline=time.monotonic() + 5,
            provider_type="lmstudio",
        )
    ]
    stream_finished = time.monotonic()

    assert attempts == 2
    assert len(frames) == 1
    assert isinstance(frames[0], _TimedProviderItem)
    assert frames[0].value == {"type": "delta", "text": "ok"}
    assert stream_started <= frames[0].received_monotonic <= stream_finished


@pytest.mark.asyncio
async def test_provider_retry_does_not_duplicate_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(DeepSpaceChatService)

    async def stream_factory():
        yield {"type": "delta", "text": "partial"}
        raise ProviderRequestError("test", 502, "connection lost")

    monkeypatch.setattr("app.deepspace.services.chat_service.asyncio.sleep", lambda _: None)
    with pytest.raises(ProviderRequestError):
        [
            item
            async for item in service._provider_stream_with_retry(
                stream_factory, run_id=None, deadline=time.monotonic() + 5
            )
        ]


@pytest.mark.asyncio
async def test_provider_retry_does_not_retry_non_retryable_provider_errors() -> None:
    service = object.__new__(DeepSpaceChatService)
    attempts = 0

    async def stream_factory():
        nonlocal attempts
        attempts += 1
        raise ProviderRequestError("test", 403, "account policy")
        yield  # pragma: no cover

    with pytest.raises(ProviderRequestError):
        [
            item
            async for item in service._provider_stream_with_retry(
                stream_factory, run_id=None, deadline=time.monotonic() + 5
            )
        ]

    assert attempts == 1


def test_context_budget_reports_thresholds_and_safe_remaining() -> None:
    state = DeepSpaceChatService._context_budget_state(
        used_tokens=195,
        context_limit=200,
        reserved_output_tokens=20,
        compacted=False,
    )

    assert state["contextStatus"] == "emergency"
    assert state["safeRemainingTokens"] == 0

    unknown = DeepSpaceChatService._context_budget_state(
        used_tokens=10,
        context_limit=None,
        reserved_output_tokens=20,
        compacted=False,
    )
    assert unknown["contextStatus"] == "unknown"
    assert unknown["safeRemainingTokens"] is None


def test_context_history_fit_preserves_system_and_newest_messages() -> None:
    messages = [{"role": "system", "content": "rules"}]
    messages.extend({"role": "user", "content": "x" * 300} for _ in range(12))

    fitted, compacted = DeepSpaceChatService._fit_history_to_context(
        messages, context_window=700, max_output_tokens=256
    )

    assert compacted is True
    assert fitted[0]["role"] == "system"
    assert fitted[-1] == messages[-1]
