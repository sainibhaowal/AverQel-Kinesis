from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.integrations.catalog.mcp_official_providers import CURATED_MCP_CATALOG_SOURCE
from app.integrations.models.mcp_server import MCPRegistryEntry
from app.integrations.services.mcp_catalog_service import MCPCatalogService


@pytest.fixture
def clean_curated_mcp_catalog(db_session: Session) -> Iterator[Session]:
    db_session.execute(
        delete(MCPRegistryEntry).where(
            MCPRegistryEntry.source.in_((CURATED_MCP_CATALOG_SOURCE, "external-test-catalog"))
        )
    )
    db_session.commit()
    try:
        yield db_session
    finally:
        db_session.execute(
            delete(MCPRegistryEntry).where(
                MCPRegistryEntry.source.in_((CURATED_MCP_CATALOG_SOURCE, "external-test-catalog"))
            )
        )
        db_session.commit()


def test_sync_official_mcp_catalog_creates_no_entries(
    clean_curated_mcp_catalog: Session,
) -> None:
    """The official catalog is empty, because Composio brokers the apps.

    AverQel no longer ships its own OAuth provider profiles. Connectable
    remote apps are brokered through Composio, so the curated official
    catalog holds no provider rows.
    """
    result = MCPCatalogService(clean_curated_mcp_catalog).sync_official_providers()
    clean_curated_mcp_catalog.commit()

    rows = (
        clean_curated_mcp_catalog.execute(
            select(MCPRegistryEntry)
            .where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
            .order_by(MCPRegistryEntry.popularity_rank)
        )
        .scalars()
        .all()
    )

    assert result.as_dict() == {"created": 0, "updated": 0, "unchanged": 0, "total": 0}
    assert rows == []


def test_sync_official_mcp_catalog_is_idempotent_and_preserves_other_sources(
    clean_curated_mcp_catalog: Session,
) -> None:
    session = clean_curated_mcp_catalog
    first_result = MCPCatalogService(session).sync_official_providers()
    session.commit()
    original_ids = {
        row.server_name: row.id
        for row in session.execute(
            select(MCPRegistryEntry).where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
        )
        .scalars()
        .all()
    }
    third_party = MCPRegistryEntry(
        source="external-test-catalog",
        server_name="google-gmail",
        display_name="External Google Gmail",
        provider_slug="external-google-gmail",
        publisher_type="community",
        transport="streamable_http",
        remote_url="https://example.com/mcp",
    )
    session.add(third_party)
    session.commit()

    second_result = MCPCatalogService(session).sync_official_providers()
    session.commit()
    current_ids = {
        row.server_name: row.id
        for row in session.execute(
            select(MCPRegistryEntry).where(MCPRegistryEntry.source == CURATED_MCP_CATALOG_SOURCE)
        )
        .scalars()
        .all()
    }

    assert first_result.created == 0
    assert second_result.as_dict() == {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "total": 0,
    }
    assert current_ids == original_ids
    assert session.get(MCPRegistryEntry, third_party.id) is not None
