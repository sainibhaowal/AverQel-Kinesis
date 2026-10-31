from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.integrations.models.mcp_server import MCPServer
from app.integrations.services import composio_service as composio
from app.integrations.services.composio_service import (
    ComposioConfigurationError,
    ComposioService,
    composio_callback_url,
    composio_provider_slug,
    composio_toolkit_for_entry,
    composio_toolkit_slug,
    is_composio_host,
    is_composio_server,
    resolve_composio_api_key,
)

TENANT_ID = uuid.UUID("11111111-1111-7111-8111-111111111111")
USER_ID = uuid.UUID("22222222-2222-7222-8222-222222222222")
SERVER_ID = uuid.UUID("33333333-3333-7333-8333-333333333333")


@pytest.mark.unit_no_db
def test_composio_secret_path_uses_the_single_averqel_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from app.core.averqel_home import get_averqel_home

    app_home = tmp_path / "app-home"
    monkeypatch.setenv("AVERQEL_HOME", str(app_home))
    get_averqel_home.cache_clear()
    try:
        assert composio.composio_secrets_path() == app_home / "secrets" / "composio_key.txt"
    finally:
        get_averqel_home.cache_clear()


class _StubToolkits:
    """Stand-in for ``client.toolkits`` with the two payload flavours."""

    def __init__(self, *, listed=None, retrieved=None) -> None:
        self._listed = listed
        self._retrieved = retrieved

    def list(self, **_: object) -> object:
        return self._listed

    def get(self, slug: str) -> object:
        assert slug
        return self._retrieved


class _StubClient:
    """Stand-in for the Composio SDK client used by the broker."""

    def __init__(self, *, toolkits=None, session=None) -> None:
        self.toolkits = toolkits
        self._session = session

    def use(self, session_id: str, *, mcp: bool = False) -> object:
        assert session_id and mcp
        return self._session


def _settings() -> SimpleNamespace:
    """Return settings with only the Composio key the broker reads."""
    return SimpleNamespace(composio_api_key="test-key")


@pytest.fixture
def isolated_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep key resolution independent of the developer's saved secrets file."""
    monkeypatch.setattr(composio, "_read_secret_file", lambda: None)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)


def _service(monkeypatch: pytest.MonkeyPatch, client: object, settings=None) -> ComposioService:
    monkeypatch.setattr(composio, "_sdk", lambda api_key: client)
    return ComposioService(None, settings or _settings())


def _server(**overrides: object) -> MCPServer:
    values: dict[str, object] = {
        "id": SERVER_ID,
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "provider_slug": composio_provider_slug("gmail"),
        "name": "Gmail",
        "transport": "streamable_http",
        "config": {},
    }
    values.update(overrides)
    return MCPServer(**values)


def test_provider_and_toolkit_slugs_round_trip() -> None:
    assert composio_provider_slug("gmail") == "composio:gmail"
    assert composio_toolkit_slug("composio:gmail") == "gmail"
    assert composio_toolkit_slug("composio:GMAIL") == "gmail"
    assert composio_toolkit_slug("composio:") is None
    assert composio_toolkit_slug("google-gmail") is None
    assert composio_toolkit_slug(None) is None


def test_is_composio_server_only_accepts_brokered_provider_slugs() -> None:
    assert is_composio_server(_server()) is True
    assert is_composio_server(_server(provider_slug="google-gmail")) is False
    assert is_composio_server(_server(provider_slug=None)) is False


def test_is_composio_host_accepts_gateway_and_legacy_catalog_hosts() -> None:
    assert is_composio_host("backend.composio.dev") is True
    assert is_composio_host("BACKEND.COMPOSIO.DEV") is True
    assert is_composio_host("api.composio.dev") is True
    assert is_composio_host("mcp.notion.com") is False
    assert is_composio_host("notcomposio.dev") is False
    assert is_composio_host(None) is False


def test_composio_toolkit_for_entry_reads_the_catalog_marker() -> None:
    entry = SimpleNamespace(
        package_metadata={"composio_toolkit": "Notion"},
        provider_slug="notion",
        remote_url="https://mcp.notion.com/mcp",
    )

    assert composio_toolkit_for_entry(entry) == "notion"


def test_composio_toolkit_for_entry_reads_the_retired_catalog_host() -> None:
    entry = SimpleNamespace(
        package_metadata={},
        provider_slug="github",
        remote_url="https://api.composio.dev/mcp/github",
    )

    assert composio_toolkit_for_entry(entry) == "github"


