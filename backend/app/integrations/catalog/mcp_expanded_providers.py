"""Expanded MCP catalog entries for AverQel Harness.

Provides curated metadata for:
1. Local STDIO MCP tools (Filesystem, Git, SQLite, Puppeteer, Memory, Fetch).
2. Composio-brokered cloud apps. Their metadata is read from the Composio
   toolkit list when an API key is configured, because that list is what the
   broker can actually authorize; the bundled SDK enum list only keeps the
   marketplace populated when Composio is unreachable.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.integrations.catalog.mcp_official_providers import (
    OFFICIAL_MCP_PROVIDERS,
    CuratedMCPTool,
)

logger = logging.getLogger(__name__)

EXPANDED_MCP_CATALOG_SOURCE = "averqel-curated-tools-v1"
CATALOG_EXPANDED_REVIEWED_AT = datetime(2026, 10, 1, tzinfo=UTC)
# Composio hosts one MCP endpoint per user session, so a catalog entry records
# the gateway rather than a per-user URL. Connections resolve their real
# endpoint through the Composio broker.
COMPOSIO_MCP_GATEWAY_URL = "https://backend.composio.dev/tool_router/mcp"
_COMPOSIO_CATALOG_PAGE_LIMIT = 100
_COMPOSIO_CATALOG_MAX_PAGES = 40
_COMPOSIO_CATEGORY_LABELS = {
    "email": "Email",
    "productivity": "Productivity",
    "developer": "Developer",
    "crm": "CRM",
    "social": "Social",
    "communication": "Communication",
    "files": "Files",
    "analytics": "Analytics",
    "marketing": "Marketing",
    "finance": "Finance",
    "project_management": "Project Management",
    "hr": "HR",
    "support": "Support",
    "ecommerce": "Ecommerce",
    "design": "Design",
}


@dataclass(frozen=True, slots=True)
class ExpandedMCPProvider:
    """An expanded MCP provider supporting stdio or remote transport."""

    provider_slug: str
    display_name: str
    publisher: str
    description: str
    transport: str  # "stdio" or "streamable_http"
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
    command: str | None = None
    args: tuple[str, ...] = ()
    connection_ready: bool = True
    connection_readiness_reason: str | None = None
    # Composio-brokered apps: the toolkit Composio authorizes, whether Composio
    # publishes it without an auth scheme, and the toolkit's own tool count.
    composio_toolkit: str | None = None
    composio_no_auth: bool = False
    tool_count: int | None = None

    @property
    def auth_type(self) -> str:
        """Return the marketplace auth type for this provider's connection."""
        if self.transport == "stdio":
            return "anonymous"
        if self.composio_toolkit:
            return "anonymous" if self.composio_no_auth else "oauth"
        return "oauth"

    def registry_values(self) -> dict[str, Any]:
        badges = {
            "official": True,
            "community": False,
            "new": True,
            "trending": True,
            "interactive": self.transport == "stdio",
            "developer_preview": self.availability == "developer_preview",
        }
        risk_policy = {
            "default_mode": "needs_approval",
            "read": {"default_mode": "always_allow"},
            "write": {"default_mode": "needs_approval"},
            "delete": {"default_mode": "needs_approval"},
            "external_message": {"default_mode": "needs_approval"},
        }
        tools = [tool.as_dict() for tool in self.tool_preview]
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
            "reviewed_at": CATALOG_EXPANDED_REVIEWED_AT.isoformat(),
            "review_due_at": CATALOG_EXPANDED_REVIEWED_AT.isoformat(),
            "trusted_logo_key": self.trusted_logo_key,
            "supported_products": list(self.supported_products),
            "tool_categories": sorted({tool.category for tool in self.tool_preview}),
            "tools": tools,
            "tool_preview": tools,
            "risk_policy": risk_policy,
            "health": {
                "status": "healthy" if self.transport == "stdio" else "not_checked",
                "last_checked_at": None,
                "detail": (
                    "Local stdio tool ready." if self.transport == "stdio" else "Ready to connect."
                ),
            },
            "connection_ready": True,
            "connection_readiness_reason": None,
            "command": self.command,
            "args": list(self.args),
        }

        return {
            "source": EXPANDED_MCP_CATALOG_SOURCE,
            "server_name": self.provider_slug,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "provider_slug": self.provider_slug,
            "publisher_type": "official",
            "version": "1.0.0",
            "description": self.description,
            "transport": self.transport,
            "remote_url": self.remote_url,
            "documentation_url": self.documentation_url,
            "health_status": "healthy" if self.transport == "stdio" else "not_checked",
            "health_checked_at": None,
            "requested_scopes": list(self.requested_scopes),
            "supported_products": list(self.supported_products),
            "risk_policy": risk_policy,
            "oauth_profile": {
                "status": "discovery_ready",
                "provider_slug": self.provider_slug,
            },
            "author_website_url": self.author_website_url,
            "support_url": self.support_url,
            "privacy_policy_url": self.privacy_policy_url,
            "catalog_badges": badges,
            "trusted_logo_key": self.trusted_logo_key,
            "tool_categories": catalog_metadata["tool_categories"],
            "tool_risk_summary": {
                label: sum(label in tool.risk_labels for tool in self.tool_preview)
                for label in ("read", "write", "delete", "external_message")
            },
            "package_metadata": {
                "provider_slug": self.provider_slug,
                "auth_type": self.auth_type,
                "tools": tools,
                "tool_preview": tools,
                "tool_categories": catalog_metadata["tool_categories"],
                "supported_products": list(self.supported_products),
                "risk_policy": risk_policy,
                "trusted_logo_key": self.trusted_logo_key,
                **(
                    {
                        "composio_toolkit": self.composio_toolkit,
                        "composio_no_auth": self.composio_no_auth,
                    }
                    if self.composio_toolkit
                    else {}
                ),
            },
            "oauth_requirements": {
                "type": self.auth_type,
                "requested_scopes": list(self.requested_scopes),
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
                    "command": self.command,
                    "args": list(self.args),
                },
                "catalog": catalog_metadata,
            },
            "logo_url": None,
            "tool_count": self.tool_count if self.tool_count is not None else len(tools),
            "verification_reason": "Verified AverQel Harness MCP Tool.",
            "trust_status": "approved",
            "verification_source": self.documentation_url,
            "verified_at": CATALOG_EXPANDED_REVIEWED_AT,
            "popularity_rank": self.popularity_rank,
            "catalog_status": "oauth_discovery_ready",
            "enrichment_error": None,
        }


