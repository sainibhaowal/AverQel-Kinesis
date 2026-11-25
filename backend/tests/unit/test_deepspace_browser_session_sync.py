from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.deepspace.services import browser_runtime as br_module
from app.deepspace.services.tools import browser_automation as ba_module


@pytest.mark.unit_no_db
def test_export_conversation_storage_state_is_tenant_user_and_conversation_scoped() -> None:
    async def _run() -> None:
        manager = br_module.BrowserManager()
        tenant_a = uuid4()
        user_a = uuid4()
        conv_a = uuid4()
        other_user = uuid4()
        other_conv = uuid4()
        now = datetime.now(UTC)

        session = br_module.BrowserSession(
            id="sess-1",
            tenant_id=tenant_a,
            user_id=user_a,
            conversation_id=conv_a,
            browser_context_id="ctx-1",
            target_id="target-1",
            cdp_session_id="cdp-1",
            created_at=now,
            last_used_at=now,
            idle_deadline=10_000.0,
        )
        manager._sessions[session.id] = session

        async def _fake_send(sess, method: str, params: dict | None = None) -> dict:
            assert sess is session
            if method == "Network.getAllCookies":
                return {
                    "cookies": [
                        {
                            "name": "session_id",
                            "value": "secret-tok",
                            "domain": "app.example.com",
                            "path": "/",
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "Lax",
                            "expires": 1900000000,
                            "priority": "High",
                        },
                        {
                            "name": "ephemeral",
                            "value": "1",
                            "domain": "app.example.com",
                            "path": "/",
                            "expires": -1,
                        },
                    ]
                }
            assert method == "Runtime.evaluate"
            return {
                "result": {
                    "value": {
                        "origin": "https://app.example.com",
                        "localStorage": [{"name": "auth_token", "value": "jwt-123"}],
                    }
                }
            }

        manager._send = _fake_send  # type: ignore[method-assign]

        # Different user or different conversation must receive None.
        assert (
            await manager.export_conversation_storage_state(
                tenant_id=tenant_a, user_id=other_user, conversation_id=conv_a
            )
            is None
        )
        assert (
            await manager.export_conversation_storage_state(
                tenant_id=tenant_a, user_id=user_a, conversation_id=other_conv
            )
            is None
        )

        exported = await manager.export_conversation_storage_state(
            tenant_id=tenant_a, user_id=user_a, conversation_id=conv_a
        )
        assert exported is not None
        assert exported["cookies"] == [
            {
                "name": "session_id",
                "value": "secret-tok",
                "domain": "app.example.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
                "expires": 1900000000.0,
            },
            {
                "name": "ephemeral",
                "value": "1",
                "domain": "app.example.com",
                "path": "/",
                "httpOnly": False,
                "secure": False,
            },
        ]
        assert exported["origins"] == [
            {
                "origin": "https://app.example.com",
                "localStorage": [{"name": "auth_token", "value": "jwt-123"}],
            }
        ]

    asyncio.run(_run())


@pytest.mark.unit_no_db
def test_get_runtime_syncs_sidebar_storage_state_into_headless_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ba_module, "browser_automation_unavailable_reason", lambda _s: None)

    added_cookies: list[list[dict]] = []
    init_scripts: list[str] = []
    eval_calls: list[dict] = []

    class _FakeContext:
        async def add_cookies(self, cookies: list[dict]) -> None:
            added_cookies.append(cookies)

        async def add_init_script(self, script: str) -> None:
            init_scripts.append(script)

    class _FakePage:
        url = "https://app.example.com/dashboard"

        async def evaluate(self, _expr: str, arg: dict) -> None:
            eval_calls.append(arg)

    class _FakeRuntime(ba_module.BrowserAutomationRuntime):
        def __init__(self, config, settings) -> None:
            super().__init__(config, settings)
            self._fake_running = False

        @property
        def is_running(self) -> bool:
            return self._fake_running

        async def start(self, conversation_id, *, storage_state=None) -> ba_module.BrowserSession:
            self._fake_running = True
            self._session = ba_module.BrowserSession(
                session_id="auto-1",
                conversation_id=conversation_id,
                browser_type="chromium",
                playwright=None,
                browser=None,
                context=_FakeContext(),
                page=_FakePage(),
            )
            return self._session

        async def stop(self) -> None:
            self._fake_running = False
            self._session = None

    monkeypatch.setattr(ba_module, "BrowserAutomationRuntime", _FakeRuntime)

    sample_state = {
        "cookies": [
            {
                "name": "sid",
                "value": "abc",
                "domain": "app.example.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            }
        ],
        "origins": [
            {
                "origin": "https://app.example.com",
                "localStorage": [{"name": "token", "value": "xyz"}],
            }
        ],
    }

    class _FakeManager:
        async def export_conversation_storage_state(self, *, tenant_id, user_id, conversation_id):
            return sample_state

    monkeypatch.setattr(br_module, "browser_manager", lambda: _FakeManager())

    ctx = SimpleNamespace(
        auth=SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()),
        conversation_id=uuid4(),
        settings=SimpleNamespace(deepspace_browser_automation_enabled=True),
    )

    async def _run() -> None:
        rt = await ba_module._get_runtime(ctx)
        assert rt.is_running
        assert added_cookies == [sample_state["cookies"]]
        assert len(init_scripts) == 1
        assert "https://app.example.com" in init_scripts[0]
        assert eval_calls == [{"token": "xyz"}]

        # Second call with unchanged origins does not duplicate init_script/eval.
        await ba_module._get_runtime(ctx)
        assert len(init_scripts) == 1
        assert len(eval_calls) == 1
        await rt.stop()

    asyncio.run(_run())


