"""End-to-end checks for conversation forks and copied session history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.deepspace.models.message import Message
from app.deepspace.models.message_version import MessageVersion
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.models.workspace_folder import DeepSpaceWorkspaceFolder
from tests.conftest import SeededUser


def _headers(user: SeededUser) -> dict[str, str]:
    token = create_access_token(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        roles={"user"},
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(user.tenant_id),
    }


def test_fork_copies_messages_and_session_events_with_new_sequence_numbers(
    client, seed_user
) -> None:
    user = seed_user("Fork Tenant", "fork-e2e@example.com", "password123", ("user",))
    headers = _headers(user)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Source conversation"},
    )
    assert created.status_code == 200
    source_id = created.json()["id"]

    feedback = client.post(
        f"/api/v1/deepspace/chats/{source_id}/feedback",
        headers=headers,
        json={"rating": "positive", "comment": "source event"},
    )
    assert feedback.status_code == 200

    fork = client.post(
        f"/api/v1/deepspace/chats/{source_id}/fork",
        headers=headers,
        json={"title": "Forked conversation", "copy_files": False},
    )
    assert fork.status_code == 200
    fork_id = fork.json()["id"]

    fork_feedback = client.get(
        f"/api/v1/deepspace/chats/{fork_id}/feedback",
        headers=headers,
    )
    assert fork_feedback.status_code == 200
    assert len(fork_feedback.json()["feedback"]) == 1

    fork_trace = client.get(
        f"/api/v1/deepspace/chats/{fork_id}/session-query/trace",
        headers=headers,
    )
    assert fork_trace.status_code == 200
    timeline = fork_trace.json()["timeline"]
    assert [event["seq"] for event in timeline] == [1, 2]
    assert timeline[0]["event_type"] == "feedback/given"
    assert timeline[1]["event_type"] == "conversation/forked"

    fork_feedback = client.post(
        f"/api/v1/deepspace/chats/{fork_id}/feedback",
        headers=headers,
        json={"rating": "neutral"},
    )
    assert fork_feedback.status_code == 200
    source_feedback = client.get(
        f"/api/v1/deepspace/chats/{source_id}/feedback",
        headers=headers,
    )
    assert len(source_feedback.json()["feedback"]) == 1


def test_fork_at_message_copies_workspace_tree_and_only_prior_history(
    client, seed_user, db_session
) -> None:
    """A message fork copies durable workspace files without sharing IDs."""
    user = seed_user("Fork Files Tenant", "fork-files-e2e@example.com", "password123", ("user",))
    headers = _headers(user)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Source with files"},
    )
    assert created.status_code == 200
    source_id = UUID(created.json()["id"])

    first_time = datetime.now(UTC) - timedelta(seconds=2)
    second_time = datetime.now(UTC) - timedelta(seconds=1)
    first = Message(
        id=uuid4(),
        conversation_id=source_id,
        role="user",
        content="first message",
        created_at=first_time,
    )
    second = Message(
        id=uuid4(),
        conversation_id=source_id,
        role="assistant",
        content="second message",
        created_at=second_time,
    )
    db_session.add_all([first, second])
    db_session.flush()
    first_version = MessageVersion(
        id=uuid4(),
        message_id=first.id,
        version_index=1,
        content=first.content,
        source_type="user",
        created_at=first_time,
    )
    second_version = MessageVersion(
        id=uuid4(),
        message_id=second.id,
        version_index=1,
        content=second.content,
        source_type="assistant",
        created_at=second_time,
    )
    db_session.add_all([first_version, second_version])
    db_session.flush()
    first.active_version_id = first_version.id
    second.active_version_id = second_version.id
    folder = DeepSpaceWorkspaceFolder(
        id=uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        conversation_id=source_id,
        name="notes",
    )
    db_session.add(folder)
    db_session.flush()
    source_file = DeepSpaceWorkspaceFile(
        id=uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        conversation_id=source_id,
        parent_folder_id=folder.id,
        name="first.md",
        content_type="text/markdown",
        content="# first",
        size_bytes=7,
    )
    db_session.add_all(
        [
            source_file,
            DeepSpaceSessionEvent(
                id=uuid4(),
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                conversation_id=source_id,
                seq=1,
                event_type="turn/end",
                turn=0,
                payload={},
                created_at=first_time,
            ),
            DeepSpaceSessionEvent(
                id=uuid4(),
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                conversation_id=source_id,
                seq=2,
                event_type="turn/end",
                turn=1,
                payload={},
                created_at=second_time,
            ),
        ]
    )
    db_session.commit()

    fork = client.post(
        f"/api/v1/deepspace/chats/{source_id}/fork",
        headers=headers,
        json={"message_id": str(first.id), "copy_files": True},
    )
    assert fork.status_code == 200
    fork_id = UUID(fork.json()["id"])

    db_session.expire_all()
    fork_messages = (
        db_session.execute(select(Message).where(Message.conversation_id == fork_id))
        .scalars()
        .all()
    )
    assert [message.content for message in fork_messages] == ["first message"]
    fork_files = (
        db_session.execute(
            select(DeepSpaceWorkspaceFile).where(DeepSpaceWorkspaceFile.conversation_id == fork_id)
        )
        .scalars()
        .all()
    )
    assert len(fork_files) == 1
    assert fork_files[0].id != source_file.id
    assert fork_files[0].parent_folder_id != folder.id
