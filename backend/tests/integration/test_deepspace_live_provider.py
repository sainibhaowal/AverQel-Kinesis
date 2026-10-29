"""The DeepSpace agent system proved against a real model provider.

The product's own tests replace the provider, so they prove the loop but not
the model.  These tests use the deployment's provider endpoint for the model's
answers, which is what makes them a live proof.

The provider key comes from the environment.  ``DEEPSEEK_API_KEY`` names the
key, ``DEEPSEEK_BASE_URL`` names the endpoint when the deployment points at a
different one, ``DEEPSEEK_MODEL`` names the first model, and
``DEEPSEEK_SECOND_MODEL`` names the second.  Without a key every test skips,
because a proof that cannot run proves nothing.

The tests need the database, because a conversation, a goal, and a child are
durable records, so they belong with the other database-backed tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.goal import DeepSpaceGoal
from app.deepspace.models.message import Message
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.subagent_runner import new_child_record, run_child_turn
from app.deepspace.services.tools._base import ToolContext
from app.providers.services.registry import ProviderRegistry
from app.providers.services.selection_service import (
    DEFAULT_PROVIDER_BASE_URLS,
    ProviderSelectionService,
)
from app.providers.services.types import (
    ChatGenerateRequest,
    ChatGenerateResponse,
    ProviderSelectionCandidate,
    ProviderSelectionResult,
)

#: The provider route the live proofs run under.
PROVIDER_TYPE = "deepseek"

#: The first live model, unless the deployment names another one.
DEFAULT_MODEL = "deepseek-v4.1-flash"

#: The second live model, unless the deployment names another one.
DEFAULT_SECOND_MODEL = "glm-5.3-flash"

#: The message a provider-only proof sends.
READY_MESSAGE = "Reply with the single word: ready."

#: The word the provider-only proof expects.
READY_WORD = "ready"

#: The marker the parent turn records through a real tool call.
LIVE_GOAL = "live-proof"

#: The content the parent turn stores in that goal.
LIVE_GOAL_CONTENT = "live proof ok"

#: The parent turn's short prompt, which needs the goal tools.
PARENT_PROMPT = (
    f"Use goal_set to record the goal named '{LIVE_GOAL}' with content "
    f"'{LIVE_GOAL_CONTENT}', then use goal_read to read it back. "
    "Reply with the goal's content."
)

#: The wall-clock budget one live proof may use.
PROOF_TIMEOUT_SECONDS = 60.0


def _api_key() -> str:
    """Return the provider key the deployment holds.

    Returns:
        The key, or an empty string when the deployment holds none.
    """
    return str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def _base_url() -> str:
    """Return the endpoint the deployment points the provider at.

    Returns:
        The endpoint, or the product's own default for the provider.
    """
    configured = str(os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    return configured or DEFAULT_PROVIDER_BASE_URLS[PROVIDER_TYPE]


def _model() -> str:
    """Return the first live model.

    Returns:
        The model name.
    """
    return str(os.environ.get("DEEPSEEK_MODEL") or "").strip() or DEFAULT_MODEL


def _second_model() -> str:
    """Return the second live model.

    Returns:
        The model name.
    """
    return str(os.environ.get("DEEPSEEK_SECOND_MODEL") or "").strip() or DEFAULT_SECOND_MODEL


requires_provider_key = pytest.mark.skipif(
    not _api_key(),
    reason=(
        "DEEPSEEK_API_KEY is not set; the live provider proof needs a provider key. "
        "Export DEEPSEEK_API_KEY (with DEEPSEEK_BASE_URL when the deployment uses "
        "another endpoint) and run the test again."
    ),
)


def _candidate(model: str) -> ProviderSelectionCandidate:
    """Build the provider route one live model runs under.

    Args:
        model: The model the route names.

    Returns:
        The selected provider candidate.
    """
    return ProviderSelectionCandidate(
        provider_type=PROVIDER_TYPE,
        model_name=model,
        feature_scope="chat",
        source="env_fallback",
        base_url=_base_url(),
        api_key=_api_key(),
        context_window=128_000,
        context_window_source="live_model",
    )


def _registry() -> ProviderRegistry:
    """Build the product's provider registry for the deployment settings.

    Returns:
        The registry.
    """
    return ProviderRegistry(get_settings())


def _generate(model: str, message: str) -> ChatGenerateResponse:
    """Ask one live model a short prompt through the product's provider.

    Args:
        model: The model to ask.
        message: The prompt to send.

    Returns:
        The provider's answer.
    """
    provider = _registry().get_chat_provider_from_selection(_candidate(model))
    return provider.generate(
        ChatGenerateRequest(
            model=model,
            messages=[{"role": "user", "content": message}],
            temperature=0.2,
            max_tokens=None,
            base_url=_base_url(),
            api_key=_api_key(),
            stream=False,
            metadata={"timeout_seconds": PROOF_TIMEOUT_SECONDS},
        )
    )


def _seed_conversation(
    db_session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> Conversation:
    """Create a DeepSpace conversation the product can stream a turn into.

    Args:
        db_session: The test's database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        title: The conversation title.

    Returns:
        The committed conversation.
    """
    conversation = Conversation(
        tenant_id=tenant_id,
        user_id=user_id,
        kind="deepspace",
        title=title,
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


def _auth(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AuthContext:
    """Build the request identity a turn runs under.

    Args:
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The request identity.
    """
    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        token_id="live-provider-test",
    )


class _LiveSelectionService(ProviderSelectionService):
    """The product's selection service with one live route in place."""

    def __init__(
        self, *, db: Session, settings: Any, candidate: ProviderSelectionCandidate
    ) -> None:
        """Create the service over the live candidate.

        Args:
            db: The test's database session.
            settings: The deployment settings.
            candidate: The live provider route.
        """
        super().__init__(db, settings)
        self._candidate = candidate

    def resolve_chat(self, **_kwargs: Any) -> ProviderSelectionResult:
        """Return the one live chat route.

        Args:
            _kwargs: The resolution arguments, which the live route ignores.

        Returns:
            The selection holding the live candidate.
        """
        return ProviderSelectionResult(feature_scope="chat", candidates=[self._candidate])

    def resolve_web_search(self, **_kwargs: Any) -> ProviderSelectionResult:
        """Return no web-search route, so the live proof stays offline.

        Args:
            _kwargs: The resolution arguments.

        Returns:
            An empty web-search selection.
        """
        return ProviderSelectionResult(feature_scope="web_search", candidates=[])


