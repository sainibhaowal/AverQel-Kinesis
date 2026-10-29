from __future__ import annotations

import pytest

from app.integrations.catalog.mcp_official_providers import (
    CURATED_MCP_CATALOG_SOURCE,
    OFFICIAL_MCP_PROVIDERS,
    validate_official_mcp_catalog,
)

PROHIBITED_KEYS = (
    "secret",
    "token",
    "password",
    "authorization",
    "client_id",
    "client_secret",
)


@pytest.mark.unit_no_db
def test_official_catalog_publishes_no_provider_apps() -> None:
    validate_official_mcp_catalog()

    assert OFFICIAL_MCP_PROVIDERS == ()
    assert CURATED_MCP_CATALOG_SOURCE == "averqel-curated-official-v1"


@pytest.mark.unit_no_db
def test_official_catalog_metadata_carries_no_credentials() -> None:
    for provider in OFFICIAL_MCP_PROVIDERS:
        values = provider.registry_values()
        assert values["trust_status"] == "approved"
        assert all(
            forbidden not in key.lower()
            for key in values["raw_metadata"]["catalog"]
            for forbidden in PROHIBITED_KEYS
        )
