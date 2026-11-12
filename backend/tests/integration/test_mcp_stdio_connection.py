from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.integrations.models.mcp_server import MCPRegistryEntry, MCPServer
from tests.conftest import SeededUser

_LIVE_STDIO_SERVER = r'''"""Small real MCP server used by the isolated readiness test."""

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

server = Server("isolated-readiness-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ping",
            description="Return a readiness response.",
            inputSchema={"type": "object", "properties": {}},
        )
    ]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
'''


def _headers(seeded: SeededUser) -> dict[str, str]:
    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles={"admin"},
        settings=get_settings(),
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(seeded.tenant_id)}


def _entry(db_session: Session, suffix: str) -> MCPRegistryEntry:
    entry = MCPRegistryEntry(
        source="averqel-reviewed-community",
        server_name=f"local-stdio-{suffix}",
        provider_slug=f"local-stdio-{suffix}",
        publisher_type="official",
        display_name=f"Local STDIO {suffix}",
        publisher="AverQel Test Catalog",
        description="Isolated local STDIO test provider.",
        transport="stdio",
        remote_url=None,
        oauth_requirements={"type": "anonymous"},
        raw_metadata={"server": {"command": "/usr/bin/python3", "args": ["-m", "mcp_test"]}},
        trust_status="approved",
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def _server_for_user(db_session: Session, seeded: SeededUser, entry: MCPRegistryEntry) -> MCPServer:
    return db_session.execute(
        select(MCPServer).where(
            MCPServer.tenant_id == seeded.tenant_id,
            MCPServer.user_id == seeded.user_id,
            MCPServer.registry_entry_id == entry.id,
        )
    ).scalar_one()


def test_stdio_catalog_entry_is_listed_with_exact_launch_metadata(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
) -> None:
    seeded = seed_user(
        "tenant-mcp-stdio-list",
        "mcp-stdio-list@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "list")

    response = client.get("/api/v1/mcp/marketplace?page=1", headers=_headers(seeded))

    assert response.status_code == 200
    listed = next(item for item in response.json()["items"] if item["id"] == str(entry.id))
    assert listed["connectable"] is True
    assert listed["action"] == "connect"
    assert listed["stdio_launch"] == {
        "command": "/usr/bin/python3",
        "args": ["-m", "mcp_test"],
    }


def test_stdio_connect_requires_explicit_launch_confirmation(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
) -> None:
    seeded = seed_user(
        "tenant-mcp-stdio-consent",
        "mcp-stdio-consent@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "consent")

    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_headers(seeded),
    )

    assert response.status_code == 428
    assert "confirming its launch command" in response.json()["error"]["message"]
    assert (
        db_session.execute(
            select(MCPServer).where(MCPServer.registry_entry_id == entry.id)
        ).scalar_one_or_none()
        is None
    )


def test_stdio_connection_stays_connecting_until_worker_and_live_catalog_verification(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    seeded = seed_user(
        "tenant-mcp-stdio-ready",
        "mcp-stdio-ready@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "ready")
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "app.integrations.api.mcp.refresh_server_catalog.delay",
        lambda *args: queued.append(args),
    )

    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_headers(seeded),
        json={"confirm_local_launch": True},
    )

    assert response.status_code == 201
    payload = response.json()["server"]
    assert payload["status"] == "connecting"
    assert payload["registry_entry_id"] == str(entry.id)
    assert queued == [(payload["id"], str(seeded.tenant_id))]
    server = _server_for_user(db_session, seeded, entry)
    assert server.config["command"] == "/usr/bin/python3"
    assert server.config["args"] == ["-m", "mcp_test"]
    assert "mcp_tools_cache" not in server.config
    assert server.enabled is True


def test_stdio_connect_launches_real_local_process_and_returns_live_catalog(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The connect flow becomes ready only after the real worker reads tools."""
    from app.integrations.workers.tasks_mcp import refresh_server_catalog

    seeded = seed_user(
        "tenant-mcp-stdio-live-ready",
        "mcp-stdio-live-ready@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "live-ready")
    script = tmp_path / "live_server.py"
    script.write_text(_LIVE_STDIO_SERVER, encoding="utf-8")
    entry.raw_metadata = {"server": {"command": sys.executable, "args": [str(script)]}}
    db_session.commit()
    monkeypatch.setattr(
        "app.integrations.api.mcp.refresh_server_catalog.delay",
        lambda *args: refresh_server_catalog.run(*args),
    )

    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_headers(seeded),
        json={"confirm_local_launch": True},
    )

    assert response.status_code == 201
    server_payload = response.json()["server"]
    assert server_payload["status"] == "connected"
    server = _server_for_user(db_session, seeded, entry)
    assert server.config["mcp_catalog_tool_count"] == 1
    assert server.config["mcp_tools_cache"][0]["name"] == "ping"


def test_stdio_startup_failure_is_reported_as_failed_not_worker_unavailable(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    """An eager worker startup error is stored as a failed connection, not a queue outage."""
    from app.integrations.workers.tasks_mcp import refresh_server_catalog

    seeded = seed_user(
        "tenant-mcp-stdio-live-failure",
        "mcp-stdio-live-failure@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "live-failure")
    entry.raw_metadata = {"server": {"command": "averqel-test-missing-mcp-executable", "args": []}}
    db_session.commit()
    monkeypatch.setattr(
        "app.integrations.api.mcp.refresh_server_catalog.delay",
        lambda *args: refresh_server_catalog.run(*args),
    )

    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_headers(seeded),
        json={"confirm_local_launch": True},
    )

    assert response.status_code == 201
    assert response.json()["server"]["status"] == "failed"
    server = _server_for_user(db_session, seeded, entry)
    assert server.status == "failed"
    assert server.last_error == "FileNotFoundError: MCP catalog refresh failed"


def test_stdio_queue_failure_marks_connection_failed_and_disables_it(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    seeded = seed_user(
        "tenant-mcp-stdio-worker-failure",
        "mcp-stdio-worker-failure@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "worker-failure")

    def fail_to_queue(*_args: Any) -> None:
        raise RuntimeError("broker endpoint and credential must not be exposed")

    monkeypatch.setattr("app.integrations.api.mcp.refresh_server_catalog.delay", fail_to_queue)
    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_headers(seeded),
        json={"confirm_local_launch": True},
    )

    assert response.status_code == 503
    assert "broker endpoint" not in response.text
    server = _server_for_user(db_session, seeded, entry)
    assert server.status == "failed"
    assert server.enabled is False
    assert server.last_error == "MCP startup could not be scheduled"


def test_explicit_connect_retry_requeues_one_pending_server_without_duplicate_rows(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    seeded = seed_user(
        "tenant-mcp-stdio-retry",
        "mcp-stdio-retry@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    entry = _entry(db_session, "retry")
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "app.integrations.api.mcp.refresh_server_catalog.delay",
        lambda *args: queued.append(args),
    )
    url = f"/api/v1/mcp/marketplace/{entry.id}/connect"
    headers = _headers(seeded)
    body = {"confirm_local_launch": True}

    first = client.post(url, headers=headers, json=body)
    second = client.post(url, headers=headers, json=body)

    assert first.status_code == second.status_code == 201
    assert first.json()["server"]["id"] == second.json()["server"]["id"]
    assert first.json()["server"]["status"] == second.json()["server"]["status"] == "connecting"
    assert len(queued) == 2
    assert (
        len(
            db_session.execute(
                select(MCPServer.id).where(
                    MCPServer.tenant_id == seeded.tenant_id,
                    MCPServer.user_id == seeded.user_id,
                    MCPServer.registry_entry_id == entry.id,
                )
            )
            .scalars()
            .all()
        )
        == 1
    )

    server = _server_for_user(db_session, seeded, entry)
    server.status = "connected"
    db_session.commit()
    third = client.post(url, headers=headers, json=body)

    assert third.status_code == 201
    assert third.json()["server"]["status"] == "connected"
    assert len(queued) == 2


def test_custom_stdio_connection_requires_explicit_launch_confirmation(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "tenant-mcp-custom-stdio-consent",
        "mcp-custom-stdio-consent@example.com",
        "StrongPass!1234",
        ("admin",),
    )

    response = client.post(
        "/api/v1/mcp/servers/custom",
        headers=_headers(seeded),
        json={
            "name": "Unconfirmed local server",
            "server_url": "stdio://local",
            "transport": "stdio",
            "command": "/usr/bin/python3",
            "args": ["-m", "mcp_test"],
        },
    )

    assert response.status_code == 428
    assert "confirming its command" in response.json()["error"]["message"]
