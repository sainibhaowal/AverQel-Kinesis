"""Code-reviewed metadata for AverQel's curated MCP marketplace catalog.

This module is deliberately data-only. It contains public provider metadata,
never OAuth client credentials, user tokens, HTTP headers, or live catalog
responses. Live tools are rediscovered only after a user authenticates.

``OFFICIAL_MCP_PROVIDERS`` is empty: AverQel brokers connectable remote apps
through Composio instead of shipping its own OAuth provider profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

CURATED_MCP_CATALOG_SOURCE = "averqel-curated-official-v1"
CATALOG_REVIEWED_AT = datetime(2026, 7, 23, tzinfo=UTC)
CATALOG_REVIEW_DUE_AT = datetime(2026, 10, 21, tzinfo=UTC)

RiskLabel = Literal["read", "write", "delete", "external_message"]


@dataclass(frozen=True, slots=True)
class CuratedMCPTool:
    """A reviewed catalog tool safe for marketplace display."""

    name: str
    description: str
    category: str
    risk_labels: tuple[RiskLabel, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk_labels": list(self.risk_labels),
        }


@dataclass(frozen=True, slots=True)
class CuratedMCPProvider:
    """One reviewed official remote MCP provider entry."""

    provider_slug: str
    display_name: str
    publisher: str
    description: str
    remote_url: str
    documentation_url: str
    author_website_url: str
    support_url: str
    privacy_policy_url: str
    categories: tuple[str, ...]
    supported_products: tuple[str, ...]
    requested_scopes: tuple[str, ...]
    tool_preview: tuple[CuratedMCPTool, ...]
    availability: str
    popularity_rank: int
    trusted_logo_key: str
    scope_mode: str = "explicit"
    scope_note: str | None = None
    catalog_status: str | None = None
    connection_ready: bool = False
    connection_readiness_reason: str | None = None

    @property
    def transport(self) -> str:
        return "streamable_http"

    @property
    def tools(self) -> tuple[CuratedMCPTool, ...]:
        """The complete reviewed tool set currently published by AverQel.

        ``tool_preview`` remains the compatibility name used by existing
        marketplace cards. Both values come from the same reviewed source;
        the detail API exposes ``tools`` explicitly so it is not mistaken for
        a truncated live-server response.
        """
        return self.tool_preview

    def registry_values(self) -> dict[str, Any]:
        """Return safe values for ``MCPRegistryEntry`` fields.

        Static-profile providers remain unavailable until their server-side
        OAuth credentials are configured. Providers that publish a reviewed
        MCP OAuth discovery flow can opt into the generic broker safely.
        """
        badges = {
            "official": True,
            "community": False,
            "new": False,
            "trending": False,
            "interactive": False,
            "developer_preview": self.availability == "developer_preview",
        }
        risk_policy = {
            "default_mode": "needs_approval",
            "read": {"default_mode": "always_allow"},
            "write": {"default_mode": "needs_approval"},
            "delete": {"default_mode": "needs_approval"},
            "external_message": {"default_mode": "needs_approval"},
        }
        tools = [tool.as_dict() for tool in self.tools]
        tool_preview = tools
        tool_risk_summary = {
            label: sum(label in tool.risk_labels for tool in self.tool_preview)
            for label in ("read", "write", "delete", "external_message")
        }
        catalog_metadata = {
            "schema_version": 1,
            "provider_slug": self.provider_slug,
            "publisher_type": "official",
            "author_name": self.publisher,
            "author_website_url": self.author_website_url,
            "documentation_url": self.documentation_url,
            "support_url": self.support_url,
            "privacy_policy_url": self.privacy_policy_url,
            "badges": badges,
            "availability": self.availability,
            "reviewed_at": CATALOG_REVIEWED_AT.isoformat(),
            "review_due_at": CATALOG_REVIEW_DUE_AT.isoformat(),
            "trusted_logo_key": self.trusted_logo_key,
            "supported_products": list(self.supported_products),
            "tool_categories": sorted({tool.category for tool in self.tool_preview}),
            "tools": tools,
            "tool_preview": tool_preview,
            "risk_policy": risk_policy,
            "health": {
                "status": "not_checked",
                "last_checked_at": None,
                "detail": "Live health is checked only after user authentication.",
            },
            "connection_ready": self.connection_ready,
            "connection_readiness_reason": self.connection_readiness_reason
            or (None if self.connection_ready else "OAuth provider profile is not configured yet."),
        }
        if self.scope_note:
            catalog_metadata["scope_note"] = self.scope_note

        return {
            "source": CURATED_MCP_CATALOG_SOURCE,
            "server_name": self.provider_slug,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "provider_slug": self.provider_slug,
            "publisher_type": "official",
            "version": None,
            "description": self.description,
            "transport": self.transport,
            "remote_url": self.remote_url,
            "documentation_url": self.documentation_url,
            "health_status": "not_checked",
            "health_checked_at": None,
            "requested_scopes": list(self.requested_scopes),
            "supported_products": list(self.supported_products),
            "risk_policy": risk_policy,
            "oauth_profile": {
                "status": "discovery_ready" if self.connection_ready else "not_configured",
                "provider_slug": self.provider_slug,
            },
            "author_website_url": self.author_website_url,
            "support_url": self.support_url,
            "privacy_policy_url": self.privacy_policy_url,
            "catalog_badges": badges,
            "trusted_logo_key": self.trusted_logo_key,
            "tool_categories": catalog_metadata["tool_categories"],
            "tool_risk_summary": tool_risk_summary,
            "package_metadata": {
                "provider_slug": self.provider_slug,
                "auth_type": "oauth",
                "tools": tools,
                "tool_preview": tool_preview,
                "tool_categories": catalog_metadata["tool_categories"],
                "supported_products": list(self.supported_products),
                "risk_policy": risk_policy,
                "trusted_logo_key": self.trusted_logo_key,
            },
            "oauth_requirements": {
                "type": "oauth",
                "requested_scopes": list(self.requested_scopes),
                "scope_mode": self.scope_mode,
                "scope_note": self.scope_note,
            },
            "categories": list(self.categories),
            "official": True,
            "verified": True,
            "raw_metadata": {
                "schema_version": 1,
                "server": {
                    "homepage": self.author_website_url,
                    "documentationUrl": self.documentation_url,
                    "supportUrl": self.support_url,
                    "privacyPolicyUrl": self.privacy_policy_url,
                    "remotes": [
                        {
                            "type": self.transport,
                            "url": self.remote_url,
                            "securitySchemes": {"type": "oauth2"},
                        }
                    ],
                },
                "catalog": catalog_metadata,
            },
            # Curated logos are served from local assets in Phase 6. Never
            # populate a third-party remote image URL from this catalog.
            "logo_url": None,
            "tool_count": len(tool_preview),
            "verification_reason": "Code-reviewed official remote MCP endpoint.",
            "trust_status": "approved",
            "verification_source": self.documentation_url,
            "verified_at": CATALOG_REVIEWED_AT,
            "popularity_rank": self.popularity_rank,
            "catalog_status": self.catalog_status
            or ("oauth_discovery_ready" if self.connection_ready else "oauth_profile_required"),
            "enrichment_error": None,
        }


SLACK_MCP_READ_SCOPES = (
    "search:read.public",
    "search:read.private",
    "search:read.mpim",
    "search:read.im",
    "search:read.files",
    "files:read",
    "search:read.users",
    "emoji:read",
    "channels:read",
    "groups:read",
    "mpim:read",
    "channels:history",
    "groups:history",
    "mpim:history",
    "im:history",
    "users:read",
    "users:read.email",
    "canvases:read",
)
SLACK_MCP_WRITE_SCOPES = (
    "chat:write",
    "channels:write",
    "groups:write",
    "im:write",
    "mpim:write",
    "reactions:write",
    "canvases:write",
)
SLACK_MCP_SCOPES = SLACK_MCP_READ_SCOPES + SLACK_MCP_WRITE_SCOPES


# AverQel no longer publishes its own third-party OAuth apps. Composio
# brokers every connectable remote app, so this catalog stays empty and the
# marketplace renders Composio toolkits instead.
OFFICIAL_MCP_PROVIDERS: tuple[CuratedMCPProvider, ...] = ()


def validate_official_mcp_catalog() -> None:
    """Fail fast when a future edit violates static catalog safety rules."""
    slugs: set[str] = set()
    ranks: set[int] = set()
    for provider in OFFICIAL_MCP_PROVIDERS:
        if provider.provider_slug in slugs:
            raise ValueError(f"Duplicate curated MCP provider slug: {provider.provider_slug}")
        slugs.add(provider.provider_slug)
        if provider.popularity_rank in ranks:
            raise ValueError(f"Duplicate curated MCP popularity rank: {provider.popularity_rank}")
        ranks.add(provider.popularity_rank)

        for label, value in (
            ("remote endpoint", provider.remote_url),
            ("documentation URL", provider.documentation_url),
            ("author website URL", provider.author_website_url),
            ("support URL", provider.support_url),
            ("privacy URL", provider.privacy_policy_url),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"Curated MCP {label} is not a safe HTTPS URL: {value}")
        if provider.transport != "streamable_http":
            raise ValueError(f"Unsupported curated MCP transport: {provider.transport}")
        if any(
            not scope
            or any(character.isspace() for character in scope)
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/"
                for character in scope
            )
            for scope in provider.requested_scopes
        ):
            raise ValueError(
                f"Curated MCP provider has an invalid OAuth scope: {provider.provider_slug}"
            )


validate_official_mcp_catalog()