_MANUAL_EXPANDED_PROVIDERS: tuple[ExpandedMCPProvider, ...] = (
    # -------------------------------------------------------------------------
    # 1. LOCAL STDIO MCP TOOLS (Zero Auth, Completely Private & Local)
    # -------------------------------------------------------------------------
    ExpandedMCPProvider(
        provider_slug="local-filesystem",
        display_name="Local Filesystem",
        publisher="Model Context Protocol",
        description="Read, write, search, and manage local files and directories directly on your computer.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/filesystem",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Files", "System", "Local"),
        supported_products=("Local OS", "Filesystem"),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool("read_file", "Read complete contents of a file.", "Files", ("read",)),
            CuratedMCPTool("write_file", "Create or overwrite file contents.", "Files", ("write",)),
            CuratedMCPTool(
                "list_directory", "List files and directories in a path.", "Files", ("read",)
            ),
            CuratedMCPTool(
                "search_files", "Recursively search files matching pattern.", "Files", ("read",)
            ),
        ),
        availability="general",
        popularity_rank=10,
        trusted_logo_key="local",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "/home/ravi"),
    ),
    ExpandedMCPProvider(
        provider_slug="local-git",
        display_name="Local Git Repositories",
        publisher="Model Context Protocol",
        description="Inspect repositories, commit history, diffs, branches, and staged files on your machine.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/git",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Developer", "System", "Local"),
        supported_products=("Git", "Source Control"),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool("git_status", "Show repository working tree status.", "Git", ("read",)),
            CuratedMCPTool(
                "git_diff", "Show changes between commits or working tree.", "Git", ("read",)
            ),
            CuratedMCPTool("git_log", "Show recent commit logs.", "Git", ("read",)),
            CuratedMCPTool("git_commit", "Record changes to the repository.", "Git", ("write",)),
        ),
        availability="general",
        popularity_rank=11,
        trusted_logo_key="git",
        command="npx",
        args=("-y", "@cyanheads/git-mcp-server"),
    ),
    ExpandedMCPProvider(
        provider_slug="local-sqlite",
        display_name="Local SQLite Databases",
        publisher="Model Context Protocol",
        description="Execute queries, inspect schemas, and manage local SQLite databases.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/sqlite",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Data", "Databases", "Local"),
        supported_products=("SQLite",),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool(
                "read_query",
                "Execute SELECT queries on local SQLite database.",
                "Database",
                ("read",),
            ),
            CuratedMCPTool(
                "write_query",
                "Execute INSERT/UPDATE queries on local database.",
                "Database",
                ("write",),
            ),
            CuratedMCPTool(
                "describe_table", "Show schema for a SQLite table.", "Database", ("read",)
            ),
        ),
        availability="general",
        popularity_rank=12,
        trusted_logo_key="sqlite",
        command="mcp-server-sqlite",
        args=("--db-path", "/home/ravi/.averqel/db/knowledge.db"),
    ),
    ExpandedMCPProvider(
        provider_slug="local-puppeteer",
        display_name="Web Browser (Puppeteer)",
        publisher="Model Context Protocol",
        description="Automate web browsing, take screenshots, navigate pages, and interact with web applications.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/puppeteer",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Web", "Automation", "Local"),
        supported_products=("Puppeteer", "Chrome"),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool("puppeteer_navigate", "Navigate browser to URL.", "Browser", ("read",)),
            CuratedMCPTool(
                "puppeteer_screenshot", "Capture webpage screenshot.", "Browser", ("read",)
            ),
            CuratedMCPTool("puppeteer_click", "Click element on webpage.", "Browser", ("write",)),
            CuratedMCPTool(
                "puppeteer_fill", "Fill out form input on webpage.", "Browser", ("write",)
            ),
        ),
        availability="general",
        popularity_rank=13,
        trusted_logo_key="puppeteer",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-puppeteer"),
    ),
    ExpandedMCPProvider(
        provider_slug="local-memory",
        display_name="Knowledge Graph Memory",
        publisher="Model Context Protocol",
        description="Persistent knowledge graph memory system for storing entities and relationships.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/memory",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Memory", "Productivity", "Local"),
        supported_products=("Knowledge Graph",),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool(
                "create_entities", "Create new entities in knowledge graph.", "Memory", ("write",)
            ),
            CuratedMCPTool(
                "create_relations", "Create relations between entities.", "Memory", ("write",)
            ),
            CuratedMCPTool("open_nodes", "Read nodes from knowledge graph.", "Memory", ("read",)),
            CuratedMCPTool("search_nodes", "Search nodes by query.", "Memory", ("read",)),
        ),
        availability="general",
        popularity_rank=14,
        trusted_logo_key="local",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
    ),
    ExpandedMCPProvider(
        provider_slug="local-fetch",
        display_name="Web Page Fetcher",
        publisher="Model Context Protocol",
        description="Fetch any public URL and convert HTML directly into clean Markdown.",
        transport="stdio",
        remote_url="https://modelcontextprotocol.io/mcp/fetch",
        documentation_url="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        author_website_url="https://modelcontextprotocol.io",
        support_url="https://github.com/modelcontextprotocol/servers/issues",
        privacy_policy_url="https://modelcontextprotocol.io/privacy",
        categories=("Web", "Research", "Local"),
        supported_products=("Web",),
        requested_scopes=(),
        tool_preview=(
            CuratedMCPTool(
                "fetch", "Fetch webpage contents and convert to markdown.", "Web", ("read",)
            ),
        ),
        availability="general",
        popularity_rank=15,
        trusted_logo_key="local",
        command="mcp-server-fetch",
        args=(),
    ),
)


