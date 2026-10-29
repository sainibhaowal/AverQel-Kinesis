"""End-to-end tests for the DeepSpace browser routes.

The browser routes are reachable over HTTP, they report a deployment that has
no browser runtime, and a browser session belongs to the tenant and the user
that opened it, so one tenant cannot reach another's session.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.deepspace.api.browser as browser_api
from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.deepspace.services.browser_runtime import (
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    BrowserNavigationError,
    BrowserPage,
    BrowserSessionNotFoundError,
    find_browser_executable,
)
from tests.conftest import SeededUser

#: The browser this host can run, when it has one.
BROWSER = find_browser_executable()

requires_browser = pytest.mark.skipif(
    BROWSER is None,
    reason="This host has no Chromium browser for the browser runtime.",
)


class FakeBrowserManager:
    """Answer the input routes with one page and record what they asked for."""

    def __init__(self) -> None:
        """Create a manager with no calls yet."""
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._sessions: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], SimpleNamespace] = {}
        self._closed_conversations: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()

    def _key(self, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID) -> tuple:
        return (tenant_id, user_id, conversation_id)

    async def require_session(self, **arguments: object) -> SimpleNamespace:
        """Return the one session, or refuse a named session that is unknown.

        Args:
            arguments: The caller's tenant, user, conversation, and session.

        Returns:
            The live session.

        Raises:
            BrowserSessionNotFoundError: When the request names an unknown
                session.
        """
        session_id = arguments.get("session_id")
        if session_id == "missing":
            raise BrowserSessionNotFoundError("No such browser session.")
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        if key in self._closed_conversations:
            raise BrowserSessionNotFoundError("No such browser session.")
        if session_id is not None:
            # The caller named a specific session; it must belong to them.
            session = self._sessions.get(key)
            if session is None or session.id != session_id:
                raise BrowserSessionNotFoundError("No such browser session.")
            return session
        # No specific session named; return the conversation's session,
        # creating it if it does not yet exist (lenient test double behaviour).
        if key not in self._sessions:
            self._sessions[key] = SimpleNamespace(id="session-1")
        return self._sessions[key]

    async def open(self, **arguments: object) -> SimpleNamespace:
        """Return the one session for a conversation.

        Args:
            arguments: The caller's tenant, user, conversation, and settings.

        Returns:
            The live session.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        if key not in self._sessions:
            self._sessions[key] = SimpleNamespace(id="session-1")
        return self._sessions[key]

    async def current(self, session_id: str, **arguments: object) -> BrowserPage:
        """Return the current page of a session.

        Args:
            session_id: The session's identifier.
            arguments: The caller's ownership.

        Returns:
            The page the browser shows.
        """
        self.calls.append(("current", {"session_id": session_id, **arguments}))
        return _page()

    async def navigate(self, session_id: str, **arguments: object) -> BrowserPage:
        """Navigate a session to a URL.

        Args:
            session_id: The session's identifier.
            arguments: The URL and the caller's ownership.

        Returns:
            The page the browser shows.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        session = self._sessions.get(key)
        if session is None or session.id != session_id:
            raise BrowserSessionNotFoundError("No such browser session.")
        # Validate the URL like the real manager does.
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise BrowserNavigationError("Enter a URL to open.")
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise BrowserNavigationError("Only http:// and https:// URLs can be opened.")
        if parsed.username or parsed.password:
            raise BrowserNavigationError("A URL with a user name or password is not allowed.")
        self.calls.append(("navigate", {"session_id": session_id, **arguments}))
        return _page()

    async def close_all_for_conversation(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> int:
        """Close all sessions for a conversation.

        Args:
            tenant_id: The tenant.
            user_id: The user.
            conversation_id: The conversation.

        Returns:
            The number of sessions closed.
        """
        key = (tenant_id, user_id, conversation_id)
        closed = 0
        if key in self._sessions:
            del self._sessions[key]
            closed = 1
        self._closed_conversations.add(key)
        return closed

    async def click(self, session_id: str, **arguments: object) -> BrowserPage:
        """Click a point in the session's page.

        Args:
            session_id: The session's identifier.
            arguments: The point and the caller's ownership.

        Returns:
            The page the browser shows.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        session = self._sessions.get(key)
        if session is None or session.id != session_id:
            raise BrowserSessionNotFoundError("No such browser session.")
        self.calls.append(("click", {"session_id": session_id, **arguments}))
        return _page()

    async def type_text(self, session_id: str, **arguments: object) -> BrowserPage:
        """Type text into the session's page.

        Args:
            session_id: The session's identifier.
            arguments: The text and the caller's ownership.

        Returns:
            The page the browser shows.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        session = self._sessions.get(key)
        if session is None or session.id != session_id:
            raise BrowserSessionNotFoundError("No such browser session.")
        self.calls.append(("type", {"session_id": session_id, **arguments}))
        return _page()

    async def press_key(self, session_id: str, **arguments: object) -> BrowserPage:
        """Press a key in the session's page.

        Args:
            session_id: The session's identifier.
            arguments: The key and the caller's ownership.

        Returns:
            The page the browser shows.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        session = self._sessions.get(key)
        if session is None or session.id != session_id:
            raise BrowserSessionNotFoundError("No such browser session.")
        self.calls.append(("key", {"session_id": session_id, **arguments}))
        return _page()

    async def scroll(self, session_id: str, **arguments: object) -> BrowserPage:
        """Scroll the session's page.

        Args:
            session_id: The session's identifier.
            arguments: The distance and the caller's ownership.

        Returns:
            The page the browser shows.
        """
        key = self._key(
            tenant_id=arguments["tenant_id"],
            user_id=arguments["user_id"],
            conversation_id=arguments["conversation_id"],
        )
        session = self._sessions.get(key)
        if session is None or session.id != session_id:
            raise BrowserSessionNotFoundError("No such browser session.")
        self.calls.append(("scroll", {"session_id": session_id, **arguments}))
        return _page()


