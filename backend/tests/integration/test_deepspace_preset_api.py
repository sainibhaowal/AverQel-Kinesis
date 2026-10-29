"""HTTP end-to-end checks for the DeepSpace agent preset routes.

The checks run against the isolated test services, so they exercise the real
routes, the real authorization, and the real database.  The custom preset
carries the settings the user edited, so they are stored, read back, and
refused when the product does not accept them.
"""

from __future__ import annotations

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
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


def test_the_preset_route_records_and_reads_the_mode(client, seed_user) -> None:
    seeded = seed_user("Preset Tenant", "preset@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Preset chat"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/preset",
        headers=headers,
        json={"preset": "minimal"},
    )

    assert recorded.status_code == 200
    assert recorded.json()["preset"] == "minimal"
    assert recorded.json()["deployment_default"] == "standard"

    read = client.get(f"/api/v1/deepspace/chats/{conversation_id}/preset", headers=headers)

    assert read.status_code == 200
    assert read.json()["preset"] == "minimal"
    assert read.json()["recorded"] == "minimal"


def test_the_custom_mode_settings_are_stored_and_read_back(client, seed_user) -> None:
    seeded = seed_user(
        "Custom Preset Tenant", "custom-preset@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Custom preset chat"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    settings = {
        "name": "Research",
        "tools": ["fs_read", "web_search"],
        "sandbox_mode": "read-only",
        "reasoning_effort": "high",
        "subagent_max_depth": 1,
        "ptc_enabled": False,
    }
    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/preset",
        headers=headers,
        json={"preset": "custom", "settings": settings},
    )

    assert recorded.status_code == 200
    assert recorded.json()["preset"] == "custom"
    assert recorded.json()["deployment_default"] == "standard"

    read = client.get(f"/api/v1/deepspace/chats/{conversation_id}/preset", headers=headers)

    assert read.status_code == 200
    assert read.json()["preset"] == "custom"
    assert read.json()["settings"] == settings


def test_a_custom_mode_setting_the_product_does_not_accept_is_refused(client, seed_user) -> None:
    seeded = seed_user("Bad Preset Tenant", "bad-preset@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Bad preset chat"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/preset",
        headers=headers,
        json={"preset": "custom", "settings": {"sandbox_mode": "root"}},
    )

    assert refused.status_code == 400


def test_a_built_in_mode_cannot_receive_custom_settings(client, seed_user) -> None:
    seeded = seed_user(
        "Built-in Preset Tenant", "builtin-preset@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Built-in preset chat"},
    )
    assert created.status_code == 200

    refused = client.put(
        f"/api/v1/deepspace/chats/{created.json()['id']}/preset",
        headers=headers,
        json={"preset": "standard", "settings": {"ptc_enabled": True}},
    )

    assert refused.status_code == 400


def test_the_custom_default_settings_are_stored_and_read_back(client, seed_user) -> None:
    seeded = seed_user(
        "Default Preset Tenant", "default-preset@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))
    settings = {"ptc_enabled": True, "plan_mode_by_default": True, "reasoning_effort": "high"}

    recorded = client.put(
        "/api/v1/deepspace/chats/presets/default",
        headers=headers,
        json={"preset": "custom", "settings": settings},
    )

    assert recorded.status_code == 200
    assert recorded.json()["preset"] == "custom"
    assert recorded.json()["recorded"] == "custom"
    assert recorded.json()["settings"] == settings
    assert recorded.json()["custom_settings"] == settings

    read = client.get("/api/v1/deepspace/chats/presets/default", headers=headers)

    assert read.status_code == 200
    assert read.json()["preset"] == "custom"
    assert read.json()["settings"] == settings
    assert read.json()["custom_settings"] == settings

    switched = client.put(
        "/api/v1/deepspace/chats/presets/default",
        headers=headers,
        json={"preset": "standard"},
    )

    assert switched.status_code == 200
    assert switched.json()["preset"] == "standard"
    assert switched.json()["settings"] is None
    assert switched.json()["custom_settings"] == settings

    switched_read = client.get("/api/v1/deepspace/chats/presets/default", headers=headers)

    assert switched_read.status_code == 200
    assert switched_read.json()["preset"] == "standard"
    assert switched_read.json()["settings"] is None
    assert switched_read.json()["custom_settings"] == settings

    catalogue = client.get("/api/v1/deepspace/chats/presets", headers=headers)
    assert catalogue.status_code == 200
    custom_catalogue = next(
        preset for preset in catalogue.json()["presets"] if preset["id"] == "custom"
    )
    assert custom_catalogue["settings"] == settings
    assert custom_catalogue["ptc_enabled"] is True

    plugins = client.get("/api/v1/deepspace/chats/plugins", headers=headers)
    assert plugins.status_code == 200
    assert plugins.json()["default_preset"] == "standard"
    custom_inventory = next(
        preset for preset in plugins.json()["session_presets"] if preset["id"] == "custom"
    )
    assert custom_inventory["is_default"] is False
    assert custom_inventory["ptc_enabled"] is True

    selected_again = client.put(
        "/api/v1/deepspace/chats/presets/default",
        headers=headers,
        json={"preset": "custom"},
    )
    assert selected_again.status_code == 200
    assert selected_again.json()["preset"] == "custom"
    assert selected_again.json()["settings"] == settings

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Inherited custom preset chat"},
    )
    assert created.status_code == 200

    inherited = client.get(
        f"/api/v1/deepspace/chats/{created.json()['id']}/preset",
        headers=headers,
    )

    assert inherited.status_code == 200
    assert inherited.json()["preset"] == "custom"
    assert inherited.json()["settings"] == settings