def _format_composio_app_name(raw: str) -> str:
    special = {
        "GITHUB": "GitHub",
        "GITLAB": "GitLab",
        "BITBUCKET": "Bitbucket",
        "HUBSPOT": "HubSpot",
        "SALESFORCE": "Salesforce",
        "AIRTABLE": "Airtable",
        "CLICKUP": "ClickUp",
        "MAILCHIMP": "Mailchimp",
        "SENDGRID": "SendGrid",
        "PAGERDUTY": "PagerDuty",
        "DATADOG": "Datadog",
        "POSTMAN": "Postman",
        "SUPABASE": "Supabase",
        "MONGODB": "MongoDB",
        "SHOPIFY": "Shopify",
        "LINKEDIN": "LinkedIn",
        "YOUTUBE": "YouTube",
        "WHATSAPP": "WhatsApp",
        "WORDPRESS": "WordPress",
        "QUICKBOOKS": "QuickBooks",
        "DOCUSIGN": "DocuSign",
        "EVENTBRITE": "Eventbrite",
        "INTERCOM": "Intercom",
        "TYPEFORM": "Typeform",
        "CALENDLY": "Calendly",
        "SURVEYMONKEY": "SurveyMonkey",
        "ZENDESK": "Zendesk",
        "ASANA": "Asana",
        "TRELLO": "Trello",
        "NOTION": "Notion",
        "SLACK": "Slack",
        "DISCORD": "Discord",
        "TWITTER": "X (Twitter)",
        "FIGMA": "Figma",
        "CANVA": "Canva",
        "STRIPE": "Stripe",
        "PAYPAL": "PayPal",
        "ZOOM": "Zoom",
        "LINEAR": "Linear",
        "JIRA": "Jira",
        "REDDIT": "Reddit",
        "BOX": "Box",
        "DROPBOX": "Dropbox",
        "EVERNOTE": "Evernote",
        "TODOIST": "Todoist",
        "BITWARDEN": "Bitwarden",
        "AUTH0": "Auth0",
        "AWS": "AWS",
        "GCP": "Google Cloud",
    }
    if raw in special:
        return special[raw]
    parts = raw.split("_")
    capitalized = [special.get(p, p.capitalize()) for p in parts]
    return " ".join(capitalized)


