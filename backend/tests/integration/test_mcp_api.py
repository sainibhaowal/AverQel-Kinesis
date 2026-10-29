from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.integrations.catalog.mcp_official_providers import CURATED_MCP_CATALOG_SOURCE
from app.integrations.models.mcp_server import MCPRegistryEntry
from app.integrations.services.mcp_catalog_service import MCPCatalogService
from app.integrations.services.mcp_oauth_service import MCPServerOAuthService
from tests.conftest import SeededUser


def _auth_headers(seeded: SeededUser, *, roles: tuple[str, ...] = ("admin",)) -> dict[str, str]:
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


def test_marketplace_rejects_blank_verified_filter(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "tenant-mcp-marketplace",
        "mcp-marketplace@example.com",
        "StrongPass!1234",
        ("admin",),
    )

    response = client.get(
        "/api/v1/mcp/marketplace?verified=&page=1",
        headers=_auth_headers(seeded),
    )

    assert response.status_code == 422


def test_marketplace_accepts_omitted_verified_filter(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "tenant-mcp-marketplace-omitted",
        "mcp-marketplace-omitted@example.com",
        "StrongPass!1234",
        ("admin",),
    )

    response = client.get(
        "/api/v1/mcp/marketplace?page=1",
        headers=_auth_headers(seeded),
    )

    assert response.status_code == 200
    payload = response.json()
    assert "items" in payload
    assert "total" in payload


def test_legacy_catalog_import_route_is_removed(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "tenant-mcp-marketplace-user",
        "mcp-marketplace-user@example.com",
        "StrongPass!1234",
        ("user",),
    )

    response = client.post(
        "/api/v1/mcp/catalog/import",
        json={"manifest_url": "https://vendor.example.com/mcp.json"},
        headers=_auth_headers(seeded, roles=("user",)),
    )

    assert response.status_code == 404


def _seed_marketplace_provider(db_session: Session) -> None:
    """Seed one curated marketplace entry for the marketplace checks.

    The shipped official catalog is empty, because AverQel brokers the
    connectable apps through Composio. The marketplace checks still need a
    reviewed provider to show, so they seed their own.
    """
    from app.integrations.catalog.mcp_official_providers import (
        CuratedMCPProvider,
        CuratedMCPTool,
    )

    provider = CuratedMCPProvider(
        provider_slug="google-gmail",
        display_name="Google Gmail",
        publisher="Google",
        description="Read and manage Gmail.",
        remote_url="https://mcp.gmail.example/mcp",
        documentation_url="https://developers.google.com/workspace",
        author_website_url="https://workspace.google.com/",
        support_url="https://support.google.com/",
        privacy_policy_url="https://policies.google.com/privacy",
        categories=("productivity",),
        supported_products=("Gmail",),
        requested_scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
        tool_preview=(
            CuratedMCPTool(
                name="search_messages",
                description="Search Gmail messages.",
                category="search",
                risk_labels=(),
            ),
        ),
        availability="remote",
        popularity_rank=1,
        trusted_logo_key="google-gmail",
        connection_ready=False,
    )
    MCPCatalogService(db_session).sync_official_providers(providers=(provider,))
    db_session.commit()


def test_curated_provider_is_visible_but_requires_provider_oauth_configuration(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    # The endpoint check resolves the provider's host, which the isolated
    # test environment cannot reach.
    monkeypatch.setattr(
        "app.integrations.services.mcp_endpoint_security.resolve_public_addresses",
        lambda _host, _port: ("8.8.8.8",),
    )
    db_session.execute(
        delete(MCPRegistryEntry).where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
    )
    _seed_marketplace_provider(db_session)
    seeded = seed_user(
        "tenant-mcp-curated-catalog",
        "mcp-curated-catalog@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    try:
        response = client.get("/api/v1/mcp/marketplace?page=1", headers=_auth_headers(seeded))

        assert response.status_code == 200
        gmail = next(
            item for item in response.json()["items"] if item["provider_slug"] == "google-gmail"
        )
        assert gmail["official"] is True
        # A remote provider is connectable, and the connection is what
        # reports the missing OAuth client configuration.
        assert gmail["connectable"] is True
        assert gmail["requested_scopes"] == [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
        assert gmail["tools"] == gmail["tool_preview"]
        assert "client_secret" not in str(gmail["oauth_requirements"])
        assert "client_secret" not in str(gmail["package_metadata"])
        assert gmail["health"] == {
            "status": "not_checked",
            "last_checked_at": None,
            "detail": "Live health is checked only after user authentication.",
        }

        connect_response = client.post(
            f"/api/v1/mcp/marketplace/{gmail['id']}/connect",
            headers=_auth_headers(seeded),
        )

        # The provider needs an OAuth client the deployment has not
        # configured, so the connection is created and left waiting for the
        # account to authorize it.
        assert connect_response.status_code == 201
        assert connect_response.json()["setup_required"] is True
    finally:
        db_session.execute(
            delete(MCPRegistryEntry).where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
        )
        db_session.commit()


def test_marketplace_connect_rebinds_tenant_context_after_oauth_commit(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    db_session: Session,
    monkeypatch,
) -> None:
    """OAuth setup commits internally; the response must still serialize under RLS."""
    seeded = seed_user(
        "tenant-mcp-connect-context",
        "mcp-connect-context@example.com",
        "StrongPass!1234",
        ("admin",),
    )
    db_session.execute(
        delete(MCPRegistryEntry).where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
    )
    _seed_marketplace_provider(db_session)
    settings = get_settings()
    settings.mcp_google_oauth_client_id = "mcp-google-client-id"
    settings.mcp_google_oauth_client_secret = "mcp-google-client-secret"
    settings.mcp_oauth_redirect_uri = "https://averqel.example/api/v1/mcp/oauth/callback"

    def fake_start(self, *, server, user_id):
        self.db.commit()
        return "https://accounts.google.com/o/oauth2/v2/auth?state=test"

    monkeypatch.setattr(MCPServerOAuthService, "start", fake_start)
    monkeypatch.setattr(
        "app.integrations.services.mcp_endpoint_security.resolve_public_addresses",
        lambda _host, _port: ("8.8.8.8",),
    )

    entry = db_session.execute(
        select(MCPRegistryEntry).where(MCPRegistryEntry.provider_slug == "google-gmail")
    ).scalar_one()
    response = client.post(
        f"/api/v1/mcp/marketplace/{entry.id}/connect",
        headers=_auth_headers(seeded),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["authorization_url"].startswith("https://accounts.google.com/")
    assert payload["server"]["provider_slug"] == "google-gmail"
