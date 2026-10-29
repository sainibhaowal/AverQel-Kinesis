"""End-to-end tests for the new DeepSpace surfaces.

The plugin system, the workspace panel, and the persistent terminals are
reachable over HTTP, and each of them is scoped to the tenant and the user
that owns the conversation.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.deepspace.services.workspace_root import workspace_root_for
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


def test_the_plugin_catalogue_is_published(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Plugin Tenant", "plugin-catalogue@example.com", "StrongPass!1234", ("admin",)
    )

    response = client.get("/api/v1/deepspace/chats/plugins", headers=_headers(seeded))

    assert response.status_code == 200
    payload = response.json()
    assert "plugins" in payload
    assert "global_plugins" in payload
    assert "session_presets" in payload
    assert payload["default_preset"] == "standard"
    assert {preset["id"] for preset in payload["session_presets"]} == {
        "standard",
        "ptc",
        "minimal",
        "creator",
        "custom",
    }
    ids = {plugin["id"] for plugin in payload["plugins"]}
    # The built-in code-metrics plugin ships with the product.
    assert "code-metrics" in ids


def test_a_conversation_records_the_plugins_it_runs(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Plugin Tenant", "plugin-conversation@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)

    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Plugin conversation"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    listed = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plugins", headers=headers)
    assert listed.status_code == 200

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/plugins",
        headers=headers,
        json={"enabled": ["code-metrics"]},
    )
    assert recorded.status_code == 200
    assert recorded.json()["enabled"] == ["code-metrics"]

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/plugins", headers=headers)
    assert read_back.status_code == 200
    assert read_back.json()["recorded"] == ["code-metrics"]


def test_the_workspace_panel_lists_and_reads_files(
    client: TestClient,
    db_session: Session,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Workspace Tenant", "workspace-panel@example.com", "StrongPass!1234", ("admin",)
    )
    from app.deepspace.services.workspace_root import workspace_root_for

    workspace = workspace_root_for(get_settings(), seeded.tenant_id, seeded.user_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "hello.txt").write_text("hello from the workspace\n", encoding="utf-8")
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    conversation_id = str(uuid.uuid4())
    tree = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace/tree", headers=_headers(seeded)
    )
    assert tree.status_code == 200
    names = {entry["name"] for entry in tree.json()["entries"]}
    assert "hello.txt" in names
    assert "src" in names

    nested = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace/tree?path=src",
        headers=_headers(seeded),
    )
    assert nested.status_code == 200
    assert {entry["name"] for entry in nested.json()["entries"]} == {"main.py"}

    read = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace/file?path=hello.txt",
        headers=_headers(seeded),
    )
    assert read.status_code == 200
    assert read.json()["content"] == "hello from the workspace\n"
    assert read.json()["language"] is None

    escaped = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/workspace/file?path=../../etc/passwd",
        headers=_headers(seeded),
    )
    assert escaped.status_code == 400
    del db_session


def test_a_terminal_session_opens_reads_writes_and_closes(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Terminal Tenant", "terminal-session@example.com", "StrongPass!1234", ("admin",)
    )
    conversation_id = str(uuid.uuid4())
    headers = _headers(seeded)

    opened = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals",
        headers=headers,
        json={"shell": "bash", "command": "printf ready"},
    )
    if opened.status_code == 409:
        # The host has no bash, so there is nothing to assert.
        return
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    listed = client.get(f"/api/v1/deepspace/chats/{conversation_id}/terminals", headers=headers)
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()["sessions"]] == [session_id]

    output = ""
    for _ in range(20):
        read = client.get(
            f"/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}", headers=headers
        )
        assert read.status_code == 200
        output = read.json()["output"]
        if "ready" in output:
            break
        time.sleep(0.25)
    # The command the session started with produced its output.
    assert "ready" in output

    # A session that starts with a command ends with that command, so the
    # write goes to a session that is still alive.
    live = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals",
        headers=headers,
        json={"shell": "bash"},
    )
    assert live.status_code == 200
    live_session_id = live.json()["session_id"]

    written = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals/{live_session_id}",
        headers=headers,
        json={"data": "printf hello\n"},
    )
    assert written.status_code == 200
    assert written.json()["written_bytes"] > 0
    assert written.json()["wait_reason"] in {
        "sent",
        "stdin_read",
        "inferred_idle",
        "timeout",
        "session_exit",
    }

    closed = client.delete(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals/{live_session_id}", headers=headers
    )
    assert closed.status_code == 200
    assert closed.json()["closed"] is True

    gone = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals/{live_session_id}", headers=headers
    )
    assert gone.status_code == 404


def test_a_terminal_session_streams_live_pty_output_and_accepts_resize(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Terminal Stream Tenant", "terminal-stream@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    conversation_id = str(uuid.uuid4())
    opened = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals",
        headers=headers,
        json={"shell": "bash"},
    )
    if opened.status_code == 409:
        return
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]
    token = headers["Authorization"].removeprefix("Bearer ")
    stream_path = (
        f"/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}/stream?"
        + urlencode({"token": token, "tenant_id": str(seeded.tenant_id)})
    )

    try:
        with client.websocket_connect(stream_path) as socket:
            ready = None
            for _ in range(3):
                packet = socket.receive_json()
                if packet.get("type") == "ready":
                    ready = packet
                    break
            assert ready == {
                "type": "ready",
                "shell": "bash",
                "pty": True,
                "cwd": str(workspace_root_for(get_settings(), seeded.tenant_id, seeded.user_id)),
                "alive": True,
            }

            socket.send_json({"type": "resize", "columns": 120, "rows": 32})
            socket.send_json({"type": "input", "data": "printf STREAM_RESULT\\n\n"})
            output = bytearray()
            for _ in range(20):
                frame = socket.receive()
                output.extend(frame.get("bytes") or b"")
                if output.count(b"STREAM_RESULT") >= 2:
                    break
            assert output.count(b"STREAM_RESULT") >= 2
    finally:
        client.delete(
            f"/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}",
            headers=headers,
        )


def test_one_tenant_cannot_read_another_tenants_terminal(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    first = seed_user("Terminal One", "terminal-one@example.com", "StrongPass!1234", ("admin",))
    second = seed_user("Terminal Two", "terminal-two@example.com", "StrongPass!1234", ("admin",))
    conversation_id = str(uuid.uuid4())

    opened = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/terminals",
        headers=_headers(first),
        json={"shell": "bash"},
    )
    if opened.status_code == 409:
        return
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    try:
        stolen = client.get(
            f"/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}",
            headers=_headers(second),
        )
        assert stolen.status_code == 404
    finally:
        client.delete(
            f"/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}",
            headers=_headers(first),
        )


def test_the_preset_catalogue_is_published(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Preset Tenant", "preset-catalogue@example.com", "StrongPass!1234", ("admin",)
    )

    response = client.get("/api/v1/deepspace/chats/presets", headers=_headers(seeded))

    assert response.status_code == 200
    ids = {preset["id"] for preset in response.json()["presets"]}
    assert {"standard", "ptc", "minimal", "creator", "custom"} <= ids


def test_a_conversation_records_the_preset_it_runs(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Preset Tenant", "preset-conversation@example.com", "StrongPass!1234", ("admin",)
    )
    headers = _headers(seeded)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Preset conversation"},
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

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/preset", headers=headers)
    assert read_back.status_code == 200
    assert read_back.json()["preset"] == "minimal"
    assert read_back.json()["recorded"] == "minimal"


def test_an_unknown_preset_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Preset Tenant", "preset-unknown@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    created = client.post(
        "/api/v1/deepspace/chats",
        headers=headers,
        json={"title": "Preset conversation"},
    )
    conversation_id = created.json()["id"]

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/preset",
        headers=headers,
        json={"preset": "not-a-preset"},
    )
    assert refused.status_code == 400