def _get_composio_app_categories(raw_name: str) -> tuple[str, ...]:
    n = raw_name.upper()
    cats = []
    if any(
        k in n
        for k in (
            "GIT",
            "SENTRY",
            "POSTMAN",
            "DOCKER",
            "KUBE",
            "AWS",
            "GCP",
            "AZURE",
            "VERCEL",
            "NETLIFY",
            "HEROKU",
            "CLOUDFLARE",
            "DATADOG",
            "GRAFANA",
            "NEWRELIC",
            "CIRCLECI",
            "JENKINS",
            "BASH",
            "CLI",
            "CODE",
            "SQL",
            "POSTGRES",
            "SUPABASE",
            "MONGO",
            "REDIS",
            "PRISMA",
        )
    ):
        cats.extend(["Development", "Developer"])
    if any(
        k in n
        for k in (
            "SLACK",
            "DISCORD",
            "ZOOM",
            "TEAMS",
            "TELEGRAM",
            "WHATSAPP",
            "TWILIO",
            "CHAT",
            "MEET",
            "DIALPAD",
            "RINGCENTRAL",
            "MESSENGER",
            "VOICE",
            "MESSAGE",
        )
    ):
        cats.extend(["Communication", "Productivity"])
    if any(
        k in n
        for k in (
            "TWITTER",
            "REDDIT",
            "LINKEDIN",
            "FACEBOOK",
            "INSTAGRAM",
            "TIKTOK",
            "YOUTUBE",
            "MEDIUM",
            "BLOG",
            "SOCIAL",
        )
    ):
        cats.extend(["Social", "Marketing"])
    if any(
        k in n
        for k in (
            "SALES",
            "CRM",
            "HUBSPOT",
            "PIPEDRIVE",
            "ZOHO",
            "ZENDESK",
            "FRESHDESK",
            "INTERCOM",
            "FRONT",
            "APOLLO",
            "CLOSE",
            "LEAD",
            "CONTACT",
        )
    ):
        cats.extend(["CRM", "Sales"])
    if any(
        k in n
        for k in (
            "NOTION",
            "AIRTABLE",
            "ASANA",
            "TRELLO",
            "CLICKUP",
            "LINEAR",
            "JIRA",
            "MONDAY",
            "TODO",
            "TASK",
            "DOC",
            "SHEET",
            "SLIDE",
            "NOTE",
            "CALENDAR",
            "CALENDLY",
            "CODA",
            "BASECAMP",
            "EVERNOTE",
        )
    ):
        cats.extend(["Productivity", "Management"])
    if any(
        k in n
        for k in ("DRIVE", "DROPBOX", "BOX", "FILE", "STORAGE", "S3", "ONEDRIVE", "SHAREPOINT")
    ):
        cats.extend(["Files", "Storage"])
    if any(
        k in n
        for k in (
            "STRIPE",
            "PAYPAL",
            "SHOPIFY",
            "SQUARE",
            "BILL",
            "INVOICE",
            "QUICKBOOKS",
            "XERO",
            "COMMERCE",
            "FINANCE",
            "BANK",
            "PAY",
            "TAX",
            "REVENUE",
        )
    ):
        cats.extend(["Finance", "Commerce"])
    if any(
        k in n
        for k in (
            "MAIL",
            "SENDGRID",
            "MARKET",
            "CAMPAIGN",
            "BREVO",
            "KLAVIYO",
            "NEWSLETTER",
            "SEO",
            "AHREFS",
            "SEMRUSH",
        )
    ):
        cats.extend(["Marketing", "Communication"])
    if any(
        k in n
        for k in ("FIGMA", "CANVA", "ADOBE", "DESIGN", "PHOTO", "VIDEO", "IMAGE", "MEDIA", "BANNER")
    ):
        cats.extend(["Design", "Creative"])
    if any(
        k in n
        for k in ("AI", "OPENAI", "ANTHROPIC", "COHERE", "PERPLEXITY", "SEARCH", "EMBED", "VECTOR")
    ):
        cats.extend(["AI", "Intelligence"])
    if not cats:
        cats = ["Productivity", "Cloud"]
    seen = set()
    res = []
    for c in cats:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return tuple(res)


