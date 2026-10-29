"""The teammate dispatch proved against a real model provider.

The teammate round trip is proved with a scripted provider: the team service,
the team tools, the worker, and the durable records are the product's own,
and only the model is replaced.  This module proves the same path with a real
provider, so the teammate's answer comes from a live model and not from a
canned response.

The provider key comes from the environment.  ``DEEPSEEK_API_KEY`` names the
key, ``DEEPSEEK_BASE_URL`` names the endpoint when the deployment points at
a different one, and ``DEEPSEEK_MODEL`` names the model.  Without a key the
module skips, because a proof that cannot run proves nothing.

The test needs the database, because a teammate is a durable record, so it
belongs with the other database-backed tests.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.models.agent_runtime import DeepSpaceAgentRun
from app.deepspace.models.agent_team import (
    TEAM_MEMBER_STATE_FINISHED,
    TEAM_MEMBER_STATE_IDLE,
    TeamMember,
)
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.team import TEAM_TOOLS
from app.deepspace.workers import subagent_tasks
from app.providers.services.registry import ProviderRegistry
from app.providers.services.selection_service import DEFAULT_PROVIDER_BASE_URLS
from app.providers.services.types import ProviderSelectionCandidate

#: The provider route the teammate runs under.
PROVIDER_TYPE = "deepseek"

#: The model the teammate runs under, unless the deployment names another one.
DEFAULT_MODEL = "deepseek-chat"

#: The one short message the teammate is sent.
MESSAGE = "Reply with the single word: ready."

#: The word the teammate is asked to answer with.
EXPECTED_WORD = "ready"

#: The name the teammate is addressed by.
TEAMMATE_NAME = "scout"


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
    """Return the model the teammate runs under.

    Returns:
        The model name.
    """
    return str(os.environ.get("DEEPSEEK_MODEL") or "").strip() or DEFAULT_MODEL


requires_provider_key = pytest.mark.skipif(
    not _api_key(),
    reason=(
        "DEEPSEEK_API_KEY is not set; the live teammate proof needs a provider key. "
        "Export DEEPSEEK_API_KEY (with DEEPSEEK_BASE_URL when the deployment uses "
        "another endpoint) and run the test again."
    ),
)


class _RecordingProvider:
    """A provider that records the request before the real provider answers.

    Attributes:
        requests: The requests the child sent to the real provider.
    """

    def __init__(self, provider: Any, requests: list[Any]) -> None:
        """Create the recorder.

        Args:
            provider: The product's real provider.
            requests: The list the requests are recorded in.
        """
        self._provider = provider
        self.requests = requests

    def generate(self, request: Any) -> Any:
        """Answer one provider request with the real provider.

        Args:
            request: The provider request.

        Returns:
            The provider's answer.
        """
        self.requests.append(request)
        return self._provider.generate(request)


class _RecordingRegistry:
    """The product's provider registry with the provider requests recorded.

    Attributes:
        requests: The requests the child sent to the real provider.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        """Create the recorder.

        Args:
            registry: The product's own provider registry.
        """
        self._registry = registry
        self.requests: list[Any] = []

    def get_chat_provider_from_selection(self, selection: Any) -> Any:
        """Return the product's provider for one selection, with the request recorded.

        Args:
            selection: The selected provider candidate.

        Returns:
            The provider the child runs on.
        """
        provider = self._registry.get_chat_provider_from_selection(selection)
        return _RecordingProvider(provider, self.requests)


def _tool(name: str) -> Any:
    """Read one team tool by its name.

    Args:
        name: The tool name.

    Returns:
        The tool spec.
    """
    for spec in TEAM_TOOLS:
        if spec.name == name:
            return spec
    raise AssertionError(f"{name} is not a team tool.")


