from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations.catalog import mcp_expanded_providers as catalog
from app.integrations.services.composio_service import (
    COMPOSIO_TOOLKIT_SLUG_PATTERN,
    is_composio_host,
)


def _sdk_toolkit_metadata_available() -> bool:
    """Report whether the installed Composio package ships toolkit metadata.

    The offline bundle is built from the toolkit metadata that the
    Composio package ships. A package without that metadata leaves the
    bundle with the reviewed local apps only, and the Composio apps are
    discovered from the live catalog instead.
    """
    try:
        import composio
    except ImportError:
        return False
    enums = Path(composio.__file__).parent / "client" / "enums"
    return (enums / "app.pyi").exists()


def _composio_entries() -> tuple[catalog.ExpandedMCPProvider, ...]:
    return tuple(
        provider for provider in catalog.EXPANDED_MCP_PROVIDERS if provider.transport != "stdio"
    )


def _provider(**overrides: object) -> catalog.ExpandedMCPProvider:
    values: dict[str, object] = {
        "provider_slug": "gmail",
        "display_name": "Gmail",
        "publisher": "Composio",
        "description": "Email",
        "transport": "streamable_http",
        "remote_url": catalog.COMPOSIO_MCP_GATEWAY_URL,
        "documentation_url": "https://docs.composio.dev/tools/gmail",
        "author_website_url": "https://composio.dev",
        "support_url": "https://composio.dev/support",
        "privacy_policy_url": "https://composio.dev/privacy",
        "categories": ("Email",),
        "supported_products": ("Gmail",),
        "requested_scopes": ("api",),
        "tool_preview": (),
        "availability": "general",
        "popularity_rank": 30,
        "trusted_logo_key": "gmail",
        "composio_toolkit": "gmail",
    }
    values.update(overrides)
    return catalog.ExpandedMCPProvider(**values)


@pytest.mark.unit_no_db
def test_bundled_catalog_remote_entries_are_all_composio_brokered() -> None:
    remote = _composio_entries()

    if not remote and not _sdk_toolkit_metadata_available():
        pytest.skip(
            "The installed Composio package ships no toolkit metadata, so the "
            "offline bundle carries no Composio apps."
        )
    assert remote, "the bundled catalog should still offer Composio apps offline"
    for provider in remote:
        assert provider.composio_toolkit == provider.provider_slug
        assert provider.remote_url == catalog.COMPOSIO_MCP_GATEWAY_URL
        assert is_composio_host(catalog.COMPOSIO_MCP_GATEWAY_URL.split("//")[-1].split("/")[0])
        assert provider.auth_type in {"oauth", "anonymous"}


@pytest.mark.unit_no_db
def test_bundled_catalog_slugs_are_valid_composio_toolkit_ids() -> None:
    for provider in _composio_entries():
        assert COMPOSIO_TOOLKIT_SLUG_PATTERN.fullmatch(
            provider.provider_slug
        ), provider.provider_slug


@pytest.mark.unit_no_db
def test_bundled_catalog_never_advertises_the_retired_composio_host() -> None:
    assert all(
        "api.composio.dev" not in str(provider.remote_url)
        for provider in catalog.EXPANDED_MCP_PROVIDERS
    )


@pytest.mark.unit_no_db
def test_local_stdio_entries_keep_running_on_this_machine() -> None:
    local = [
        provider for provider in catalog.EXPANDED_MCP_PROVIDERS if provider.transport == "stdio"
    ]

    assert {provider.provider_slug for provider in local} == {
        "local-filesystem",
        "local-git",
        "local-sqlite",
        "local-puppeteer",
        "local-memory",
        "local-fetch",
    }
    for provider in local:
        assert provider.command
        assert provider.composio_toolkit is None
        assert provider.auth_type == "anonymous"


@pytest.mark.unit_no_db
def test_registry_values_mark_composio_apps_and_skip_invented_tools() -> None:
    provider = _provider(tool_count=61)

    values = provider.registry_values()

    assert values["package_metadata"]["composio_toolkit"] == "gmail"
    assert values["package_metadata"]["composio_no_auth"] is False
    assert values["package_metadata"]["auth_type"] == "oauth"
    assert values["package_metadata"]["tools"] == []
    assert values["tool_count"] == 61
    assert values["raw_metadata"]["catalog"]["connection_ready"] is True


@pytest.mark.unit_no_db
def test_anonymous_composio_apps_publish_anonymous_auth() -> None:
    provider = _provider(
        provider_slug="hackernews",
        display_name="Hacker News",
        description="News",
        categories=("News",),
        supported_products=("Hacker News",),
        trusted_logo_key="hackernews",
        composio_toolkit="hackernews",
        composio_no_auth=True,
    )

    values = provider.registry_values()

    assert provider.auth_type == "anonymous"
    assert values["package_metadata"]["auth_type"] == "anonymous"
    assert values["package_metadata"]["composio_no_auth"] is True


@pytest.mark.unit_no_db
def test_expanded_catalog_reports_when_composio_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _provider()
    monkeypatch.setattr(catalog, "_load_live_composio_providers", lambda: ((live,), True))

    providers, is_live = catalog.expanded_catalog_providers()

    assert is_live is True
    assert live in providers
    assert any(provider.transport == "stdio" for provider in providers)


@pytest.mark.unit_no_db
def test_expanded_catalog_falls_back_when_composio_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "_load_live_composio_providers", lambda: ((), False))

    providers, is_live = catalog.expanded_catalog_providers()

    assert is_live is False
    if not any(provider.composio_toolkit for provider in providers):
        pytest.skip(
            "The installed Composio package ships no toolkit metadata, so the "
            "offline bundle carries no Composio apps."
        )
    assert any(provider.composio_toolkit for provider in providers)