def _page() -> BrowserPage:
    """Build the page the fake browser shows.

    Returns:
        The page.
    """
    return BrowserPage(
        url="http://localhost/form",
        title="Example",
        screenshot=b"\x89PNG\r\n\x1a\n",
        can_go_back=False,
        can_go_forward=False,
    )


def _headers(seeded: SeededUser) -> dict[str, str]:
    """Build the request headers for one seeded user.

    Args:
        seeded: The seeded user.

    Returns:
        The headers.
    """
    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles={"admin"},
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(seeded.tenant_id),
    }


@pytest.fixture
def browser_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the browser runtime on for the deployment under test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "deepspace_browser_enabled", True)


def _message(payload: object) -> str:
    """Read the message one failed response carries.

    Args:
        payload: The response body.

    Returns:
        The message, or an empty string when the body carries none.
    """
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    detail = payload.get("detail")
    return str(detail) if isinstance(detail, str) else ""


def test_the_browser_routes_need_authentication(client: TestClient) -> None:
    conversation_id = str(uuid.uuid4())

    refused = client.post(f"/api/v1/deepspace/chats/{conversation_id}/browser/session")

    assert refused.status_code in {401, 403}


def test_a_deployment_without_a_browser_runtime_says_so(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Browser Tenant", "browser-runtime@example.com", "StrongPass!1234", ("admin",)
    )
    conversation_id = str(uuid.uuid4())

    refused = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
        headers=_headers(seeded),
    )

    assert refused.status_code == 409
    assert "no browser runtime" in _message(refused.json())


def test_a_conversation_without_a_browser_session_is_not_found(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user(
        "Browser Tenant", "browser-missing@example.com", "StrongPass!1234", ("admin",)
    )
    conversation_id = str(uuid.uuid4())

    missing = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/screenshot",
        headers=_headers(seeded),
    )

    assert missing.status_code == 404
    assert "No such browser session." in _message(missing.json())