@pytest.mark.unit_no_db
def test_encrypted_storage_bridge_cross_process_and_api_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.deepspace.api import browser as browser_api
    from app.deepspace.services import browser_storage_bridge as bridge

    monkeypatch.setattr(bridge, "_bridge_dir", lambda: tmp_path / "runtime" / "browser-sync")
    (tmp_path / "runtime" / "browser-sync").mkdir(parents=True, exist_ok=True)
    raw_key = AESGCM.generate_key(bit_length=256)

    class _FakeCipher:
        def encrypt(self, plaintext: bytes) -> dict:
            import base64
            import os

            nonce = os.urandom(12)
            ct = AESGCM(raw_key).encrypt(nonce, plaintext, None)
            return {
                "n": base64.b64encode(nonce).decode(),
                "c": base64.b64encode(ct).decode(),
            }

        def decrypt(self, envelope: dict) -> bytes:
            import base64

            return AESGCM(raw_key).decrypt(
                base64.b64decode(envelope["n"]),
                base64.b64decode(envelope["c"]),
                None,
            )

    monkeypatch.setattr(bridge, "get_session_log_cipher", lambda: _FakeCipher())

    tenant_id = uuid4()
    user_id = uuid4()
    conv_id = uuid4()
    other_user = uuid4()

    auth = SimpleNamespace(tenant_id=tenant_id, user_id=user_id)
    req = browser_api.BrowserStorageStateRequest(
        cookies=[
            {
                "name": "auth_sid",
                "value": "s3cr3t-value",
                "domain": "localhost",
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        origins=[
            {
                "origin": "http://localhost:3000",
                "localStorage": [{"name": "user_tok", "value": "abc"}],
            }
        ],
    )
    resp = asyncio.run(browser_api.sync_browser_storage_state(conv_id, req, auth=auth))
    assert resp == {"synced": True, "cookies_count": 1, "origins_count": 1}

    # Verify disk file is encrypted (never contains plaintext cookie value).
    enc_files = list((tmp_path / "runtime" / "browser-sync").glob("*.enc"))
    assert len(enc_files) == 1
    assert "s3cr3t-value" not in enc_files[0].read_text(encoding="utf-8")

    # Simulate a separate Celery worker by clearing the in-memory cache.
    bridge._IN_MEMORY_STATES.clear()

    # Different user cannot load it.
    assert (
        bridge.load_conversation_storage_state(
            tenant_id=tenant_id, user_id=other_user, conversation_id=conv_id
        )
        is None
    )

    # Same tenant/user/conversation loads and decrypts it from disk.
    loaded = bridge.load_conversation_storage_state(
        tenant_id=tenant_id, user_id=user_id, conversation_id=conv_id
    )
    assert loaded is not None
    assert loaded["cookies"][0]["name"] == "auth_sid"
    assert loaded["cookies"][0]["value"] == "s3cr3t-value"
    assert loaded["origins"] == [
        {"origin": "http://localhost:3000", "localStorage": [{"name": "user_tok", "value": "abc"}]}
    ]

    # _load_sidebar_storage_state falls back to the encrypted bridge when no live CDP session exists.
    class _EmptyManager:
        async def export_conversation_storage_state(self, *, tenant_id, user_id, conversation_id):
            return None

    monkeypatch.setattr(br_module, "browser_manager", lambda: _EmptyManager())
    ctx = SimpleNamespace(auth=auth, conversation_id=conv_id)
    fallback_state = asyncio.run(ba_module._load_sidebar_storage_state(ctx))
    assert fallback_state == loaded

    bridge.delete_conversation_storage_state(
        tenant_id=tenant_id, user_id=user_id, conversation_id=conv_id
    )
    assert (
        bridge.load_conversation_storage_state(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conv_id
        )
        is None
    )


@pytest.mark.unit_no_db
def test_browser_automation_cdp_endpoint_attach_preserves_external_browser_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_calls: list[str] = []
    cdp_endpoints: list[str] = []

    class _FakePage:
        async def close(self) -> None:
            closed_calls.append("page")

    class _FakeContext:
        pages = [_FakePage()]

        async def close(self) -> None:
            closed_calls.append("context")

    class _FakeBrowser:
        contexts = [_FakeContext()]

        async def close(self) -> None:
            closed_calls.append("browser")

    class _FakeChromium:
        async def connect_over_cdp(self, endpoint: str, *, timeout: int = 30_000) -> _FakeBrowser:
            cdp_endpoints.append(endpoint)
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self) -> None:
            closed_calls.append("playwright_stop")

    class _FakePlaywrightFactory:
        async def start(self) -> _FakePlaywright:
            return _FakePlaywright()

    monkeypatch.setattr(ba_module, "_playwright_factory", lambda: _FakePlaywrightFactory())

    config = ba_module.BrowserAutomationConfig(
        browser_type="chromium",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    runtime = ba_module.BrowserAutomationRuntime(config, SimpleNamespace())

    async def _run() -> None:
        await runtime.start(uuid4())
        assert cdp_endpoints == ["http://127.0.0.1:9222"]
        await runtime.stop()
        # External browser, context, and page must remain open; only playwright client stops.
        assert closed_calls == ["playwright_stop"]

    asyncio.run(_run())


@pytest.mark.unit_no_db
def test_sync_storage_state_navigates_blank_headless_page_to_sidebar_active_url() -> None:
    navigated: list[tuple[str, str]] = []

    class _FakeContext:
        async def add_cookies(self, cookies: list[dict]) -> None:
            return None

    class _FakePage:
        url = "about:blank"

        async def goto(
            self,
            target_url: str,
            *,
            wait_until: str = "load",
            timeout: int = 15_000,
        ) -> None:
            self.url = target_url
            navigated.append((target_url, wait_until))

        async def wait_for_load_state(self, state: str, *, timeout: int = 4_000) -> None:
            return None

    rt = ba_module.BrowserAutomationRuntime(ba_module.BrowserAutomationConfig(), SimpleNamespace())
    rt._session = ba_module.BrowserSession(
        session_id="auto-blank",
        conversation_id=uuid4(),
        browser_type="chromium",
        playwright=None,
        browser=None,
        context=_FakeContext(),
        page=_FakePage(),
    )

    async def _run() -> None:
        await rt.sync_storage_state(
            {
                "cookies": [],
                "origins": [],
                "active_url": "https://openai.com/",
            }
        )
        assert navigated == [("https://openai.com/", "load")]
        assert rt._session.page.url == "https://openai.com/"

    asyncio.run(_run())


@pytest.mark.unit_no_db
def test_browser_get_content_and_screenshot_fallback_to_sidebar_webview_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    fake_png = b"\x89PNG\r\n\x1a\n" + b"real-webview-image-bytes"
    fake_b64 = base64.b64encode(fake_png).decode()

    class _ChallengePage:
        url = "https://openai.com/"

        async def title(self) -> str:
            return "Just a moment..."

    class _ChallengeRuntime:
        page = _ChallengePage()

        async def screenshot(self, *_args, **_kwargs) -> dict:
            return {"image_base64": "AA==", "format": "png", "bytes": 100}

        async def get_content(self, *_args, **_kwargs) -> dict:
            return {"content": "Just a moment... Checking your browser"}

    async def _fake_get_runtime(_ctx):
        return _ChallengeRuntime()

    async def _fake_load_state(_ctx):
        return {
            "cookies": [],
            "origins": [],
            "active_url": "https://openai.com/",
            "text_snapshot": "Introducing GPT-6.2 Astra and Sol",
            "screenshot_base64": fake_b64,
        }

    monkeypatch.setattr(ba_module, "_get_runtime", _fake_get_runtime)
    monkeypatch.setattr(ba_module, "_load_sidebar_storage_state", _fake_load_state)

    ctx = SimpleNamespace(
        auth=SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()), conversation_id=uuid4()
    )

    async def _run() -> None:
        shot = await ba_module._browser_screenshot({}, ctx)
        assert shot["source"] == "sidebar_browser_snapshot"
        assert shot["image_base64"] == fake_b64
        assert shot["bytes"] == len(fake_png)

        content = await ba_module._browser_get_content({"format": "text"}, ctx)
        assert content["source"] == "sidebar_browser_snapshot"
        assert content["content"] == "Introducing GPT-6.2 Astra and Sol"

    asyncio.run(_run())