def _load_composio_sdk_providers() -> tuple[ExpandedMCPProvider, ...]:
    """Dynamically load 250+ Composio SDK connectors and tools from local enums metadata."""
    try:
        import composio

        enums_dir = Path(composio.__file__).parent / "client" / "enums"
        app_file = enums_dir / "app.pyi"
        action_file = enums_dir / "action.pyi"
        if not app_file.exists():
            return ()
        app_lines = app_file.read_text(encoding="utf-8").splitlines()
        apps = [
            m.group(1)
            for line in app_lines
            if (m := re.match(r"^\s+([A-Z0-9_]+):\s*\"App\"", line))
        ]
    except Exception:
        return ()

    app_actions: dict[str, list[str]] = defaultdict(list)
    try:
        if action_file.exists():
            action_lines = action_file.read_text(encoding="utf-8").splitlines()
            actions = [
                m.group(1)
                for line in action_lines
                if (m := re.match(r"^\s+([A-Z0-9_]+):\s*\"Action\"", line))
            ]
            apps_by_len = sorted(apps, key=lambda a: -len(a))
            for act in actions:
                for a in apps_by_len:
                    if act.startswith(a + "_"):
                        app_actions[a].append(act[len(a) + 1 :])
                        break
    except Exception:
        pass

    existing_official_slugs = {p.provider_slug for p in OFFICIAL_MCP_PROVIDERS}
    existing_manual_slugs = {p.provider_slug for p in _MANUAL_EXPANDED_PROVIDERS}
    reserved_slugs = existing_official_slugs | existing_manual_slugs

    providers: list[ExpandedMCPProvider] = []
    for i, raw_app in enumerate(apps):
        # Composio toolkit slugs are lowercase and keep underscores
        # (``active_campaign``), so the enum name is only lowercased.
        slug = raw_app.lower()
        if slug in reserved_slugs:
            continue

        display_name = _format_composio_app_name(raw_app)
        categories = _get_composio_app_categories(raw_app)
        raw_actions = app_actions.get(raw_app, [])

        # Composio publishes the tool list only per connection, so the catalog
        # records the action count and leaves discovery to the connected app.
        desc = (
            f"Integrate {display_name} with {len(raw_actions)}+ automated actions "
            f"for {categories[0].lower()} workflows."
            if raw_actions
            else f"Connect {display_name} for automated {categories[0].lower()} operations."
        )

        providers.append(
            ExpandedMCPProvider(
                provider_slug=slug,
                display_name=display_name,
                publisher="Composio",
                description=desc,
                transport="streamable_http",
                remote_url=COMPOSIO_MCP_GATEWAY_URL,
                documentation_url=f"https://docs.composio.dev/tools/{slug}",
                author_website_url=f"https://composio.dev/app/{slug}",
                support_url="https://composio.dev/support",
                privacy_policy_url="https://composio.dev/privacy",
                categories=categories,
                supported_products=(display_name,),
                requested_scopes=("api",),
                tool_preview=(),
                availability="general",
                popularity_rank=30 + i,
                trusted_logo_key=slug,
                composio_toolkit=slug,
                tool_count=len(raw_actions) or None,
            )
        )

    return tuple(providers)


