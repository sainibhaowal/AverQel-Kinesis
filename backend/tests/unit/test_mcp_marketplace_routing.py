from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.api.mcp import _marketplace_auth_type


def _entry(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "package_metadata": {},
        "provider_slug": "local-git",
        "remote_url": None,
        "oauth_requirements": {"type": "anonymous"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit_no_db
def test_marketplace_auth_type_reports_composio_apps_as_oauth() -> None:
    entry = _entry(
        package_metadata={"composio_toolkit": "gmail"},
        provider_slug="gmail",
        remote_url="https://backend.composio.dev/tool_router/mcp",
        oauth_requirements={"type": "setup_required"},
    )

    assert _marketplace_auth_type(entry) == "oauth"


@pytest.mark.unit_no_db
def test_marketplace_auth_type_reports_composio_no_auth_apps_as_anonymous() -> None:
    entry = _entry(
        package_metadata={"composio_toolkit": "hackernews", "composio_no_auth": True},
        provider_slug="hackernews",
        remote_url="https://backend.composio.dev/tool_router/mcp",
        oauth_requirements={"type": "oauth"},
    )

    assert _marketplace_auth_type(entry) == "anonymous"


@pytest.mark.unit_no_db
def test_marketplace_auth_type_keeps_the_registry_declared_type() -> None:
    entry = _entry(oauth_requirements={"type": "token"})

    assert _marketplace_auth_type(entry) == "token"
