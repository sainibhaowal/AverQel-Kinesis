from __future__ import annotations

import logging

import pytest

from app import main as main_module
from app.core.http_proxy import ProxyPolicy

pytestmark = pytest.mark.unit_no_db


def _lifecycle_handler(name: str):
    return next(
        handler
        for handler in main_module.app.router.on_startup + main_module.app.router.on_shutdown
        if handler.__name__ == name
    )


def test_proxy_policy_is_installed_and_restored_with_application_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ProxyPolicy(
        http_proxy="http://proxy.example:8080",
        https_proxy=None,
        no_proxy=("localhost",),
    )
    previous = {"HTTP_PROXY": "http://original.example"}
    restored: list[dict[str, str | None]] = []
    monkeypatch.setattr(main_module, "resolve_proxy_policy", lambda: policy)
    monkeypatch.setattr(main_module, "install_proxy_environment", lambda actual: previous)
    monkeypatch.setattr(main_module, "restore_proxy_environment", restored.append)

    _lifecycle_handler("install_outbound_proxy_policy")()
    assert main_module.app.state.proxy_environment_before_install is previous

    _lifecycle_handler("restore_outbound_proxy_policy")()
    assert restored == [previous]
    assert not hasattr(main_module.app.state, "proxy_environment_before_install")

    _lifecycle_handler("restore_outbound_proxy_policy")()
    assert restored == [previous]


def test_invalid_proxy_configuration_logs_names_without_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_url = "http://secret-user:secret-password@bad-proxy.example/path"
    policy = ProxyPolicy(
        http_proxy=None,
        https_proxy=None,
        no_proxy=("localhost",),
        invalid_variables=("HTTPS_PROXY",),
    )
    monkeypatch.setattr(main_module, "resolve_proxy_policy", lambda: policy)
    monkeypatch.setattr(main_module, "install_proxy_environment", lambda _policy: {})
    caplog.set_level(logging.WARNING, logger="app.main")

    _lifecycle_handler("install_outbound_proxy_policy")()

    assert "HTTPS_PROXY" in caplog.text
    assert secret_url not in caplog.text
    if hasattr(main_module.app.state, "proxy_environment_before_install"):
        del main_module.app.state.proxy_environment_before_install


@pytest.mark.asyncio
async def test_application_shutdown_closes_ssh_rpc_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import ssh_rpc

    closed: list[bool] = []

    class _Pool:
        async def close_all(self) -> None:
            closed.append(True)

    pool = _Pool()
    monkeypatch.setattr(ssh_rpc, "ssh_rpc_pool", lambda: pool)

    await _lifecycle_handler("stop_deepspace_ssh_rpc")()

    assert closed == [True]