def test_composio_toolkit_for_entry_ignores_providers_that_composio_does_not_broker() -> None:
    curated = SimpleNamespace(
        package_metadata={},
        provider_slug="google-gmail",
        remote_url="https://gmailmcp.googleapis.com/mcp/v1",
    )
    local_tool = SimpleNamespace(
        package_metadata={},
        provider_slug="local-git",
        remote_url=None,
    )

    assert composio_toolkit_for_entry(curated) is None
    assert composio_toolkit_for_entry(local_tool) is None


def test_composio_toolkit_for_entry_falls_back_to_the_slug_for_a_broken_marker() -> None:
    entry = SimpleNamespace(
        package_metadata={"composio_toolkit": "../../etc/passwd"},
        provider_slug="gmail",
        remote_url="https://backend.composio.dev/tool_router/mcp",
    )

    assert composio_toolkit_for_entry(entry) == "gmail"


def test_composio_callback_url_accepts_configured_origins() -> None:
    assert (
        composio_callback_url("http://localhost:10030", "gmail")
        == "http://localhost:10030/dashboard/mcp/composio/callback?toolkit_slug=gmail"
    )
    assert (
        composio_callback_url("https://app.averqel.example/", "googlecalendar")
        == "https://app.averqel.example/dashboard/mcp/composio/callback?toolkit_slug=googlecalendar"
    )


def test_composio_callback_url_rejects_unusable_origins() -> None:
    assert composio_callback_url(None, "gmail") is None
    assert composio_callback_url("", "gmail") is None
    assert composio_callback_url("http://app.example.com", "gmail") is None
    assert composio_callback_url("https://app.example.com/dashboard", "gmail") is None
    assert composio_callback_url("https://user:secret@app.example.com", "gmail") is None
    assert composio_callback_url("https://app.example.com?next=evil", "gmail") is None


def test_resolve_composio_api_key_prefers_the_saved_secret_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composio, "_read_secret_file", lambda: "file-key")
    monkeypatch.setenv("COMPOSIO_API_KEY", "env-key")

    assert resolve_composio_api_key(_settings()) == "file-key"


def test_resolve_composio_api_key_falls_back_to_settings_then_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composio, "_read_secret_file", lambda: None)
    monkeypatch.setenv("COMPOSIO_API_KEY", "env-key")
    assert resolve_composio_api_key(_settings()) == "test-key"

    monkeypatch.setenv("COMPOSIO_API_KEY", "env-key")
    assert resolve_composio_api_key(SimpleNamespace(composio_api_key="")) == "env-key"

    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert resolve_composio_api_key(SimpleNamespace(composio_api_key="")) is None


