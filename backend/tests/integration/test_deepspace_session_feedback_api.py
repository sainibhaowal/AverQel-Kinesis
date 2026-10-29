"""End-to-end checks for DeepSpace session feedback routes."""

from __future__ import annotations

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
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


def test_feedback_is_submitted_read_back_and_counted_in_session_scope(client, seed_user) -> None:
    user = seed_user("Feedback Tenant", "feedback-e2e@example.com", "password123", ("user",))
    headers = _headers(user)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Feedback conversation"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    submitted = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/feedback",
        headers=headers,
        json={
            "rating": "positive",
            "categories": ["helpfulness"],
            "comment": "The answer was useful.",
            "turn": 1,
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["feedback"]["rating"] == "positive"

    listed = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/feedback",
        headers=headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()["feedback"]) == 1

    stats = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/feedback/stats",
        headers=headers,
    )
    assert stats.status_code == 200
    assert stats.json()["total_feedback"] == 1
    assert stats.json()["positive"] == 1

    trace = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/session-query/trace",
        headers=headers,
    )
    assert trace.status_code == 200
    assert any(event["event_type"] == "feedback/given" for event in trace.json()["timeline"])


def test_negative_feedback_requires_a_comment(client, seed_user) -> None:
    user = seed_user(
        "Negative Feedback Tenant", "negative-feedback-e2e@example.com", "password123", ("user",)
    )
    headers = _headers(user)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Negative feedback conversation"},
    )
    conversation_id = created.json()["id"]

    response = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/feedback",
        headers=headers,
        json={"rating": "negative"},
    )
    assert response.status_code == 400
    assert "Comment required" in response.json()["error"]["message"]
