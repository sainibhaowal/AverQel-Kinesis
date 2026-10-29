"""The browser panel routes for DeepSpace.

The panel's Browser tab drives a real browser.  Every route here is scoped to
the tenant, the user, and the conversation the request belongs to, so a
request can never reach another conversation's browser session.

A conversation holds one browser session.  The session route opens it and
returns it, the navigation routes drive the browser's own history, the input
routes click, type, press a key, and scroll, and the screenshot route reads the
page the browser is showing.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.deepspace.services.browser_runtime import (
    MAX_SCROLL_DELTA,
    MAX_TYPE_TEXT_LENGTH,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    BrowserNavigationError,
    BrowserPage,
    BrowserProtocolError,
    BrowserRuntimeError,
    BrowserSessionLimitError,
    BrowserSessionNotFoundError,
    BrowserUnavailableError,
    browser_manager,
)

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-browser"])


class BrowserNavigationRequest(BaseModel):
    """The address a navigation request opens."""

    #: The address to open.
    url: str = Field(min_length=1, max_length=4_096)
    #: The session to drive, when the caller names one.
    session_id: str | None = Field(default=None, max_length=128)


class BrowserSessionRequest(BaseModel):
    """The session a request drives, when the caller names one."""

    #: The session to drive.
    session_id: str | None = Field(default=None, max_length=128)


class BrowserClickRequest(BaseModel):
    """The point a click request presses."""

    #: The session to drive, when the caller names one.
    session_id: str | None = Field(default=None, max_length=128)
    #: The horizontal position in the page's viewport, in pixels.
    x: float = Field(ge=0, lt=VIEWPORT_WIDTH)
    #: The vertical position in the page's viewport, in pixels.
    y: float = Field(ge=0, lt=VIEWPORT_HEIGHT)


class BrowserTypeRequest(BaseModel):
    """The text a typing request inserts."""

    #: The session to drive, when the caller names one.
    session_id: str | None = Field(default=None, max_length=128)
    #: The text to insert.
    text: str = Field(min_length=1, max_length=MAX_TYPE_TEXT_LENGTH)


class BrowserKeyRequest(BaseModel):
    """The key a key request presses."""

    #: The session to drive, when the caller names one.
    session_id: str | None = Field(default=None, max_length=128)
    #: The key to press.
    key: str = Field(min_length=1, max_length=32)
    #: The modifier keys held while the key is pressed.
    modifiers: list[str] = Field(default_factory=list, max_length=8)


class BrowserScrollRequest(BaseModel):
    """The distance a scroll request moves the page."""

    #: The session to drive, when the caller names one.
    session_id: str | None = Field(default=None, max_length=128)
    #: The horizontal distance to scroll, in pixels.
    delta_x: float = Field(ge=-MAX_SCROLL_DELTA, le=MAX_SCROLL_DELTA)
    #: The vertical distance to scroll, in pixels.
    delta_y: float = Field(ge=-MAX_SCROLL_DELTA, le=MAX_SCROLL_DELTA)


def _page_payload(page: BrowserPage, *, session_id: str) -> dict[str, Any]:
    """Build the answer that carries one page.

    Args:
        page: The page the browser is showing.
        session_id: The session the page belongs to.

    Returns:
        The answer's fields.
    """
    return {
        "session_id": session_id,
        "url": page.url,
        "title": page.title,
        "screenshot_base64": base64.b64encode(page.screenshot).decode("ascii"),
        "can_go_back": page.can_go_back,
        "can_go_forward": page.can_go_forward,
        "viewport_width": VIEWPORT_WIDTH,
        "viewport_height": VIEWPORT_HEIGHT,
    }


def _fail(exc: Exception) -> HTTPException:
    """Turn a runtime failure into the answer the caller receives.

    Args:
        exc: The failure the runtime reported.

    Returns:
        The error to raise.
    """
    if isinstance(exc, BrowserSessionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BrowserSessionLimitError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, BrowserNavigationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, BrowserUnavailableError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, BrowserProtocolError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/{conversation_id}/browser/session",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def open_browser_session(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Open the conversation's browser session, or return the one it holds."""
    manager = browser_manager()
    try:
        session = await manager.open(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        page = await manager.current(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/navigate",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def navigate_browser(
    conversation_id: uuid.UUID,
    payload: BrowserNavigationRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Open an address in the conversation's browser."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id,
        )
        page = await manager.navigate(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            url=payload.url,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/back",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def go_back_in_browser(
    conversation_id: uuid.UUID,
    payload: BrowserSessionRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Move to the earlier page in the browser's own history."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id if payload else None,
        )
        page = await manager.back(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/forward",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def go_forward_in_browser(
    conversation_id: uuid.UUID,
    payload: BrowserSessionRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Move to the later page in the browser's own history."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id if payload else None,
        )
        page = await manager.forward(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/reload",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def reload_browser(
    conversation_id: uuid.UUID,
    payload: BrowserSessionRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Load the conversation's page again."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id if payload else None,
        )
        page = await manager.reload(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/click",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def click_in_browser(
    conversation_id: uuid.UUID,
    payload: BrowserClickRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Click one point of the conversation's browser page."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id,
        )
        page = await manager.click(
            session.id,
            x=payload.x,
            y=payload.y,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/type",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def type_in_browser(
    conversation_id: uuid.UUID,
    payload: BrowserTypeRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Insert text into the focused field of the conversation's browser page."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id,
        )
        page = await manager.type_text(
            session.id,
            text=payload.text,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/key",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def press_key_in_browser(
    conversation_id: uuid.UUID,
    payload: BrowserKeyRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Press one key in the conversation's browser page."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id,
        )
        page = await manager.press_key(
            session.id,
            key=payload.key,
            modifiers=payload.modifiers,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.post(
    "/{conversation_id}/browser/scroll",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def scroll_browser(
    conversation_id: uuid.UUID,
    payload: BrowserScrollRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Scroll the conversation's browser page."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=payload.session_id,
        )
        page = await manager.scroll(
            session.id,
            delta_x=payload.delta_x,
            delta_y=payload.delta_y,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.get(
    "/{conversation_id}/browser/screenshot",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def read_browser_screenshot(
    conversation_id: uuid.UUID,
    session_id: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Read the screenshot of the page the conversation's browser is showing."""
    manager = browser_manager()
    try:
        session = await manager.require_session(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        page = await manager.current(
            session.id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
    except BrowserRuntimeError as exc:
        raise _fail(exc) from exc
    return _page_payload(page, session_id=session.id)


@router.delete(
    "/{conversation_id}/browser/session",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def close_browser_session(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    """Close the conversation's browser session."""
    manager = browser_manager()
    closed = await manager.close_all_for_conversation(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    return {"closed": closed > 0}


async def _close_browser_runtime() -> None:
    """Stop the browser when the application shuts down."""
    await browser_manager().shutdown()


router.add_event_handler("shutdown", _close_browser_runtime)


__all__ = ["router"]