def test_toolkit_details_reports_authorization_from_managed_schemes(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    retrieved = SimpleNamespace(
        slug="gmail",
        name="Gmail",
        composio_managed_auth_schemes=["OAUTH2"],
        meta={
            "description": "Email for humans",
            "logo": "https://logos.composio.dev/api/gmail",
            "app_url": "https://mail.google.com",
            "categories": [{"name": "Email", "slug": "email"}],
            "tools_count": 61,
            "triggers_count": 2,
        },
    )
    service = _service(monkeypatch, _StubClient(toolkits=_StubToolkits(retrieved=retrieved)))

    details = service.toolkit_details("GMAIL")

    assert details["slug"] == "gmail"
    assert details["name"] == "Gmail"
    assert details["categories"] == ["Email"]
    assert details["tools_count"] == 61
    assert details["no_auth"] is False


def test_toolkit_details_marks_toolkits_without_auth_schemes_anonymous(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    retrieved = SimpleNamespace(
        slug="hackernews",
        name="Hacker News",
        composio_managed_auth_schemes=[],
        composio_managed_auth=[],
        meta={"categories": [{"name": "News"}], "tools_count": 14},
    )
    service = _service(monkeypatch, _StubClient(toolkits=_StubToolkits(retrieved=retrieved)))

    assert service.toolkit_details("hackernews")["no_auth"] is True


def test_toolkit_details_rejects_unusable_metadata(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    service = _service(
        monkeypatch,
        _StubClient(toolkits=_StubToolkits(retrieved=SimpleNamespace(slug="", name=""))),
    )

    with pytest.raises(ComposioConfigurationError):
        service.toolkit_details("gmail")

    with pytest.raises(ComposioConfigurationError):
        service.toolkit_details("../../etc/passwd")


def test_list_toolkits_normalizes_list_items_and_keeps_paging(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    listed = SimpleNamespace(
        items=[
            SimpleNamespace(
                slug="gmail",
                name="Gmail",
                no_auth=False,
                auth_schemes=["OAUTH2"],
                meta={"categories": [{"id": "email", "name": "Email"}], "tools_count": 61.0},
            ),
            SimpleNamespace(slug="", name="Broken", no_auth=True, auth_schemes=[], meta={}),
        ],
        next_cursor="cursor-2",
    )
    service = _service(monkeypatch, _StubClient(toolkits=_StubToolkits(listed=listed)))

    page = service.list_toolkits(limit=500, cursor="cursor-1")

    assert [item["slug"] for item in page["items"]] == ["gmail"]
    assert page["items"][0]["categories"] == ["Email"]
    assert page["items"][0]["no_auth"] is False
    assert page["next_cursor"] == "cursor-2"


def test_list_toolkits_reports_an_unreachable_composio(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    class _Broken:
        def list(self, **_: object) -> object:
            raise RuntimeError("boom")

    service = _service(monkeypatch, _StubClient(toolkits=_Broken()))

    with pytest.raises(ComposioConfigurationError):
        service.list_toolkits()


def test_endpoint_config_rejects_state_that_does_not_match_the_app(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None
) -> None:
    service = _service(monkeypatch, _StubClient())
    server = _server(config={"composio_toolkit_slug": "notion"})

    with pytest.raises(ComposioConfigurationError):
        service.mcp_endpoint_config(tenant_id=TENANT_ID, user_id=USER_ID, server=server)

    with pytest.raises(ComposioConfigurationError):
        service.mcp_endpoint_config(tenant_id=TENANT_ID, user_id=USER_ID, server=_server(config={}))


def test_endpoint_config_requires_a_provider_key(
    monkeypatch: pytest.MonkeyPatch, isolated_key: None, tmp_path: object
) -> None:
    service = ComposioService(None, SimpleNamespace(composio_api_key=""))
    monkeypatch.setattr(composio, "_read_secret_file", lambda: None)

    with pytest.raises(ComposioConfigurationError):
        service.list_toolkits()


def test_endpoint_config_returns_transient_gateway_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composio, "_read_secret_file", lambda: "test-key")
    session = SimpleNamespace(
        mcp=SimpleNamespace(
            url="https://backend.composio.dev/tool_router/trs_unit/mcp",
            headers={"X-Api-Key": "ak_unit"},
        )
    )
    service = _service(monkeypatch, _StubClient(session=session), settings=get_settings())
    server = _server()
    server.config = {
        "composio_toolkit_slug": "gmail",
        **service._encrypt_session_id(
            "trs_unit", tenant_id=TENANT_ID, user_id=USER_ID, server_id=SERVER_ID
        ),
    }

    config = service.mcp_endpoint_config(tenant_id=TENANT_ID, user_id=USER_ID, server=server)

    assert config == {
        "auth_mode": "mcp",
        "credentials": {
            "server_url": "https://backend.composio.dev/tool_router/trs_unit/mcp",
            "transport": "streamable_http",
            "oauth_mode": "none",
            "headers": {"x-api-key": "ak_unit"},
        },
    }
    assert "trs_unit" not in str(server.config)


def test_endpoint_config_rejects_foreign_hosts_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(composio, "_read_secret_file", lambda: "test-key")
    ephemeral = ComposioService(None, settings)
    encrypted = ephemeral._encrypt_session_id(
        "trs_unit", tenant_id=TENANT_ID, user_id=USER_ID, server_id=SERVER_ID
    )
    server = _server()
    server.config = {"composio_toolkit_slug": "gmail", **encrypted}

    foreign_host = _StubClient(
        session=SimpleNamespace(
            mcp=SimpleNamespace(
                url="https://evil.example.com/tool_router/trs_unit/mcp",
                headers={"x-api-key": "ak_unit"},
            )
        )
    )
    with pytest.raises(ComposioConfigurationError):
        _service(monkeypatch, foreign_host, settings=settings).mcp_endpoint_config(
            tenant_id=TENANT_ID, user_id=USER_ID, server=server
        )

    unsupported_header = _StubClient(
        session=SimpleNamespace(
            mcp=SimpleNamespace(
                url="https://backend.composio.dev/tool_router/trs_unit/mcp",
                headers={"authorization": "Bearer ak_unit"},
            )
        )
    )
    with pytest.raises(ComposioConfigurationError):
        _service(monkeypatch, unsupported_header, settings=settings).mcp_endpoint_config(
            tenant_id=TENANT_ID, user_id=USER_ID, server=server
        )


def test_complete_auth_rejects_invalid_redirect_parameters(isolated_key: None) -> None:
    service = ComposioService(None, _settings())

    with pytest.raises(ComposioConfigurationError):
        service.complete_auth(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            toolkit_slug="gmail",
            connected_account_id="",
            status="success",
        )

    with pytest.raises(ComposioConfigurationError):
        service.complete_auth(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            toolkit_slug="gmail",
            connected_account_id="ca_1\n2",
            status="success",
        )

    with pytest.raises(ComposioConfigurationError):
        service.complete_auth(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            toolkit_slug="../../etc/passwd",
            connected_account_id="ca_123",
            status="success",
        )
