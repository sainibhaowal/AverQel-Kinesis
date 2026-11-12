from __future__ import annotations

from pathlib import Path

import pytest

from app.core import http_proxy
from app.core.http_proxy import (
    ProxyPolicy,
    install_proxy_environment,
    resolve_proxy_policy,
    restore_proxy_environment,
)
from app.core.launch_environment import LaunchEnvironment

pytestmark = pytest.mark.unit_no_db


def _snapshot(values: dict[str, str]) -> LaunchEnvironment:
    return LaunchEnvironment(
        process=values,
        project_env=Path("/missing-project-env"),
        user_env=Path("/missing-user-env"),
    )


def test_proxy_policy_uses_scheme_proxy_then_all_proxy_and_bypasses_local() -> None:
    policy = resolve_proxy_policy(
        _snapshot(
            {
                "http_proxy": "http://proxy.example:8080",
                "ALL_PROXY": "https://fallback.example:8443",
                "NO_PROXY": ".internal.example,example.net:8443,10.20.0.0/16",
            }
        )
    )

    assert policy.proxy_for_url("http://api.example.com") == "http://proxy.example:8080"
    assert policy.proxy_for_url("https://api.example.com") == "https://fallback.example:8443"
    assert policy.proxy_for_url("https://service.internal.example") is None
    assert policy.proxy_for_url("https://example.net:8443") is None
    assert policy.proxy_for_url("http://10.20.1.4") is None
    assert policy.proxy_for_url("http://127.0.0.1:9000") is None
    assert policy.proxy_for_url("file:///tmp/document") is None


def test_invalid_proxy_is_not_used_or_exposed_in_diagnostics() -> None:
    secret = "http://private-user:very-secret@proxy.example/path"
    policy = resolve_proxy_policy(_snapshot({"HTTPS_PROXY": secret}))

    assert policy.https_proxy is None
    assert policy.invalid_variables == ("HTTPS_PROXY",)
    assert "very-secret" not in str(policy.diagnostics())
    assert policy.diagnostics()["https_proxy_configured"] is False


def test_invalid_scheme_proxy_falls_back_to_valid_all_proxy() -> None:
    policy = resolve_proxy_policy(
        _snapshot(
            {
                "HTTPS_PROXY": "file:///not-a-proxy",
                "ALL_PROXY": "http://proxy.example:8080",
            }
        )
    )

    assert policy.https_proxy == "http://proxy.example:8080"
    assert policy.invalid_variables == ("HTTPS_PROXY",)


def test_child_environment_is_a_copy_and_installation_can_be_restored() -> None:
    policy = resolve_proxy_policy(
        _snapshot({"ALL_PROXY": "http://proxy.example:8080", "NO_PROXY": "example.org"})
    )
    base = {"PATH": "/bin", "https_proxy": "http://unvalidated.example"}
    child = policy.child_environment(base)
    assert base["https_proxy"] == "http://unvalidated.example"
    assert child["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert child["NO_PROXY"].startswith("example.org,")

    target = {"HTTP_PROXY": "http://original.example"}
    before = install_proxy_environment(policy, environ=target)
    assert target["HTTP_PROXY"] == "http://proxy.example:8080"
    restore_proxy_environment(before, environ=target)
    assert target == {"HTTP_PROXY": "http://original.example"}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "ftp://proxy.example",
        "http://",
        "http://proxy.example:invalid",
        "http://proxy.example:0",
        "http://proxy.example:65536",
        "http://proxy.example/path",
        "http://proxy.example?secret=value",
        "http://proxy.example#fragment",
    ],
)
def test_proxy_url_validation_rejects_unsupported_or_malformed_values(value: str | None) -> None:
    policy = resolve_proxy_policy(_snapshot({"ALL_PROXY": value or ""}))
    assert policy.http_proxy is None
    assert policy.https_proxy is None


def test_no_proxy_matches_domains_ports_networks_ipv6_and_wildcards() -> None:
    policy = resolve_proxy_policy(
        _snapshot(
            {
                "ALL_PROXY": "http://proxy.example:8080/",
                "NO_PROXY": ".example.org,*.wild.example,service.example.com:8443,"
                "[2001:db8::1]:443,2001:db8::2,10.0.0.0/8,bad/invalid,broken:port",
            }
        )
    )

    assert policy.http_proxy == "http://proxy.example:8080"
    assert policy.proxy_for_url("https://api.example.org") is None
    assert policy.proxy_for_url("https://a.wild.example") is None
    assert policy.proxy_for_url("https://service.example.com:8443") is None
    assert policy.proxy_for_url("https://service.example.com:443") == "http://proxy.example:8080"
    assert policy.proxy_for_url("https://[2001:db8::1]:443") is None
    assert policy.proxy_for_url("https://[2001:db8::1]:444") == "http://proxy.example:8080"
    assert policy.proxy_for_url("https://[2001:db8::2]") is None
    assert policy.proxy_for_url("http://10.12.1.4") is None
    assert policy.proxy_for_url("http://service.example.net") == "http://proxy.example:8080"
    assert policy.proxy_for_url("http://api.localhost") is None
    assert policy.proxy_for_url("http://not-a-url:bad-port") is None
    assert policy.proxy_for_url("ftp://service.example.net") is None


def test_no_proxy_wildcard_and_invalid_entries_are_safe() -> None:
    wildcard = resolve_proxy_policy(_snapshot({"ALL_PROXY": "http://proxy", "NO_PROXY": "*"}))
    assert wildcard.proxy_for_url("https://anything.example") is None

    entries = resolve_proxy_policy(
        _snapshot({"ALL_PROXY": "http://proxy", "NO_PROXY": "broken:port,not-a-network/zz"})
    )
    assert entries.proxy_for_url("https://other.example") == "http://proxy"


def test_empty_policy_removes_proxy_variables_and_default_install_uses_os_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ProxyPolicy(http_proxy=None, https_proxy=None, no_proxy=())
    base = {
        "http_proxy": "http://old-http",
        "HTTP_PROXY": "http://old-http-upper",
        "https_proxy": "http://old-https",
        "HTTPS_PROXY": "http://old-https-upper",
        "all_proxy": "http://old-all",
        "ALL_PROXY": "http://old-all-upper",
        "no_proxy": "old.local",
        "NO_PROXY": "old-upper.local",
    }
    assert policy.child_environment(base) == {}
    target = dict(base)
    monkeypatch.setattr(http_proxy.os, "environ", target)
    before = install_proxy_environment(policy)
    assert target == {}
    restore_proxy_environment(before)
    assert target == base


def test_resolver_uses_the_shared_launch_snapshot_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        http_proxy,
        "get_launch_environment",
        lambda: _snapshot({"HTTPS_PROXY": "http://shared.example:8080"}),
    )
    policy = resolve_proxy_policy()
    assert policy.https_proxy == "http://shared.example:8080"