EXPANDED_MCP_PROVIDERS: tuple[ExpandedMCPProvider, ...] = (
    _MANUAL_EXPANDED_PROVIDERS + _load_composio_sdk_providers()
)


def _composio_display_name(name: str, slug: str) -> str:
    """Return a display name for a live Composio toolkit."""
    value = str(name or "").strip()
    return value or _format_composio_app_name(slug.upper())


def _composio_categories(meta: dict[str, Any]) -> tuple[str, ...]:
    """Return readable category labels for a live Composio toolkit."""
    labels: list[str] = []
    raw = meta.get("categories")
    if isinstance(raw, list):
        for entry in raw:
            name = entry.get("name") if isinstance(entry, dict) else entry
            value = str(name or "").strip()
            if not value:
                continue
            label = _COMPOSIO_CATEGORY_LABELS.get(value.lower(), value.title())
            if label not in labels:
                labels.append(label)
    if not labels:
        labels = ["Productivity", "Cloud"]
    return tuple(labels)


def _load_live_composio_providers() -> tuple[tuple[ExpandedMCPProvider, ...], bool]:
    """Read the deployment's real Composio toolkits from the Composio API.

    Returns the entries and whether the live API answered. A ``False`` flag
    means the caller must fall back to the bundled list instead of treating an
    empty result as "Composio publishes nothing".
    """
    from app.core.config import get_settings
    from app.integrations.services.composio_service import composio_client_or_none

    client = composio_client_or_none(get_settings())
    if client is None:
        return (), False
    items: list[Any] = []
    try:
        cursor: str | None = None
        for _ in range(_COMPOSIO_CATALOG_MAX_PAGES):
            page = client.toolkits.list(limit=_COMPOSIO_CATALOG_PAGE_LIMIT, cursor=cursor)
            items.extend(list(getattr(page, "items", []) or []))
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
    except Exception:  # noqa: BLE001
        logger.warning(
            "Composio toolkit catalog could not be loaded; using the bundled fallback list"
        )
        return (), False
    if not items:
        return (), False

    reserved_slugs = {p.provider_slug for p in _MANUAL_EXPANDED_PROVIDERS}
    providers: list[ExpandedMCPProvider] = []
    for index, item in enumerate(items):
        slug = str(getattr(item, "slug", "") or "").strip().lower()
        if not slug or slug in reserved_slugs:
            continue
        meta = getattr(item, "meta", None)
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump()
        if not isinstance(meta, dict):
            meta = {}
        display_name = _composio_display_name(str(getattr(item, "name", "") or ""), slug)
        categories = _composio_categories(meta)
        description = str(meta.get("description") or "").strip() or (
            f"Connect {display_name} to AverQel through Composio."
        )
        tools_count = int(meta.get("tools_count") or 0)
        providers.append(
            ExpandedMCPProvider(
                provider_slug=slug,
                display_name=display_name,
                publisher="Composio",
                description=description,
                transport="streamable_http",
                remote_url=COMPOSIO_MCP_GATEWAY_URL,
                documentation_url=f"https://docs.composio.dev/tools/{slug}",
                author_website_url=str(meta.get("app_url") or "") or "https://composio.dev",
                support_url="https://composio.dev/support",
                privacy_policy_url="https://composio.dev/privacy",
                categories=categories,
                supported_products=(display_name,),
                requested_scopes=("api",),
                tool_preview=(),
                availability="general",
                popularity_rank=30 + index,
                trusted_logo_key=slug,
                composio_toolkit=slug,
                composio_no_auth=bool(getattr(item, "no_auth", False)),
                tool_count=tools_count or None,
            )
        )

    return tuple(providers), True


def composio_catalog_providers() -> tuple[ExpandedMCPProvider, ...]:
    """Return the Composio apps this deployment can connect.

    The live toolkit list is authoritative because it is exactly what the
    broker can authorize. The bundled SDK list keeps the marketplace populated
    when Composio is unreachable.
    """
    live, _ = _load_live_composio_providers()
    return live or _load_composio_sdk_providers()


def expanded_catalog_providers() -> tuple[tuple[ExpandedMCPProvider, ...], bool]:
    """Return the full expanded catalog and whether its Composio part is live.

    The boolean lets seeding prune entries Composio no longer publishes without
    deleting the catalog when Composio was simply unreachable.
    """
    live, is_live = _load_live_composio_providers()
    return _MANUAL_EXPANDED_PROVIDERS + (live or _load_composio_sdk_providers()), is_live
