"""End-to-end test for the DeepSpace named teammate worker.

The provider is stubbed, so the check exercises the real team service, the
real team tools, the real subagent worker, and the real durable records
against the isolated PostgreSQL test database.  Only the model provider and
the broker are replaced: the worker task itself is the product's own.
"""

from __future__ import annotations

import asyncio
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
    TEAM_MESSAGE_STATE_DELIVERED,
    TEAM_MESSAGE_STATE_QUEUED,
    TeamMember,
    TeamMessage,
)
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.team import TEAM_TOOLS
from app.deepspace.workers import subagent_tasks
from app.providers.services.types import ChatGenerateResponse

#: The answer the scripted provider gives the teammate.
ANSWER = "The teammate found three sources."

#: The message the agent sends to the teammate.
MESSAGE = "Collect three sources for me."


class StubProvider:
    """A provider that answers with a scripted sequence of responses."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        """Create the provider.

        Args:
            script: The responses the provider gives, in order.
        """
        self.script = list(script)
        self.requests: list[Any] = []

    def generate(self, request: Any) -> ChatGenerateResponse:
        """Answer one provider request.

        Args:
            request: The provider request.

        Returns:
            The scripted response.
        """
        self.requests.append(request)
        payload = self.script.pop(0) if self.script else {"content": ANSWER}
        return ChatGenerateResponse(
            content=str(payload.get("content") or ""),
            tool_calls=payload.get("tool_calls"),
        )


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
def team_worker_environment(
    db_session: Session,
    seed_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Build a conversation, a team context, and a scripted provider.

    Args:
        db_session: The test's database session.
        seed_user: The tenant and user factory.
        monkeypatch: The patch fixture.

    Returns:
        The environment of one teammate round trip.
    """
    owner = seed_user(
        "tenant-team-worker",
        "team-worker@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    conversation = Conversation(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        kind="deepspace",
        title="Team worker",
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

    provider = StubProvider([{"content": ANSWER}])
    candidate = SimpleNamespace(
        provider_type="stub",
        model_name="stub-model",
        base_url="http://stub.invalid",
        api_key="stub-key",
    )

    class _StubChatService(DeepSpaceChatService):
        """The product's chat service with a scripted provider route."""

        def build_tool_context(self, **kwargs: Any) -> Any:
            """Build the tool context with the scripted provider.

            Args:
                kwargs: The context arguments the worker passes.

            Returns:
                The tool context.
            """
            context = super().build_tool_context(**kwargs)
            return replace(
                context,
                provider_registry=SimpleNamespace(
                    get_chat_provider_from_selection=lambda _candidate: provider
                ),
                provider_selection=SimpleNamespace(
                    resolve_chat=lambda **_kwargs: SimpleNamespace(candidates=[candidate])
                ),
            )

    monkeypatch.setattr(subagent_tasks, "DeepSpaceChatService", _StubChatService)

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
            token_id="team-worker-test",
        ),
        conversation_id=conversation.id,
        settings=get_settings(),
        db=db_session,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        run_id=run.id,
    )
    yield {
        "context": context,
        "db_session": db_session,
        "provider": provider,
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


def test_a_teammate_answers_the_message_it_was_sent(team_worker_environment: Any) -> None:
    """A message reaches a teammate, the teammate works, and the answer lands."""
    context = team_worker_environment["context"]
    db_session = team_worker_environment["db_session"]
    provider = team_worker_environment["provider"]
    dispatched = team_worker_environment["dispatched"]
    conversation_id = team_worker_environment["conversation_id"]

    spawned = asyncio.run(_tool("spawn_teammate").handler({"name": "scout"}, context))
    assert spawned["status"] == "ok"
    assert spawned["member"]["state"] == TEAM_MEMBER_STATE_IDLE

    child = _child(db_session, conversation_id)
    # The teammate's own run is the record the worker picks up.
    assert child.mode == "continuable"
    assert child.status == "idle"
    assert child.delegation_depth == 1
    assert child.descriptor["version"] == 3
    assert child.descriptor["mode"] == "continuable"
    assert child.busy is False
    # The teammate's place in the conversation's delegation tree is recorded.
    assert child.parent_run_id == team_worker_environment["run_id"]
    assert child.parent_agent_id is None

    sent = asyncio.run(
        _tool("team_send_message").handler({"to": "scout", "body": MESSAGE}, context)
    )
    assert sent["status"] == "ok"
    # The teammate is away, so the message waits for it.
    assert sent["queued"] is True
    assert sent["dispatched"] is True
    assert len(dispatched) == 1
    turn = dispatched[0]
    assert turn["child_id"] == str(child.id)
    assert turn["conversation_id"] == str(conversation_id)
    assert turn["message"] == MESSAGE

    # The worker task is the product's own; only the broker is replaced.
    stop_reason = subagent_tasks.run_subagent_turn_task.run(**turn)
    assert stop_reason == "completed"

    member = _member(db_session, conversation_id)
    assert member.state == TEAM_MEMBER_STATE_FINISHED

    child = _child(db_session, conversation_id)
    assert child.result_text == ANSWER
    assert child.stop_reason == "completed"
    assert child.status == "idle"
    assert child.turn_count == 1
    assert child.busy is False

    # The teammate's own context holds the message it answered.
    assert child.turn_history[0]["role"] == "user"
    assert child.turn_history[0]["content"] == MESSAGE
    assert child.turn_history[1]["role"] == "assistant"
    assert child.turn_history[1]["content"] == ANSWER

    # The provider saw the message the teammate was sent.
    sent_messages = provider.requests[0].messages
    assert any(MESSAGE in str(message.get("content") or "") for message in sent_messages)

    # The message that waited for the teammate is delivered by its turn.
    db_session.expire_all()
    message = (
        db_session.query(TeamMessage).filter(TeamMessage.conversation_id == conversation_id).one()
    )
    assert message.state == TEAM_MESSAGE_STATE_DELIVERED
    assert message.delivered_at is not None
    assert message.body == MESSAGE


def test_a_message_that_waited_for_a_teammate_is_delivered_by_its_turn(
    team_worker_environment: Any,
) -> None:
    """A message queued for a teammate away is delivered when it works."""
    context = team_worker_environment["context"]
    db_session = team_worker_environment["db_session"]
    conversation_id = team_worker_environment["conversation_id"]

    asyncio.run(_tool("spawn_teammate").handler({"name": "scout"}, context))
    sent = asyncio.run(
        _tool("team_send_message").handler({"to": "scout", "body": MESSAGE}, context)
    )
    assert sent["queued"] is True

    db_session.expire_all()
    queued = (
        db_session.query(TeamMessage).filter(TeamMessage.conversation_id == conversation_id).one()
    )
    assert queued.state == TEAM_MESSAGE_STATE_QUEUED
    assert queued.delivered_at is None

    turn = team_worker_environment["dispatched"][0]
    subagent_tasks.run_subagent_turn_task.run(**turn)

    db_session.expire_all()
    delivered = (
        db_session.query(TeamMessage).filter(TeamMessage.conversation_id == conversation_id).one()
    )
    assert delivered.state == TEAM_MESSAGE_STATE_DELIVERED
    assert delivered.delivered_at is not None


def test_the_agent_reads_the_answer_a_teammate_reported(team_worker_environment: Any) -> None:
    """The teammate's answer is readable through the team tools."""
    context = team_worker_environment["context"]
    db_session = team_worker_environment["db_session"]

    asyncio.run(_tool("spawn_teammate").handler({"name": "scout"}, context))
    asyncio.run(_tool("team_send_message").handler({"to": "scout", "body": MESSAGE}, context))
    turn = team_worker_environment["dispatched"][0]
    subagent_tasks.run_subagent_turn_task.run(**turn)

    listed = asyncio.run(_tool("list_teammates").handler({}, context))
    assert listed["status"] == "ok"
    assert listed["members"][0]["name"] == "scout"
    assert listed["members"][0]["state"] == TEAM_MEMBER_STATE_FINISHED
    assert listed["members"][0]["result"] == ANSWER

    db_session.expire_all()


def test_a_teammate_answers_the_next_message_it_is_sent(team_worker_environment: Any) -> None:
    """A teammate's queue survives the turn that answered the first message."""
    context = team_worker_environment["context"]
    db_session = team_worker_environment["db_session"]
    provider = team_worker_environment["provider"]
    dispatched = team_worker_environment["dispatched"]
    conversation_id = team_worker_environment["conversation_id"]

    asyncio.run(_tool("spawn_teammate").handler({"name": "scout"}, context))
    asyncio.run(_tool("team_send_message").handler({"to": "scout", "body": MESSAGE}, context))
    subagent_tasks.run_subagent_turn_task.run(**dispatched[0])

    second_message = "And two more sources?"
    provider.script = [{"content": "Two more sources, with their dates."}]
    sent = asyncio.run(
        _tool("team_send_message").handler({"to": "scout", "body": second_message}, context)
    )
    assert sent["status"] == "ok"
    assert sent["dispatched"] is True

    stop_reason = subagent_tasks.run_subagent_turn_task.run(**dispatched[1])
    assert stop_reason == "completed"

    member = _member(db_session, conversation_id)
    assert member.state == TEAM_MEMBER_STATE_FINISHED

    child = _child(db_session, conversation_id)
    assert child.result_text == "Two more sources, with their dates."
    assert child.turn_count == 2
    # The second turn answers in the context the first turn built.
    assert len(child.turn_history) == 4
    assert child.turn_history[2]["content"] == second_message
    assert child.turn_history[3]["content"] == "Two more sources, with their dates."
    second_request = provider.requests[1].messages
    assert any(ANSWER in str(message.get("content") or "") for message in second_request)
    assert any(second_message in str(message.get("content") or "") for message in second_request)