def test_a_bare_conversation_custom_selection_keeps_the_saved_custom_tools(
    client, seed_user
) -> None:
    """Selecting Custom on a conversation keeps the user's saved tool list."""
    seeded = seed_user(
        "Inherited Custom Tenant", "inherited-custom@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))
    settings = {"tools": ["fs_read"]}

    defaulted = client.put(
        "/api/v1/deepspace/chats/presets/default",
        headers=headers,
        json={"preset": "custom", "settings": settings},
    )
    assert defaulted.status_code == 200

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Explicit Custom preset chat"},
    )
    assert created.status_code == 200

    selected = client.put(
        f"/api/v1/deepspace/chats/{created.json()['id']}/preset",
        headers=headers,
        json={"preset": "custom"},
    )
    assert selected.status_code == 200

    read = client.get(
        f"/api/v1/deepspace/chats/{created.json()['id']}/preset",
        headers=headers,
    )
    assert read.status_code == 200
    assert read.json()["preset"] == "custom"
    assert read.json()["settings"] == settings


def test_an_unknown_mode_is_refused(client, seed_user) -> None:
    seeded = seed_user(
        "Unknown Preset Tenant", "unknown-preset@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Unknown preset chat"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/preset",
        headers=headers,
        json={"preset": "does-not-exist"},
    )

    assert refused.status_code == 400


def test_the_preset_catalogue_publishes_the_mode_settings(client, seed_user) -> None:
    seeded = seed_user(
        "Preset Catalogue Tenant", "preset-list@example.com", "password123", ("user",)
    )
    headers = _auth_headers(seeded, roles=("user",))

    response = client.get("/api/v1/deepspace/chats/presets", headers=headers)

    assert response.status_code == 200
    presets = {preset["id"]: preset for preset in response.json()["presets"]}
    assert set(presets) == {"standard", "ptc", "minimal", "creator", "custom"}
    assert presets["ptc"]["ptc_enabled"] is True
    assert presets["minimal"]["sandbox_mode"] == "read-only"
    assert presets["custom"]["editable"] is True
    assert "subagent_max_depth" in presets["standard"]


def test_named_custom_presets_can_be_created_selected_updated_and_deleted(
    client, seed_user
) -> None:
    seeded = seed_user("Named Custom Tenant", "named-custom@example.com", "password123", ("user",))
    headers = _auth_headers(seeded, roles=("user",))
    settings = {
        "tools": [],
        "ptc_enabled": False,
        "plan_mode_by_default": True,
        "sandbox_mode": "read-only",
    }

    created = client.post(
        "/api/v1/deepspace/chats/presets/custom",
        headers=headers,
        json={
            "name": "Safe research",
            "description": "Read-only research mode",
            "settings": settings,
        },
    )

    assert created.status_code == 200
    custom = created.json()["preset"]
    assert custom["id"].startswith("custom_")
    assert custom["settings"]["tools"] == []
    assert custom["sandbox_mode"] == "read-only"

    listed = client.get("/api/v1/deepspace/chats/presets", headers=headers)
    assert listed.status_code == 200
    assert any(row["id"] == custom["id"] for row in listed.json()["presets"])

    updated = client.put(
        f"/api/v1/deepspace/chats/presets/custom/{custom['id']}",
        headers=headers,
        json={"settings": {**settings, "tools": ["fs_read"]}},
    )
    assert updated.status_code == 200
    assert updated.json()["preset"]["settings"]["tools"] == ["fs_read"]

    defaulted = client.put(
        "/api/v1/deepspace/chats/presets/default",
        headers=headers,
        json={"preset": custom["id"]},
    )
    assert defaulted.status_code == 200
    assert defaulted.json()["preset"] == custom["id"]

    created_chat = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Named custom chat"},
    )
    assert created_chat.status_code == 200
    inherited = client.get(
        f"/api/v1/deepspace/chats/{created_chat.json()['id']}/preset",
        headers=headers,
    )
    assert inherited.status_code == 200
    assert inherited.json()["preset"] == custom["id"]
    assert inherited.json()["settings"]["tools"] == ["fs_read"]

    deleted = client.delete(
        f"/api/v1/deepspace/chats/presets/custom/{custom['id']}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["default"] == "standard"