async def _collect_turn(
    service: DeepSpaceChatService,
    *,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    prompt: str,
) -> list[str]:
    """Run one parent turn and return its SSE frames.

    Args:
        service: The product's chat service with the live route in place.
        auth: The request identity.
        conversation_id: The conversation the turn belongs to.
        prompt: The prompt the turn answers.

    Returns:
        The SSE frames the turn emitted.
    """

    async def _collect() -> list[str]:
        return [
            frame
            async for frame in service.stream_turn(
                auth=auth,
                conversation_id=conversation_id,
                prompt=prompt,
                thinking_enabled=False,
            )
        ]

    return await asyncio.wait_for(_collect(), timeout=PROOF_TIMEOUT_SECONDS)


def _events(frames: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE frames into event names and their JSON payloads.

    Args:
        frames: The frames the turn emitted.

    Returns:
        The event name and payload of every frame that carries data.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        name, separator, body = frame.partition("\n")
        if not separator or not name.startswith("event: "):
            continue
        data = next(
            (line[len("data: ") :] for line in body.splitlines() if line.startswith("data: ")),
            None,
        )
        if data is None:
            continue
        events.append((name[len("event: ") :], json.loads(data)))
    return events


def _final_assistant_message(db_session: Session, conversation_id: uuid.UUID) -> Message | None:
    """Read the conversation's last assistant message.

    Args:
        db_session: The test's database session.
        conversation_id: The conversation to read.

    Returns:
        The assistant message row, or None when the turn wrote none.
    """
    db_session.expire_all()
    return (
        db_session.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .first()
    )


def _message_content(message: Message) -> str:
    """Read the active text of one message.

    Args:
        message: The message row.

    Returns:
        The message's active content.
    """
    if message.active_version is not None and message.active_version.content is not None:
        return str(message.active_version.content)
    return str(message.content or "")


def _streamed_answer(events: list[tuple[str, dict[str, Any]]]) -> str:
    """Join the assistant text the turn streamed to its client.

    Args:
        events: The parsed SSE events.

    Returns:
        The answer as the client received it.
    """
    answer = ""
    for name, payload in events:
        if name == "delta":
            answer += str(payload.get("text") or "")
        elif name == "replace":
            answer = str(payload.get("content") or "")
    return answer


def _stored_goal(db_session: Session, conversation_id: uuid.UUID, name: str) -> DeepSpaceGoal:
    """Read the goal a live tool call recorded.

    Args:
        db_session: The test's database session.
        conversation_id: The conversation that owns the goal.
        name: The goal's name.

    Returns:
        The goal row.
    """
    db_session.expire_all()
    return (
        db_session.query(DeepSpaceGoal)
        .filter(
            DeepSpaceGoal.conversation_id == conversation_id,
            DeepSpaceGoal.name == name,
        )
        .one()
    )


def _child(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    model: str,
    label: str,
) -> DeepSpaceSubagentRun:
    """Build one durable child record for a delegated turn.

    Args:
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation the child belongs to.
        model: The model the child runs under.
        label: The short description of the delegation.

    Returns:
        The unsaved child record.
    """
    return new_child_record(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        parent_run_id=None,
        parent_agent_id=None,
        origin="subagent",
        mode="one-shot",
        label=label,
        delegation_depth=1,
        agent_provider=PROVIDER_TYPE,
        agent_model=model,
        reasoning_effort=None,
        persona=None,
        tool_filter={"allow": [], "deny": []},
    )


def _child_context(
    db_session: Session, conversation_id: uuid.UUID, auth: AuthContext
) -> ToolContext:
    """Build the tool context a child's live turn runs with.

    Args:
        db_session: The test's database session.
        conversation_id: The conversation the child belongs to.
        auth: The request identity.

    Returns:
        The tool context with the product's registry and the live selection.
    """

    async def _executor(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    return ToolContext(
        auth=auth,
        conversation_id=conversation_id,
        settings=get_settings(),
        db=db_session,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=_registry(),
        provider_selection=_LiveSelectionService(
            db=db_session,
            settings=get_settings(),
            candidate=_candidate(_model()),
        ),
        native_executor=_executor,
    )


@requires_provider_key
def test_the_endpoint_lists_both_live_models() -> None:
    """The provider endpoint offers the two models the deployment names."""
    response = httpx.get(
        f"{_base_url().rstrip('/')}/models",
        headers={"Authorization": f"Bearer {_api_key()}"},
        timeout=PROOF_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200
    listed = {str(item.get("id") or "") for item in response.json().get("data") or []}
    for model in (_model(), _second_model()):
        assert model in listed


@requires_provider_key
def test_the_product_provider_answers_on_the_first_live_model() -> None:
    """The product's provider registry answers on the first live model."""
    answer = _generate(_model(), READY_MESSAGE)
    assert READY_WORD in answer.content.casefold()


@requires_provider_key
def test_the_product_provider_answers_on_the_second_live_model() -> None:
    """The product's provider registry answers on the second live model."""
    answer = _generate(_second_model(), READY_MESSAGE)
    assert READY_WORD in answer.content.casefold()


@requires_provider_key
def test_a_parent_turn_runs_a_real_tool_call_and_answers(
    db_session: Session,
    seed_user: Any,
) -> None:
    """A parent turn reaches the live model, runs a real tool, and answers."""
    owner = seed_user(
        "tenant-live-parent",
        "live-parent@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    conversation = _seed_conversation(
        db_session, owner.tenant_id, owner.user_id, "Live parent turn"
    )
    settings = get_settings()
    service = DeepSpaceChatService(db=db_session, settings=settings)
    service.providers = _LiveSelectionService(
        db=db_session,
        settings=settings,
        candidate=_candidate(_model()),
    )

    frames = asyncio.run(
        _collect_turn(
            service,
            auth=_auth(owner.tenant_id, owner.user_id),
            conversation_id=conversation.id,
            prompt=PARENT_PROMPT,
        )
    )

    events = _events(frames)
    tool_results = [
        payload
        for name, payload in events
        if name == "tool_result" and payload.get("success") is True
    ]
    assert any(payload.get("tool_name") == "goal_set" for payload in tool_results)
    assert any(name == "done" for name, _payload in events)
    assert LIVE_GOAL_CONTENT in _streamed_answer(events)

    message = _final_assistant_message(db_session, conversation.id)
    assert message is not None
    assert LIVE_GOAL_CONTENT in _message_content(message)
    goal = _stored_goal(db_session, conversation.id, LIVE_GOAL)
    assert goal.content == LIVE_GOAL_CONTENT


@requires_provider_key
def test_two_children_run_in_parallel_against_the_live_provider(
    db_session: Session,
    seed_user: Any,
) -> None:
    """Two children run at once and both answer through the live model."""
    owner = seed_user(
        "tenant-live-parallel",
        "live-parallel@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    conversation = _seed_conversation(
        db_session, owner.tenant_id, owner.user_id, "Live parallel children"
    )
    auth = _auth(owner.tenant_id, owner.user_id)
    context = _child_context(db_session, conversation.id, auth)
    children = [
        _child(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            conversation_id=conversation.id,
            model=_model(),
            label=f"parallel-{index}",
        )
        for index in range(2)
    ]
    db_session.add_all(children)
    db_session.commit()

    async def _run() -> list[Any]:
        return await asyncio.wait_for(
            asyncio.gather(
                *(
                    run_child_turn(
                        context=context,
                        child=child,
                        task="Reply with exactly one word: alpha. Do not call any tools.",
                    )
                    for child in children
                )
            ),
            timeout=PROOF_TIMEOUT_SECONDS,
        )

    outcomes = asyncio.run(_run())

    assert len(outcomes) == 2
    for outcome in outcomes:
        assert outcome.stop_reason == "completed"
        assert outcome.text.strip()


@requires_provider_key
def test_six_children_answer_under_load(
    db_session: Session,
    seed_user: Any,
) -> None:
    """Six children run at once and every one answers through the live model."""
    owner = seed_user(
        "tenant-live-load",
        "live-load@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    conversation = _seed_conversation(db_session, owner.tenant_id, owner.user_id, "Live child load")
    auth = _auth(owner.tenant_id, owner.user_id)
    context = _child_context(db_session, conversation.id, auth)
    children = [
        _child(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            conversation_id=conversation.id,
            model=_model(),
            label=f"load-{index}",
        )
        for index in range(6)
    ]
    db_session.add_all(children)
    db_session.commit()

    async def _run() -> list[Any]:
        return await asyncio.wait_for(
            asyncio.gather(
                *(
                    run_child_turn(
                        context=context,
                        child=child,
                        task="Reply with exactly one word: ok. Do not call any tools.",
                    )
                    for child in children
                )
            ),
            timeout=PROOF_TIMEOUT_SECONDS,
        )

    outcomes = asyncio.run(_run())

    assert len(outcomes) == 6
    for outcome in outcomes:
        assert outcome.stop_reason == "completed"
        assert outcome.text.strip()
