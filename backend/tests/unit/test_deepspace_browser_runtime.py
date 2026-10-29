"""Tests for the DeepSpace browser runtime.

The checks cover the ownership boundary between tenants, the addresses the
runtime refuses, the idle sweeper, the reduced environment the browser
receives, and a real browser driven over the DevTools protocol.

The checks that need a browser are skipped on a host that has none.
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.deepspace.services.browser_runtime import (
    MAX_SCROLL_DELTA,
    MAX_TYPE_TEXT_LENGTH,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    BrowserManager,
    BrowserNavigationError,
    BrowserSession,
    BrowserSessionNotFoundError,
    BrowserUnavailableError,
    browser_runtime_enabled,
    build_browser_environment,
    find_browser_executable,
    validate_browser_url,
)

pytestmark = [pytest.mark.unit_no_db, pytest.mark.xdist_group("deepspace_browser")]

#: The browser this host can run, when it has one.
BROWSER = find_browser_executable()

requires_browser = pytest.mark.skipif(
    BROWSER is None,
    reason="This host has no Chromium browser for the browser runtime.",
)


def _settings(**overrides: object) -> SimpleNamespace:
    """Build the deployment settings the runtime reads.

    Args:
        overrides: The settings that differ from the defaults.

    Returns:
        The settings in force.
    """
    values: dict[str, object] = {
        "deepspace_browser_enabled": True,
        "deepspace_browser_executable": "",
        "deepspace_browser_idle_timeout_seconds": 900.0,
        "deepspace_sandbox_policy": "workspace-write",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDevToolsConnection:
    """Record the commands the manager sends and answer the page reads."""

    def __init__(self) -> None:
        """Create a connection with no commands yet."""
        self.calls: list[tuple[str, dict[str, object], str | None]] = []

    async def send(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        """Record one command and answer the reads the page capture needs.

        Args:
            method: The DevTools method the manager calls.
            params: The command's parameters.
            session_id: The attached page session.
            timeout_seconds: The time the answer may take.

        Returns:
            The answer the manager expects.
        """
        del timeout_seconds
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Page.getNavigationHistory":
            return {
                "entries": [{"id": 1, "url": "http://localhost/served"}],
                "currentIndex": 0,
            }
        if method == "Runtime.evaluate":
            return {"result": {"value": "Example"}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")}
        return {}

    def input_calls(self) -> list[tuple[str, dict[str, object], str | None]]:
        """Return the input commands the manager sent.

        Returns:
            The commands whose method starts with ``Input.``.
        """
        return [call for call in self.calls if call[0].startswith("Input.")]


def _managed_session(
    connection: FakeDevToolsConnection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> tuple[BrowserManager, BrowserSession]:
    """Build a manager that drives one session over a fake connection.

    Args:
        connection: The connection that records the commands.
        tenant_id: The tenant that owns the session.
        user_id: The user that owns the session.
        conversation_id: The conversation that owns the session.

    Returns:
        The manager and its live session.
    """
    manager = BrowserManager()
    now = datetime.now(UTC)
    session = BrowserSession(
        id="session-1",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        browser_context_id="context-1",
        target_id="target-1",
        cdp_session_id="cdp-session-1",
        created_at=now,
        last_used_at=now,
    )
    manager._sessions[session.id] = session
    manager._process = SimpleNamespace(connection=connection)
    return manager, session


@pytest.fixture
def page_server() -> Iterator[str]:
    """Serve one page the browser can open.

    Yields:
        The address the server listens on.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name the HTTP server requires.
            body = (
                f"<html><head><title>DeepSpace {self.path}</title></head>"
                f"<body><h1>DeepSpace {self.path}</h1></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def interactive_page_server() -> Iterator[str]:
    """Serve a page whose input and button react to the runtime's input.

    Yields:
        The address the server listens on.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name the HTTP server requires.
            if self.path == "/clicked":
                body = (
                    b"<html><head><title>Clicked</title></head><body><h1>Clicked</h1></body></html>"
                )
            else:
                body = (
                    b"<html><head><title>Interactive</title></head><body>"
                    b'<input id="name" style="position:absolute;left:20px;top:20px;'
                    b'width:300px;height:40px" oninput="document.title=this.value">'
                    b'<button id="go" style="position:absolute;left:20px;top:100px;'
                    b'width:160px;height:40px" onclick="location.href=\'/clicked\'">Go</button>'
                    b"</body></html>"
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------
# The addresses the runtime opens
# --------------------------------------------------------------------------


def test_only_http_addresses_are_opened() -> None:
    assert validate_browser_url("http://localhost:3000/path") == "http://localhost:3000/path"
    assert validate_browser_url("https://example.com") == "https://example.com/"


@pytest.mark.parametrize(
    "address",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<h1>hi</h1>",
        "ftp://example.com/file",
    ],
)
def test_an_address_that_is_not_http_is_refused(address: str) -> None:
    with pytest.raises(BrowserNavigationError) as failure:
        validate_browser_url(address)

    assert "Only http:// and https:// URLs can be opened." in str(failure.value)


def test_an_address_that_is_not_a_url_is_refused() -> None:
    with pytest.raises(BrowserNavigationError) as failure:
        validate_browser_url("not a url")

    assert "Enter a full URL" in str(failure.value)


def test_an_address_with_a_password_is_refused() -> None:
    with pytest.raises(BrowserNavigationError) as failure:
        validate_browser_url("http://user:secret@example.com/")

    assert "password" in str(failure.value)


def test_the_runtime_is_off_until_a_deployment_turns_it_on() -> None:
    assert browser_runtime_enabled(_settings(deepspace_browser_enabled=True)) is True
    assert browser_runtime_enabled(_settings(deepspace_browser_enabled=False)) is False
    assert browser_runtime_enabled(SimpleNamespace()) is False


# --------------------------------------------------------------------------
# The environment the browser receives
# --------------------------------------------------------------------------


def test_the_browser_environment_carries_no_deployment_secret(tmp_path) -> None:
    environment = build_browser_environment(tmp_path, mode="workspace-write")

    assert set(environment) == {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "TERM",
        "DEEPSPACE_SANDBOX",
        "DEEPSPACE_SANDBOX_MODE",
        "DEEPSPACE_WORKSPACE",
        "DEEPSPACE_BROWSER",
    }
    assert environment["HOME"] == str(tmp_path)
    assert environment["DEEPSPACE_BROWSER"] == "1"
    assert "DEEPSPACE_SHELL" not in environment


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@requires_browser
async def test_a_conversation_claims_one_browser_session() -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(),
        )
        again = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(),
        )
        other = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            settings=_settings(),
        )

        assert again.id == session.id
        assert other.id != session.id
        assert (
            manager.session_for_conversation(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            is not None
        )
    finally:
        await manager.shutdown()


@requires_browser
async def test_another_tenants_session_id_is_refused_like_one_that_does_not_exist() -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(),
        )
        other_tenant = uuid.uuid4()

        with pytest.raises(BrowserSessionNotFoundError) as stolen:
            manager.get(
                session.id,
                tenant_id=other_tenant,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        with pytest.raises(BrowserSessionNotFoundError) as missing:
            manager.get(
                "no-such-session",
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )

        assert str(stolen.value) == str(missing.value)
    finally:
        await manager.shutdown()


@requires_browser
async def test_an_idle_session_is_closed() -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(deepspace_browser_idle_timeout_seconds=0.5),
        )
        await asyncio.sleep(0.7)

        closed = await manager.sweep()

        assert session.id in closed
        assert (
            manager.session_for_conversation(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            is None
        )
    finally:
        await manager.shutdown()


@requires_browser
async def test_the_browser_process_stops_with_the_manager() -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    await manager.open(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        settings=_settings(),
    )
    pid = manager.browser_pid
    assert pid is not None

    await manager.shutdown()

    assert manager.browser_pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_deployment_without_a_browser_runtime_refuses_to_open_a_session() -> None:
    manager = BrowserManager()

    with pytest.raises(BrowserUnavailableError) as failure:
        await manager.open(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            settings=_settings(deepspace_browser_enabled=False),
        )

    assert "no browser runtime" in str(failure.value)


# --------------------------------------------------------------------------
# Input commands
# --------------------------------------------------------------------------


async def test_a_click_sends_the_mouse_sequence_to_the_page() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    page = await manager.click(
        session.id,
        x=120,
        y=240,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    assert connection.input_calls() == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": 120.0, "y": 240.0},
            "cdp-session-1",
        ),
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": 120.0,
                "y": 240.0,
                "button": "left",
                "clickCount": 1,
            },
            "cdp-session-1",
        ),
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": 120.0,
                "y": 240.0,
                "button": "left",
                "clickCount": 1,
            },
            "cdp-session-1",
        ),
    ]
    assert page.url == "http://localhost/served"


async def test_typing_inserts_text_into_the_focused_field() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    await manager.type_text(
        session.id,
        text="hello",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    assert connection.input_calls() == [
        ("Input.insertText", {"text": "hello"}, "cdp-session-1"),
    ]


async def test_pressing_enter_sends_a_key_down_and_a_key_up() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    await manager.press_key(
        session.id,
        key="Enter",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    assert connection.input_calls() == [
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "modifiers": 0,
                "text": "\r",
            },
            "cdp-session-1",
        ),
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "modifiers": 0,
            },
            "cdp-session-1",
        ),
    ]


async def test_a_scroll_wheel_turns_at_the_viewport_centre_and_is_clamped() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    await manager.scroll(
        session.id,
        delta_x=10_000,
        delta_y=-10_000,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    assert connection.input_calls() == [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": VIEWPORT_WIDTH // 2,
                "y": VIEWPORT_HEIGHT // 2,
                "deltaX": MAX_SCROLL_DELTA,
                "deltaY": -MAX_SCROLL_DELTA,
            },
            "cdp-session-1",
        )
    ]


async def test_a_click_outside_the_viewport_is_refused() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    with pytest.raises(BrowserNavigationError) as failure:
        await manager.click(
            session.id,
            x=VIEWPORT_WIDTH,
            y=10,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    assert "outside" in str(failure.value)
    assert connection.input_calls() == []


async def test_an_unsupported_key_is_refused() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    with pytest.raises(BrowserNavigationError) as failure:
        await manager.press_key(
            session.id,
            key="F1",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    assert "not supported" in str(failure.value)
    assert connection.input_calls() == []


async def test_typing_refuses_empty_and_oversized_text() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    with pytest.raises(BrowserNavigationError):
        await manager.type_text(
            session.id,
            text="",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    with pytest.raises(BrowserNavigationError):
        await manager.type_text(
            session.id,
            text="a" * (MAX_TYPE_TEXT_LENGTH + 1),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    assert connection.input_calls() == []


async def test_another_tenant_cannot_drive_the_session() -> None:
    connection = FakeDevToolsConnection()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    manager, session = _managed_session(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    with pytest.raises(BrowserSessionNotFoundError):
        await manager.click(
            session.id,
            x=10,
            y=10,
            tenant_id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
        )
    with pytest.raises(BrowserSessionNotFoundError):
        await manager.type_text(
            session.id,
            text="hello",
            tenant_id=tenant_id,
            user_id=uuid.uuid4(),
            conversation_id=conversation_id,
        )

    assert connection.input_calls() == []


# --------------------------------------------------------------------------
# The real browser
# --------------------------------------------------------------------------


@requires_browser
async def test_the_browser_opens_a_page_and_screenshots_it(page_server: str) -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(),
        )

        page = await manager.navigate(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            url=f"{page_server}/one",
        )

        assert page.url == f"{page_server}/one"
        assert page.title == "DeepSpace /one"
        assert page.screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(page.screenshot) > 0
        assert page.can_go_back is False
        assert page.can_go_forward is False

        second = await manager.navigate(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            url=f"{page_server}/two",
        )
        assert second.url == f"{page_server}/two"
        assert second.can_go_back is True

        # The browser's own history is the history, so going back returns to
        # the page the browser recorded, not to a list of typed addresses.
        back = await manager.back(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        assert back.url == f"{page_server}/one"
        assert back.can_go_forward is True

        forward = await manager.forward(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        assert forward.url == f"{page_server}/two"

        reloaded = await manager.reload(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        assert reloaded.url == f"{page_server}/two"
        assert reloaded.title == "DeepSpace /two"
    finally:
        await manager.shutdown()


@requires_browser
async def test_the_browser_types_into_a_field_and_clicks_a_button(
    interactive_page_server: str,
) -> None:
    manager = BrowserManager()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    try:
        session = await manager.open(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=_settings(),
        )
        await manager.navigate(
            session.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            url=f"{interactive_page_server}/form",
        )

        await manager.click(
            session.id,
            x=100,
            y=40,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        typed = await manager.type_text(
            session.id,
            text="hello",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

        assert typed.title == "hello"

        clicked = await manager.click(
            session.id,
            x=100,
            y=120,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

        assert clicked.url.endswith("/clicked")
        assert clicked.title == "Clicked"
    finally:
        await manager.shutdown()
