"""End-to-end tests for the DeepSpace workspace routes.

The workspace catalogue, the creation of a named workspace, and the
workspace one conversation works in are reachable over HTTP, and every one
of them is scoped to the tenant and the user that owns the conversation.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from tests.conftest import SeededUser


def _headers(seeded: SeededUser) -> dict[str, str]:
    """Build the request headers for one seeded user.

    Args:
        seeded: The seeded user.

    Returns:
        The headers.
    """
    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles={"admin"},
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(seeded.tenant_id),
    }


def _conversation(client: TestClient, headers: dict[str, str], title: str) -> str:
    """Create one conversation for a test.

    Args:
        client: The test client.
        headers: The request headers.
        title: The conversation's title.

    Returns:
        The conversation's identifier.
    """
    created = client.post("/api/v1/deepspace/chats", headers=headers, json={"title": title})
    assert created.status_code == 200
    return str(created.json()["id"])


def test_the_workspaces_are_published(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-catalogue@example.com", "StrongPass!1234", ("admin",)
    )

    response = client.get("/api/v1/deepspace/chats/workspaces", headers=_headers(seeded))

    assert response.status_code == 200
    payload = response.json()
    assert payload["default"] == "default"
    workspaces = payload["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == "default"
    assert workspaces[0]["name"] == "Default"
    assert workspaces[0]["is_default"] is True
    assert workspaces[0]["path"]


def test_a_named_workspace_is_created_and_listed(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-create@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)

    created = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=headers,
        json={"name": "My Project"},
    )

    assert created.status_code == 200
    record = created.json()
    assert record["id"] == "my-project"
    assert record["name"] == "My Project"
    assert record["is_default"] is False
    assert record["path"]

    listed = client.get("/api/v1/deepspace/chats/workspaces", headers=headers)
    assert listed.status_code == 200
    ids = [workspace["id"] for workspace in listed.json()["workspaces"]]
    assert ids == ["default", "my-project"]


def test_an_invalid_workspace_name_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-invalid@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)

    for name in ("", "   ", "..", "a/b"):
        refused = client.post(
            "/api/v1/deepspace/chats/workspaces",
            headers=headers,
            json={"name": name},
        )
        assert refused.status_code == 400


def test_a_duplicate_workspace_name_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-duplicate@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    created = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=headers,
        json={"name": "Reports"},
    )
    assert created.status_code == 200

    repeated = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=headers,
        json={"name": "Reports"},
    )

    assert repeated.status_code == 409


def test_a_conversation_records_the_workspace_it_works_in(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-select@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    conversation_id = _conversation(client, headers, "Workspace conversation")
    created = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=headers,
        json={"name": "Reports"},
    )
    assert created.status_code == 200
    workspace_id = created.json()["id"]

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace",
        headers=headers,
        json={"workspace": workspace_id},
    )

    assert recorded.status_code == 200
    assert recorded.json() == {"workspace": workspace_id}

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/workspace", headers=headers)
    assert read_back.status_code == 200
    payload = read_back.json()
    assert payload["recorded"] == workspace_id
    assert payload["workspace"]["id"] == workspace_id
    assert payload["workspace"]["is_default"] is False


def test_a_conversation_without_a_selection_works_in_the_default_workspace(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-default@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    conversation_id = _conversation(client, headers, "Default workspace conversation")

    response = client.get(f"/api/v1/deepspace/chats/{conversation_id}/workspace", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["recorded"] is None
    assert payload["workspace"]["id"] == "default"
    assert payload["workspace"]["is_default"] is True


def test_an_unknown_workspace_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-unknown@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    conversation_id = _conversation(client, headers, "Unknown workspace conversation")

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace",
        headers=headers,
        json={"workspace": "no-such-workspace"},
    )

    assert refused.status_code == 400


def test_one_tenant_cannot_see_or_select_another_tenants_workspace(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    first = seed_user("Workspace One", "workspace-one@example.com", "StrongPass!1234", ("admin",))
    second = seed_user("Workspace Two", "workspace-two@example.com", "StrongPass!1234", ("admin",))
    created = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=_headers(first),
        json={"name": "Private"},
    )
    assert created.status_code == 200
    workspace_id = created.json()["id"]

    listed = client.get("/api/v1/deepspace/chats/workspaces", headers=_headers(second))
    assert listed.status_code == 200
    ids = [workspace["id"] for workspace in listed.json()["workspaces"]]
    assert workspace_id not in ids

    conversation_id = _conversation(client, _headers(second), "Other tenant conversation")
    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace",
        headers=_headers(second),
        json={"workspace": workspace_id},
    )
    assert refused.status_code == 400

    read_back = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace", headers=_headers(second)
    )
    assert read_back.status_code == 200
    assert read_back.json()["recorded"] is None
    assert read_back.json()["workspace"]["id"] == "default"


def test_the_workspace_panel_shows_the_selected_workspace(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-panel@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    conversation_id = _conversation(client, headers, "Panel conversation")
    created = client.post(
        "/api/v1/deepspace/chats/workspaces",
        headers=headers,
        json={"name": "Panel"},
    )
    assert created.status_code == 200
    workspace_id = created.json()["id"]
    path = created.json()["path"]
    from pathlib import Path

    Path(path).mkdir(parents=True, exist_ok=True)
    (Path(path) / "hello.txt").write_text("hello from the workspace\n", encoding="utf-8")

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace",
        headers=headers,
        json={"workspace": workspace_id},
    )
    assert recorded.status_code == 200

    tree = client.get(f"/api/v1/deepspace/chats/{conversation_id}/workspace/tree", headers=headers)
    assert tree.status_code == 200
    names = {entry["name"] for entry in tree.json()["entries"]}
    assert "hello.txt" in names
