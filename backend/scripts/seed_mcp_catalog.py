"""Idempotently seed AverQel's reviewed official remote MCP catalog."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from app.auth.models.tenant import Tenant  # noqa: F401
from app.auth.models.user import User  # noqa: F401
from app.integrations.services.mcp_catalog_service import MCPCatalogService
from app.platform.database.session import get_session_factory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def seed_mcp_catalog() -> dict[str, int]:
    """Persist only public, code-reviewed marketplace metadata."""
    from app.integrations.catalog.mcp_expanded_providers import expanded_catalog_providers

    providers, composio_is_live = expanded_catalog_providers()
    with get_session_factory()() as session:
        svc = MCPCatalogService(session)
        result = svc.sync_official_providers()
        retired = svc.prune_retired_official_entries()
        svc.sync_expanded_providers(providers, prune=composio_is_live)
        session.commit()
    payload = result.as_dict()
    payload["retired_official"] = retired
    payload["composio_catalog"] = "live" if composio_is_live else "bundled"
    logger.info("Curated MCP catalog synchronization complete: %s", payload)
    return payload


if __name__ == "__main__":
    seed_mcp_catalog()
