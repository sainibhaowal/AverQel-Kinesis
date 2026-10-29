from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.providers.services.searxng_runtime import SearXNGRuntime


def _settings(tmp_path, *, enabled: bool = True, base_url: str = "http://127.0.0.1:8888"):
    return SimpleNamespace(
        searxng_base_url=base_url,
        searxng_local_runtime_enabled=enabled,
        searxng_runtime_dir=str(tmp_path),
        searxng_source_url="https://github.com/searxng/searxng.git",
        searxng_source_ref="master",
        searxng_install_timeout_seconds=60,
        searxng_start_timeout_seconds=10,
    )


@pytest.mark.unit_no_db
def test_disabled_runtime_does_not_install(tmp_path, monkeypatch) -> None:
    runtime = SearXNGRuntime(_settings(tmp_path, enabled=False))
    monkeypatch.setattr(runtime, "_install", lambda: pytest.fail("install was called"))

    runtime.ensure_running()

    assert runtime.status()["state"] == "disabled"


@pytest.mark.unit_no_db
def test_external_endpoint_is_not_managed(tmp_path, monkeypatch) -> None:
    runtime = SearXNGRuntime(_settings(tmp_path, base_url="https://search.example.com"))
    monkeypatch.setattr(runtime, "_install", lambda: pytest.fail("install was called"))

    runtime.ensure_running()

    assert runtime.status()["state"] == "external"
    assert runtime.status()["local_only"] is False


@pytest.mark.unit_no_db
def test_local_runtime_prepare_start_sequence(tmp_path, monkeypatch) -> None:
    runtime = SearXNGRuntime(_settings(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(runtime, "_probe", lambda: False)
    monkeypatch.setattr(runtime, "_install", lambda: calls.append("install"))
    monkeypatch.setattr(runtime, "_write_settings", lambda: calls.append("settings"))
    monkeypatch.setattr(runtime, "_start", lambda: calls.append("start"))
    monkeypatch.setattr(runtime, "_wait_until_ready", lambda: calls.append("ready"))

    runtime.ensure_running()

    assert calls == ["install", "settings", "start", "ready"]


@pytest.mark.unit_no_db
def test_settings_enable_json_and_use_a_private_secret(tmp_path) -> None:
    runtime = SearXNGRuntime(_settings(tmp_path))

    runtime._write_settings()

    content = runtime.settings_path.read_text(encoding="utf-8")
    assert "- json" in content
    assert "ultrasecretkey" not in content
    assert 'secret_key: "' in content
    assert runtime.settings_path.stat().st_mode & 0o077 == 0
