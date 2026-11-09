from app.core.dev_server import (
    SOURCE_RELOAD_GRACEFUL_SHUTDOWN_SECONDS,
    backend_bind_host,
    graceful_shutdown_timeout,
)


def test_source_reload_has_a_bounded_websocket_drain() -> None:
    assert SOURCE_RELOAD_GRACEFUL_SHUTDOWN_SECONDS == 10.0
    assert graceful_shutdown_timeout(source_reload=True) == 10.0


def test_non_reloading_server_keeps_uvicorn_default_shutdown_behavior() -> None:
    assert graceful_shutdown_timeout(source_reload=False) is None


def test_desktop_backend_binds_only_to_loopback() -> None:
    assert backend_bind_host({"AVERQEL_DESKTOP_LOCAL_RUNTIME": "1"}) == "127.0.0.1"
    assert backend_bind_host({"AVERQEL_DESKTOP_PACKAGED": "1"}) == "127.0.0.1"


def test_non_desktop_backend_keeps_its_existing_network_binding() -> None:
    assert backend_bind_host({}) == "0.0.0.0"