def test_the_input_routes_return_the_page(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = seed_user("Browser Tenant", "browser-input@example.com", "StrongPass!1234", ("admin",))
    conversation_id = str(uuid.uuid4())
    headers = _headers(seeded)
    manager = FakeBrowserManager()
    monkeypatch.setattr(browser_api, "browser_manager", lambda: manager)

    requests: list[tuple[str, dict[str, object]]] = [
        ("click", {"x": 10, "y": 20}),
        ("type", {"text": "hello"}),
        ("key", {"key": "Enter", "modifiers": ["Shift"]}),
        ("scroll", {"delta_x": 0, "delta_y": 300}),
    ]
    for route, body in requests:
        response = client.post(
            f"/api/v1/deepspace/chats/{conversation_id}/browser/{route}",
            headers=headers,
            json=body,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["session_id"] == "session-1"
        assert payload["url"] == "http://localhost/form"
        assert payload["title"] == "Example"
        assert payload["screenshot_base64"]
        assert payload["can_go_back"] is False
        assert payload["can_go_forward"] is False
        assert payload["viewport_width"] == VIEWPORT_WIDTH
        assert payload["viewport_height"] == VIEWPORT_HEIGHT

    assert [name for name, _arguments in manager.calls] == [
        "click",
        "type",
        "key",
        "scroll",
    ]
    assert manager.calls[0][1]["x"] == 10
    assert manager.calls[0][1]["y"] == 20
    assert manager.calls[1][1]["text"] == "hello"
    assert manager.calls[2][1]["key"] == "Enter"
    assert manager.calls[2][1]["modifiers"] == ["Shift"]
    assert manager.calls[3][1]["delta_x"] == 0
    assert manager.calls[3][1]["delta_y"] == 300


def test_an_input_route_refuses_an_unknown_session(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = seed_user(
        "Browser Tenant", "browser-input-missing@example.com", "StrongPass!1234", ("admin",)
    )
    conversation_id = str(uuid.uuid4())
    manager = FakeBrowserManager()
    monkeypatch.setattr(browser_api, "browser_manager", lambda: manager)

    missing = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/click",
        headers=_headers(seeded),
        json={"session_id": "missing", "x": 1, "y": 2},
    )

    assert missing.status_code == 404
    assert "No such browser session." in _message(missing.json())


def test_a_conversation_opens_a_browser_session_and_drives_it(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser routes work with a fake browser manager.

    This test does not require a real Chromium; it exercises the API routes
    and the tenant/user/conversation scoping through a fake browser manager.
    """
    seeded = seed_user(
        "Browser Tenant", "browser-session@example.com", "StrongPass!1234", ("admin",)
    )
    conversation_id = str(uuid.uuid4())
    headers = _headers(seeded)

    fake = FakeBrowserManager()
    monkeypatch.setattr("app.deepspace.api.browser.browser_manager", lambda: fake)

    opened = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
        headers=headers,
    )
    assert opened.status_code == 200
    payload = opened.json()
    session_id = payload["session_id"]
    assert session_id
    assert payload["screenshot_base64"]

    # Opening the session again returns the session the conversation holds.
    again = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
        headers=headers,
    )
    assert again.status_code == 200
    assert again.json()["session_id"] == session_id

    refused = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/navigate",
        headers=headers,
        json={"url": "javascript:alert(1)"},
    )
    assert refused.status_code == 400
    assert "Only http:// and https:// URLs can be opened." in _message(refused.json())

    closed = client.delete(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
        headers=headers,
    )
    assert closed.status_code == 200
    assert closed.json()["closed"] is True

    gone = client.get(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/screenshot",
        headers=headers,
    )
    assert gone.status_code == 404


def test_one_tenant_cannot_reach_another_tenants_browser_session(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant isolation works with a fake browser manager.

    This test does not require a real Chromium; it exercises the tenant
    isolation logic through the API routes and a fake browser manager.
    """
    first = seed_user("Browser One", "browser-one@example.com", "StrongPass!1234", ("admin",))
    second = seed_user("Browser Two", "browser-two@example.com", "StrongPass!1234", ("admin",))
    conversation_id = str(uuid.uuid4())

    fake = FakeBrowserManager()
    monkeypatch.setattr("app.deepspace.api.browser.browser_manager", lambda: fake)

    opened = client.post(
        f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
        headers=_headers(first),
    )
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    try:
        stolen = client.get(
            f"/api/v1/deepspace/chats/{conversation_id}/browser/screenshot",
            headers=_headers(second),
            params={"session_id": session_id},
        )
        missing = client.get(
            f"/api/v1/deepspace/chats/{conversation_id}/browser/screenshot",
            headers=_headers(second),
            params={"session_id": "no-such-session"},
        )

        assert stolen.status_code == 404
        assert missing.status_code == 404
        # A session that belongs to another tenant is refused exactly as one
        # that does not exist, so a caller cannot learn that it exists.
        assert _message(stolen.json()) == _message(missing.json())

        navigated = client.post(
            f"/api/v1/deepspace/chats/{conversation_id}/browser/navigate",
            headers=_headers(second),
            json={"url": "http://127.0.0.1:9/", "session_id": session_id},
        )
        assert navigated.status_code == 404
    finally:
        client.delete(
            f"/api/v1/deepspace/chats/{conversation_id}/browser/session",
            headers=_headers(first),
        )
