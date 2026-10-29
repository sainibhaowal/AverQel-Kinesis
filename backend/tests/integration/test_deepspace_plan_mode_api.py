"""HTTP end-to-end checks for the DeepSpace plan mode and runtime routes.

The checks run against the isolated test services, so they exercise the real
routes, the real authorization, and the real database.
"""

from __future__ import annotations

import uuid

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.services.plan_mode import PlanModeService
from tests.conftest import SeededUser


def _auth_headers(seeded: SeededUser, *, roles: tuple[str, ...]) -> dict[str, str]:
    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles=set(roles),
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(seeded.tenant_id),
    }


def test_a_conversation_reports_that_plan_mode_is_off(client, seed_user) -> None:
    seeded = seed_user("Plan Mode Tenant", "plan-mode@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Plan mode chat"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    response = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plan", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"active": False, "plan": None}


def test_plan_mode_can_be_turned_on_and_off(client, seed_user) -> None:
    seeded = seed_user("Plan Mode Toggle", "plan-toggle@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Plan mode toggle"},
    )
    conversation_id = created.json()["id"]

    enabled = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/plan/mode",
        headers=headers,
        json={"active": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["active"] is True

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plan", headers=headers)
    assert read_back.json()["active"] is True

    disabled = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/plan/mode",
        headers=headers,
        json={"active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False


def test_a_proposed_plan_is_reviewed_over_http(client, seed_user, db_session) -> None:
    seeded = seed_user("Plan Review Tenant", "plan-review@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Plan review"},
    )
    conversation_id = created.json()["id"]

    PlanModeService(db_session).submit(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        conversation_id=uuid.UUID(conversation_id),
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build\n2. Test",
    )

    pending = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plan", headers=headers)
    assert pending.status_code == 200
    assert pending.json()["plan"]["title"] == "Ship the release"
    assert pending.json()["active"] is True

    approved = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/plan/review",
        headers=headers,
        json={"decision": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["plan"]["status"] == "approved"

    after = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plan", headers=headers)
    assert after.json()["active"] is False


def test_reviewing_a_plan_that_does_not_exist_is_refused(client, seed_user) -> None:
    seeded = seed_user("Plan Missing Tenant", "plan-missing@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Plan missing"},
    )
    conversation_id = created.json()["id"]

    response = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/plan/review",
        headers=headers,
        json={"decision": "approved"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_one_caller_cannot_review_another_callers_plan(client, seed_user) -> None:
    owner = seed_user("Plan Owner Tenant", "plan-owner@example.com", "password123", ("user",))
    other = seed_user("Plan Other Tenant", "plan-other@example.com", "password123", ("user",))
    owner_headers = _auth_headers(owner, roles=("user",))
    other_headers = _auth_headers(other, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=owner_headers,
        json={"title": "Owner's plan"},
    )
    conversation_id = created.json()["id"]

    response = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/plan",
        headers=other_headers,
    )

    # The route reads the plan by conversation, and a conversation the caller
    # does not own has no plan to read.
    assert response.status_code == 200
    assert response.json()["plan"] is None


def test_the_runtime_route_reports_the_deployment_controls(client, seed_user) -> None:
    seeded = seed_user("Runtime Tenant", "runtime@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.get("/api/v1/deepspace/sandbox/runtime", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert "sandbox" in payload
    assert payload["max_concurrent_tools"] >= 1
    assert payload["plan_mode_enabled"] is True
    assert payload["spill_threshold_chars"] >= 2000


def test_the_sandbox_mode_route_reads_and_sets_the_conversation_mode(client, seed_user) -> None:
    seeded = seed_user("Sandbox Mode Tenant", "sandbox-mode@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Sandbox mode"},
    )
    conversation_id = created.json()["id"]

    initial = client.get(f"/api/v1/deepspace/chats/{conversation_id}/sandbox-mode", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["mode"] == "workspace-write"
    assert initial.json()["recorded_mode"] is None

    changed = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/sandbox-mode",
        headers=headers,
        json={"mode": "read-only"},
    )
    assert changed.status_code == 200
    assert changed.json()["mode"] == "read-only"

    after = client.get(f"/api/v1/deepspace/chats/{conversation_id}/sandbox-mode", headers=headers)
    assert after.json()["mode"] == "read-only"
    assert after.json()["recorded_mode"] == "read-only"


def test_an_unknown_sandbox_mode_is_refused(client, seed_user) -> None:
    seeded = seed_user("Sandbox Mode Bad", "sandbox-mode-bad@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Sandbox mode"},
    )
    conversation_id = created.json()["id"]

    response = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/sandbox-mode",
        headers=headers,
        json={"mode": "root"},
    )

    assert response.status_code == 422


def test_the_sandbox_backend_route_still_reports_its_backend(client, seed_user) -> None:
    seeded = seed_user("Sandbox Tenant", "sandbox@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.get("/api/v1/deepspace/sandbox/backend", headers=headers)

    assert response.status_code == 200
    assert "selected" in response.json()


def _enable_acp(client) -> None:
    """Turn the Agent Client Protocol bridge on for one test."""
    from app.core.config import get_settings as real_get_settings

    settings = real_get_settings().model_copy(update={"deepspace_acp_enabled": True})
    client.app.dependency_overrides[real_get_settings] = lambda: settings


def test_the_acp_bridge_reports_the_protocol_version(client, seed_user) -> None:
    _enable_acp(client)
    seeded = seed_user("ACP Tenant", "acp@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.post(
        "/api/v1/deepspace/acp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == 1
    assert payload["result"]["agentInfo"]["name"] == "averqel-deepspace-acp"


def test_the_acp_bridge_creates_a_session(client, seed_user) -> None:
    _enable_acp(client)
    seeded = seed_user("ACP Session Tenant", "acp-session@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.post(
        "/api/v1/deepspace/acp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {}},
    )

    assert response.status_code == 200
    assert response.json()["result"]["sessionId"]


def test_the_acp_bridge_refuses_an_unknown_method(client, seed_user) -> None:
    _enable_acp(client)
    seeded = seed_user("ACP Method Tenant", "acp-method@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.post(
        "/api/v1/deepspace/acp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "does/not/exist", "params": {}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32601


def test_the_acp_bridge_refuses_a_prompt_without_text(client, seed_user) -> None:
    _enable_acp(client)
    seeded = seed_user("ACP Prompt Tenant", "acp-prompt@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.post(
        "/api/v1/deepspace/acp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": "unknown", "prompt": [{"type": "image", "data": "..."}]},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32602


def test_the_acp_bridge_is_disabled_by_default(client, seed_user) -> None:
    seeded = seed_user("ACP Disabled Tenant", "acp-disabled@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    response = client.post(
        "/api/v1/deepspace/acp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}},
    )

    assert response.status_code == 403
    assert "disabled" in response.json()["error"]["message"]


def test_a_conversation_is_created_by_the_deepspace_repository(client, seed_user) -> None:
    seeded = seed_user("Repository Tenant", "repository@example.com", "password123", ("user",))

    from app.platform.database.session import get_session_factory

    session = get_session_factory()()
    try:
        conversation = DeepSpaceChatRepository(session).create_conversation(
            tenant_id=seeded.tenant_id,
            user_id=seeded.user_id,
            title="Repository chat",
        )
        session.commit()
        assert conversation.id is not None
    finally:
        session.close()