@pytest.fixture
def live_teammate_environment(
    db_session: Session,
    seed_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Build a conversation, a team context, and the live provider route.

    Args:
        db_session: The test's database session.
        seed_user: The tenant and user factory.
        monkeypatch: The patch fixture.

    Returns:
        The environment of one teammate round trip.
    """
    owner = seed_user(
        "tenant-team-live",
        "team-live@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    conversation = Conversation(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        kind="deepspace",
        title="Team live provider",
    )
    db_session.add(conversation)
    db_session.commit()
    run = DeepSpaceAgentRun(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        conversation_id=conversation.id,
    )
    db_session.add(run)
    db_session.commit()

    settings = get_settings()
    candidate = ProviderSelectionCandidate(
        provider_type=PROVIDER_TYPE,
        model_name=_model(),
        feature_scope="chat",
        source="env_fallback",
        base_url=_base_url(),
        api_key=_api_key(),
    )
    registry = _RecordingRegistry(ProviderRegistry(settings))

    class _LiveChatService(DeepSpaceChatService):
        """The product's chat service with the live provider route."""

        def build_tool_context(self, **kwargs: Any) -> Any:
            """Build the tool context with the live provider route.

            Args:
                kwargs: The context arguments the worker passes.

            Returns:
                The tool context.
            """
            context = super().build_tool_context(**kwargs)
            return replace(
                context,
                provider_registry=registry,
                provider_selection=SimpleNamespace(
                    resolve_chat=lambda **_kwargs: SimpleNamespace(candidates=[candidate])
                ),
            )

    monkeypatch.setattr(subagent_tasks, "DeepSpaceChatService", _LiveChatService)

    dispatched: list[dict[str, Any]] = []

    def _capture(*args: Any, **kwargs: Any) -> None:
        """Record the turn arguments the tool dispatched.

        Args:
            args: The positional arguments, which the tool does not use.
            kwargs: The keyword arguments, which carry the turn arguments.
        """
        dispatched.append(dict(kwargs.get("kwargs") or {}))

    monkeypatch.setattr(subagent_tasks.run_subagent_turn_task, "apply_async", _capture)

    context = ToolContext(
        auth=AuthContext(
            user_id=owner.user_id,
            tenant_id=owner.tenant_id,
            roles=frozenset({"admin"}),
            token_id="team-live-test",
        ),
        conversation_id=conversation.id,
        settings=settings,
        db=db_session,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        run_id=run.id,
    )
    return {
        "context": context,
        "db_session": db_session,
        "registry": registry,
        "dispatched": dispatched,
        "tenant_id": owner.tenant_id,
        "user_id": owner.user_id,
        "conversation_id": conversation.id,
        "run_id": run.id,
    }


def _member(db_session: Session, conversation_id: uuid.UUID) -> TeamMember:
    """Read the one teammate of a conversation.

    Args:
        db_session: The test's database session.
        conversation_id: The conversation the teammate belongs to.

    Returns:
        The teammate row.
    """
    db_session.expire_all()
    return db_session.query(TeamMember).filter(TeamMember.conversation_id == conversation_id).one()


def _child(db_session: Session, conversation_id: uuid.UUID) -> DeepSpaceSubagentRun:
    """Read the one child run of a conversation.

    Args:
        db_session: The test's database session.
        conversation_id: The conversation the child belongs to.

    Returns:
        The child run row.
    """
    db_session.expire_all()
    return (
        db_session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == conversation_id)
        .one()
    )


@requires_provider_key
def test_a_teammate_answers_through_the_real_provider(
    live_teammate_environment: Any,
) -> None:
    """A message reaches a teammate and the teammate answers with a live model."""
    context = live_teammate_environment["context"]
    db_session = live_teammate_environment["db_session"]
    registry = live_teammate_environment["registry"]
    dispatched = live_teammate_environment["dispatched"]
    conversation_id = live_teammate_environment["conversation_id"]

    spawned = asyncio.run(
        _tool("spawn_teammate").handler(
            {
                "name": TEAMMATE_NAME,
                "provider": PROVIDER_TYPE,
                "model": _model(),
                "tool_filter": ["list_teammates"],
            },
            context,
        )
    )
    assert spawned["status"] == "ok"
    assert spawned["member"]["state"] == TEAM_MEMBER_STATE_IDLE

    sent = asyncio.run(
        _tool("team_send_message").handler({"to": TEAMMATE_NAME, "body": MESSAGE}, context)
    )
    assert sent["status"] == "ok"
    assert sent["queued"] is True
    assert sent["dispatched"] is True
    assert len(dispatched) == 1

    # The worker task is the product's own; only the model provider is real.
    stop_reason = subagent_tasks.run_subagent_turn_task.run(**dispatched[0])
    assert stop_reason == "completed"

    # The provider was asked, over the network, for this answer.
    assert registry.requests, "The teammate did not reach the provider."
    request = registry.requests[0]
    assert request.model == _model()
    assert any(MESSAGE in str(message.get("content") or "") for message in request.messages)

    child = _child(db_session, conversation_id)
    assert child.stop_reason == "completed"
    assert child.result_text
    assert EXPECTED_WORD in child.result_text.lower()

    member = _member(db_session, conversation_id)
    assert member.state == TEAM_MEMBER_STATE_FINISHED
