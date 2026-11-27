"""Real Browser Automation for DeepSpace using Playwright.

Provides a real browser (Playwright) driven as an external process with full tool bindings.
This is the "real browser" alternative to the embedded browser panel.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from app.core.config import Settings
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    boolean_property,
    define_native_tool,
    integer_property,
    object_property,
    string_property,
)

logger = logging.getLogger(__name__)

_BROWSER_DISPLAY_ENVIRONMENT = (
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "DBUS_SESSION_BUS_ADDRESS",
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

_playwright_factory: Any = None
try:
    from playwright.async_api import async_playwright as _imported_async_playwright

    _playwright_factory = _imported_async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BrowserAutomationConfig:
    """Browser automation runtime configuration."""

    enabled: bool = True
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    default_timeout_ms: int = 30_000
    max_timeout_ms: int = 300_000
    screenshot_on_error: bool = True
    record_video: bool = False
    video_dir: str | None = None
    downloads_dir: str | None = None
    user_agent: str | None = None
    locale: str = "en-US"
    timezone_id: str = "UTC"
    permissions: tuple[str, ...] = ("geolocation", "notifications")
    extra_args: tuple[str, ...] = ()
    proxy: dict[str, str] | None = None
    cdp_endpoint: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Browser Session State
# ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class BrowserSession:
    """Active browser session state."""

    session_id: str
    conversation_id: UUID
    browser_type: str
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))


# ──────────────────────────────────────────────────────────────────────
# Browser Automation Runtime (Real Playwright Implementation)
# ──────────────────────────────────────────────────────────────────────


class BrowserAutomationRuntime:
    """Manages a Playwright browser instance with CDP-like tool interface."""

    def __init__(self, config: BrowserAutomationConfig, settings: Settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright not installed. Run: pip install playwright && playwright install chromium"
            )

        self.config = config
        self.settings = settings
        self._playwright: Playwright | None = None
        self._session: BrowserSession | None = None
        self._managed_browser_server: Any = None
        self._browser_profile_dir: Path | None = None
        self._attached_external_cdp: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock: asyncio.Lock | None = None

    def _get_start_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._start_lock is None or self._loop is not loop:
            self._loop = loop
            self._start_lock = asyncio.Lock()
        return self._start_lock

    async def start(
        self,
        conversation_id: UUID,
        *,
        storage_state: dict[str, Any] | None = None,
    ) -> BrowserSession:
        """Start a new browser automation session."""
        if self._session is not None:
            await self.stop()

        from app.deepspace.services.browser_runtime import (
            ManagedPlaywrightServer,
            _browser_profile_dir,
            build_browser_environment,
            find_browser_executable,
        )

        playwright = await _playwright_factory().start()
        self._playwright = playwright
        profile_dir: Path | None = None
        try:
            cdp_endpoint = (self.config.cdp_endpoint or "").strip()
            if cdp_endpoint and self.config.browser_type == "chromium":
                browser = await playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=30_000,
                )
                self._attached_external_cdp = True
                contexts = list(getattr(browser, "contexts", ()) or ())
                context = contexts[0] if contexts else await browser.new_context()
                pages = list(getattr(context, "pages", ()) or ())
                page = pages[0] if pages else await context.new_page()
            else:
                self._attached_external_cdp = False
                profile_dir = _browser_profile_dir()
                self._browser_profile_dir = profile_dir
                mode = str(
                    getattr(self.settings, "deepspace_sandbox_policy", None)
                    or getattr(self.settings, "deepspace_permission_mode", "workspace-write")
                )
                launch_options: dict[str, Any] = {"headless": self.config.headless}
                if self.config.extra_args:
                    launch_options["args"] = list(self.config.extra_args)
                if self.config.proxy:
                    launch_options["proxy"] = dict(self.config.proxy)
                if self.config.browser_type == "chromium":
                    executable_path = find_browser_executable()
                    if executable_path:
                        launch_options["executablePath"] = executable_path

                environment = build_browser_environment(profile_dir, mode=mode)
                if not self.config.headless:
                    for name in _BROWSER_DISPLAY_ENVIRONMENT:
                        value = os.environ.get(name)
                        if value:
                            environment[name] = value
                self._managed_browser_server = await ManagedPlaywrightServer.start(
                    browser_type=self.config.browser_type,
                    launch_options=launch_options,
                    environment=environment,
                )
                browser_type = getattr(self._playwright, self.config.browser_type)
                browser = await browser_type.connect(
                    self._managed_browser_server.endpoint,
                    timeout=30_000,
                )

                # Create context with viewport and settings
                context_kwargs: dict[str, Any] = {
                    "viewport": {
                        "width": self.config.viewport_width,
                        "height": self.config.viewport_height,
                    },
                    "user_agent": self.config.user_agent,
                    "locale": self.config.locale,
                    "timezone_id": self.config.timezone_id,
                    "permissions": list(self.config.permissions),
                    "record_video_dir": self.config.video_dir,
                    "accept_downloads": True,
                }
                if storage_state is not None:
                    context_kwargs["storage_state"] = storage_state
                context = await browser.new_context(**context_kwargs)

                context.set_default_timeout(self.config.default_timeout_ms)
                context.set_default_navigation_timeout(self.config.default_timeout_ms)
                page = await context.new_page()

            self._session = BrowserSession(
                session_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                browser_type=self.config.browser_type,
                playwright=self._playwright,
                browser=browser,
                context=context,
                page=page,
            )
            logger.info(
                "Started browser automation session %s for conversation %s",
                self._session.session_id,
                conversation_id,
            )
            return self._session
        except BaseException:
            if self._managed_browser_server is not None:
                with contextlib.suppress(Exception):
                    await self._managed_browser_server.close()
                self._managed_browser_server = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None
            if profile_dir is not None:
                shutil.rmtree(profile_dir, ignore_errors=True)
            self._browser_profile_dir = None
            raise

    async def stop(self) -> None:
        """Stop the browser automation session."""
        try:
            if self._session:
                try:
                    if not self._attached_external_cdp:
                        if self._session.page:
                            await self._session.page.close()
                        if self._session.context:
                            await self._session.context.close()
                        if self._session.browser:
                            await self._session.browser.close()
                except Exception:
                    logger.warning("Error closing browser session", exc_info=True)

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    logger.warning("Error stopping Playwright client", exc_info=True)
                self._playwright = None
        finally:
            if self._managed_browser_server is not None:
                with contextlib.suppress(Exception):
                    await self._managed_browser_server.close()
                self._managed_browser_server = None
            if self._browser_profile_dir is not None:
                shutil.rmtree(self._browser_profile_dir, ignore_errors=True)
                self._browser_profile_dir = None
            self._session = None
        logger.info("Browser automation session stopped")

    async def ensure_started(self, conversation_id: UUID) -> BrowserSession:
        """Start the session on first use, serializing concurrent tool calls."""
        async with self._get_start_lock():
            if not self.is_running:
                return await self.start(conversation_id)
            assert self._session is not None
            self._session.last_activity = datetime.now(UTC)
            return self._session

    async def sync_storage_state(self, storage_state: dict[str, Any]) -> None:
        """Merge cookies and origin localStorage from the sidebar browser into this runtime."""
        session = self._session
        if session is None or not self.is_running:
            return
        cookies = storage_state.get("cookies")
        if isinstance(cookies, list) and cookies:
            try:
                await session.context.add_cookies(cookies)
            except Exception:
                # Fall back per-cookie so one invalid cookie never drops the rest.
                for cookie in cookies:
                    with contextlib.suppress(Exception):
                        await session.context.add_cookies([cookie])

        origins = storage_state.get("origins")
        if isinstance(origins, list) and origins:
            synced_origins: dict[str, dict[str, str]] = getattr(self, "_synced_origins", {})
            self._synced_origins = synced_origins
            for entry in origins:
                if not isinstance(entry, dict):
                    continue
                origin = entry.get("origin")
                items = entry.get("localStorage")
                if (
                    not isinstance(origin, str)
                    or not origin
                    or not isinstance(items, list)
                    or not items
                ):
                    continue
                mapping = {
                    str(item["name"]): str(item["value"])
                    for item in items
                    if isinstance(item, dict) and "name" in item and "value" in item
                }
                if not mapping or synced_origins.get(origin) == mapping:
                    continue
                synced_origins[origin] = mapping
                script = (
                    "(() => {"
                    f"  if (window.location.origin !== {json.dumps(origin)}) return;"
                    f"  const entries = {json.dumps(mapping)};"
                    "  for (const [k, v] of Object.entries(entries)) {"
                    "    if (window.localStorage.getItem(k) === null) window.localStorage.setItem(k, v);"
                    "  }"
                    "})();"
                )
                with contextlib.suppress(Exception):
                    await session.context.add_init_script(script)
                if session.page is not None and str(session.page.url or "").startswith(origin):
                    with contextlib.suppress(Exception):
                        await session.page.evaluate(
                            "entries => {"
                            "  for (const [k, v] of Object.entries(entries)) window.localStorage.setItem(k, v);"
                            "}",
                            mapping,
                        )

        active_url = storage_state.get("active_url")
        if (
            isinstance(active_url, str)
            and active_url.startswith(("http://", "https://"))
            and session.page is not None
            and (not session.page.url or session.page.url == "about:blank")
        ):
            with contextlib.suppress(Exception):
                await session.page.goto(active_url, wait_until="load", timeout=15_000)
                with contextlib.suppress(Exception):
                    await session.page.wait_for_load_state("networkidle", timeout=4_000)

    @property
    def is_running(self) -> bool:
        return self._session is not None and self._session.page is not None

    @property
    def page(self) -> Page | None:
        return self._session.page if self._session else None

    @property
    def context(self) -> BrowserContext | None:
        return self._session.context if self._session else None

    @property
    def browser(self) -> Browser | None:
        return self._session.browser if self._session else None

    async def _ensure_session(self) -> BrowserSession:
        """Ensure a session is running, raise if not."""
        session = self._session
        if session is None or session.page is None:
            raise RuntimeError("Browser automation not running. Start a session first.")
        return session

    # ──────────────────────────────────────────────────────────────────
    # High-level Tool Operations
    # ──────────────────────────────────────────────────────────────────

    async def navigate(
        self, url: str, wait_until: str = "networkidle", timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Navigate to a URL."""
        from app.deepspace.services.browser_runtime import validate_browser_url

        url = validate_browser_url(url)
        session = await self._ensure_session()
        response = await session.page.goto(url, wait_until=wait_until)
        return {
            "success": True,
            "url": session.page.url,
            "status": response.status if response else None,
        }

    async def click(
        self,
        selector: str,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Click an element."""
        session = await self._ensure_session()
        await session.page.click(
            selector,
            button=button,
            click_count=click_count,
            delay=delay_ms,
        )
        return {"success": True}

    async def type_text(
        self, selector: str, text: str, delay_ms: int = 0, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Type text into an element."""
        session = await self._ensure_session()
        await session.page.fill(
            selector,
            "",
        )
        await session.page.type(
            selector,
            text,
            delay=delay_ms,
        )
        return {"success": True}

    async def fill(
        self, selector: str, value: str, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Fill an input field."""
        session = await self._ensure_session()
        await session.page.fill(
            selector,
            value,
        )
        return {"success": True}

    async def select_option(
        self, selector: str, values: list[str], timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Select option(s) from a dropdown."""
        session = await self._ensure_session()
        await session.page.select_option(
            selector,
            value=values,
        )
        return {"success": True}

    async def hover(self, selector: str, timeout_ms: int | None = None) -> dict[str, Any]:
        """Hover over an element."""
        session = await self._ensure_session()
        await session.page.hover(
            selector,
        )
        return {"success": True}

    async def wait_for(
        self,
        wait_for: str = "networkidle",
        selector: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Wait for navigation or element."""
        session = await self._ensure_session()
        if selector:
            await session.page.wait_for_selector(
                selector,
            )
        else:
            await session.page.wait_for_load_state(
                wait_for,
            )
        return {"success": True}

    async def screenshot(
        self,
        full_page: bool = False,
        selector: str | None = None,
        image_type: str = "png",
        quality: int | None = None,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        clip: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Take a screenshot at the requested viewport, element, clip, or full-page size."""
        session = await self._ensure_session()
        previous_viewport = session.page.viewport_size
        resized = False
        if viewport_width is not None and viewport_height is not None:
            await session.page.set_viewport_size(
                {"width": int(viewport_width), "height": int(viewport_height)}
            )
            resized = True
        try:
            options: dict[str, Any] = {"type": image_type, "full_page": full_page}
            if quality is not None:
                options["quality"] = quality
            if clip is not None and not selector and not full_page:
                options["clip"] = {
                    "x": float(clip["x"]),
                    "y": float(clip["y"]),
                    "width": float(clip["width"]),
                    "height": float(clip["height"]),
                }
            if selector:
                element = await session.page.wait_for_selector(selector)
                element_options: dict[str, Any] = {"type": image_type}
                if quality is not None:
                    element_options["quality"] = quality
                image_bytes = await element.screenshot(**element_options)
            else:
                image_bytes = await session.page.screenshot(**options)
        finally:
            if resized and previous_viewport is not None:
                with contextlib.suppress(Exception):
                    await session.page.set_viewport_size(previous_viewport)
        b64 = base64.b64encode(image_bytes).decode()
        return {
            "image_base64": b64,
            "format": image_type,
            "bytes": len(image_bytes),
        }

    async def evaluate(
        self,
        expression: str,
        arg: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate JavaScript in the page context.

        Playwright applies the page's default timeout here, because
        ``Page.evaluate`` has no per-call timeout parameter.
        """
        session = await self._ensure_session()
        result = await session.page.evaluate(expression, arg)
        return {"result": result}

    async def get_content(
        self, format: str = "html", selector: str | None = None
    ) -> dict[str, Any]:
        """Get page content (HTML, text, or JSON)."""
        session = await self._ensure_session()
        if selector:
            element = await session.page.wait_for_selector(selector)
            content = await element.inner_html() if format == "html" else await element.inner_text()
        else:
            if format == "html":
                content = await session.page.content()
            elif format == "text":
                content = await session.page.evaluate("() => document.body.innerText")
            else:
                content = await session.page.evaluate(
                    "() => JSON.stringify({html: document.documentElement.outerHTML, text: document.body.innerText})"
                )
        return {"content": content, "format": format}

    async def pdf(self, **options: Any) -> dict[str, Any]:
        """Generate PDF of the page."""
        session = await self._ensure_session()
        pdf_bytes = await session.page.pdf(**options)
        b64 = base64.b64encode(pdf_bytes).decode()
        return {"pdf_base64": b64}

    async def new_tab(self, url: str | None = None) -> dict[str, Any]:
        """Open a new tab."""
        session = await self._ensure_session()
        new_page = await session.context.new_page()
        if url:
            from app.deepspace.services.browser_runtime import validate_browser_url

            url = validate_browser_url(url)
            await new_page.goto(url, wait_until="networkidle")
        # Switch to new page
        session.page = new_page
        return {"success": True, "page_index": len(session.context.pages) - 1}

    async def switch_tab(self, index: int) -> dict[str, Any]:
        """Switch to a tab by index."""
        session = await self._ensure_session()
        pages = session.context.pages
        if 0 <= index < len(pages):
            session.page = pages[index]
            return {"success": True, "page_index": index}
        raise ValueError(f"Invalid tab index: {index}")

    async def close_tab(self, index: int | None = None) -> dict[str, Any]:
        """Close current or specified tab."""
        session = await self._ensure_session()
        pages = session.context.pages
        if index is None:
            # Close current page
            await session.page.close()
            session.page = session.context.pages[-1] if session.context.pages else None
        else:
            if 0 <= index < len(pages):
                await pages[index].close()
            else:
                raise ValueError(f"Invalid tab index: {index}")
        return {"success": True}

    async def download_file(
        self, selector: str, save_as: str | None = None, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Trigger and wait for file download."""
        session = await self._ensure_session()
        download_path = self.config.downloads_dir or tempfile.gettempdir()
        async with session.page.expect_download(
            timeout=timeout_ms or self.config.max_timeout_ms
        ) as download_info:
            await session.page.click(selector)
        download = await download_info.value
        save_path = save_as or download.suggested_filename
        full_path = Path(download_path) / save_path
        await download.save_as(full_path)
        return {"success": True, "path": str(full_path), "filename": download.suggested_filename}

    async def drag_drop(
        self, source_selector: str, target_selector: str, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        """Drag and drop an element."""
        session = await self._ensure_session()
        await session.page.drag_and_drop(
            source_selector,
            target_selector,
        )
        return {"success": True}


# ──────────────────────────────────────────────────────────────────────
# Global Runtime Instance
# ──────────────────────────────────────────────────────────────────────

_BROWSER_RUNTIMES_BY_LOOP: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[UUID, BrowserAutomationRuntime]
] = WeakKeyDictionary()
_BROWSER_RUNTIMES_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    WeakKeyDictionary()
)


def _loop_runtimes() -> tuple[dict[UUID, BrowserAutomationRuntime], asyncio.Lock]:
    """Return the conversation->runtime map and lock bound to the active event loop."""
    loop = asyncio.get_running_loop()
    runtimes = _BROWSER_RUNTIMES_BY_LOOP.get(loop)
    if runtimes is None:
        runtimes = {}
        _BROWSER_RUNTIMES_BY_LOOP[loop] = runtimes
    lock = _BROWSER_RUNTIMES_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _BROWSER_RUNTIMES_LOCKS[loop] = lock
    return runtimes, lock


def _config_for(settings: Settings) -> BrowserAutomationConfig:
    """Build the browser runtime configuration from deployment settings."""
    return BrowserAutomationConfig(
        enabled=bool(getattr(settings, "deepspace_browser_automation_enabled", True)),
        browser_type=getattr(settings, "deepspace_browser_automation_browser_type", "chromium"),
        headless=bool(getattr(settings, "deepspace_browser_automation_headless", True)),
        viewport_width=int(getattr(settings, "deepspace_browser_automation_viewport_width", 1280)),
        viewport_height=int(getattr(settings, "deepspace_browser_automation_viewport_height", 720)),
        default_timeout_ms=int(
            getattr(settings, "deepspace_browser_automation_default_timeout_ms", 30_000)
        ),
        max_timeout_ms=int(
            getattr(settings, "deepspace_browser_automation_max_timeout_ms", 300_000)
        ),
        record_video=bool(getattr(settings, "deepspace_browser_automation_record_video", False)),
        video_dir=getattr(settings, "deepspace_browser_automation_video_dir", None),
        downloads_dir=getattr(settings, "deepspace_browser_automation_downloads_dir", None),
        user_agent=getattr(settings, "deepspace_browser_automation_user_agent", None),
        locale=str(getattr(settings, "deepspace_browser_automation_locale", "en-US")),
        timezone_id=str(getattr(settings, "deepspace_browser_automation_timezone_id", "UTC")),
        permissions=tuple(
            getattr(
                settings,
                "deepspace_browser_automation_permissions",
                ("geolocation", "notifications"),
            )
        ),
        extra_args=tuple(getattr(settings, "deepspace_browser_automation_extra_args", ())),
        proxy=getattr(settings, "deepspace_browser_automation_proxy", None),
        cdp_endpoint=getattr(settings, "deepspace_browser_automation_cdp_endpoint", None),
    )


def browser_automation_unavailable_reason(settings: Any) -> str | None:
    """Return why browser automation cannot start without launching a browser."""
    if not bool(getattr(settings, "deepspace_browser_automation_enabled", True)):
        return "Browser automation is disabled by the deployment setting."
    if not PLAYWRIGHT_AVAILABLE:
        return "The Playwright Python runtime is not installed."
    browser_type = str(getattr(settings, "deepspace_browser_automation_browser_type", "chromium"))
    if browser_type == "chromium":
        from app.deepspace.services.browser_runtime import find_browser_executable

        if find_browser_executable() is None:
            return "No Chromium browser is installed or bundled for Playwright."
    elif browser_type in {"firefox", "webkit"}:
        configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        browser_cache = (
            Path(configured_cache).expanduser()
            if configured_cache and configured_cache != "0"
            else Path.home() / ".cache" / "ms-playwright"
        )
        if not any(browser_cache.glob(f"{browser_type}-*")):
            return f"The Playwright {browser_type} browser is not installed or bundled."
    else:
        return f"Unsupported Playwright browser type: {browser_type}."
    return None


async def _load_sidebar_storage_state(context: ToolContext) -> dict[str, Any] | None:
    """Export the conversation's right-sidebar browser login state when available."""
    auth = getattr(context, "auth", None)
    tenant_id = getattr(auth, "tenant_id", None)
    user_id = getattr(auth, "user_id", None)
    conversation_id = getattr(context, "conversation_id", None)
    if tenant_id is None or user_id is None or conversation_id is None:
        return None
    try:
        from app.deepspace.services.browser_runtime import browser_manager

        live_state = await browser_manager().export_conversation_storage_state(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if live_state is not None:
            return live_state
    except Exception:
        logger.debug("Live sidebar browser storage state export skipped", exc_info=True)

    try:
        from app.deepspace.services.browser_storage_bridge import (
            load_conversation_storage_state,
        )

        return load_conversation_storage_state(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.debug("Encrypted sidebar storage state fallback skipped", exc_info=True)
        return None


async def _get_runtime(context: ToolContext) -> BrowserAutomationRuntime:
    """Return the browser runtime owned by one conversation, starting it if needed.

    Each conversation owns a separate browser context so two conversations
    cannot navigate, click, or close each other's page.

    Args:
        context: The tool call's conversation and deployment settings.

    Returns:
        The running runtime for this conversation.

    Raises:
        RuntimeError: Playwright is missing or browser automation is disabled.
    """
    unavailable_reason = browser_automation_unavailable_reason(context.settings)
    if unavailable_reason:
        raise RuntimeError(unavailable_reason)
    conversation_id = context.conversation_id
    runtimes, lock = _loop_runtimes()
    async with lock:
        runtime = runtimes.get(conversation_id)
        if runtime is None:
            config = _config_for(context.settings)
            if not config.enabled:
                raise RuntimeError(
                    "Browser automation is disabled. Set "
                    "DEEPSPACE_BROWSER_AUTOMATION_ENABLED=true."
                )
            runtime = BrowserAutomationRuntime(config, context.settings)
            runtimes[conversation_id] = runtime
    await runtime.ensure_started(conversation_id)
    storage_state = await _load_sidebar_storage_state(context)
    if storage_state is not None:
        sync_fn = getattr(runtime, "sync_storage_state", None)
        if callable(sync_fn):
            await sync_fn(storage_state)
    return runtime


async def shutdown_browser_runtimes() -> None:
    """Close every conversation's browser on the running event loop; used at shutdown."""
    runtimes_map, lock = _loop_runtimes()
    async with lock:
        runtimes = list(runtimes_map.values())
        runtimes_map.clear()
    for runtime in runtimes:
        await runtime.stop()


# ──────────────────────────────────────────────────────────────────────
# Tool Handlers
# ──────────────────────────────────────────────────────────────────────


async def _browser_navigate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    from app.deepspace.services.browser_runtime import (
        BrowserNavigationError,
        validate_browser_url,
    )

    try:
        url = validate_browser_url(str(arguments.get("url") or ""))
    except BrowserNavigationError as exc:
        return {
            "status": "invalid_input",
            "error_code": "BROWSER_URL_UNSUPPORTED",
            "message": str(exc),
            "recovery": (
                "browser_navigate opens HTTP(S) web pages only. Do not retry a file:// URL; "
                "use an authorized file or document reader for local files."
            ),
        }
    runtime = await _get_runtime(context)
    return await runtime.navigate(
        url,
        arguments.get("wait_until", "networkidle"),
        arguments.get("timeout_ms"),
    )


async def _browser_click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.click(
        arguments["selector"],
        arguments.get("button", "left"),
        arguments.get("click_count", 1),
        arguments.get("delay_ms", 0),
        arguments.get("timeout_ms"),
    )


async def _browser_type(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.type_text(
        arguments["selector"],
        arguments["text"],
        arguments.get("delay_ms", 0),
        arguments.get("timeout_ms"),
    )


async def _browser_fill(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.fill(
        arguments["selector"], arguments["value"], arguments.get("timeout_ms")
    )


async def _browser_select(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.select_option(
        arguments["selector"], arguments["values"], arguments.get("timeout_ms")
    )


async def _browser_hover(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.hover(arguments["selector"], arguments.get("timeout_ms"))


async def _browser_wait(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.wait_for(
        arguments.get("wait_for", "networkidle"),
        arguments.get("selector"),
        arguments.get("timeout_ms"),
    )


def _is_interstitial_or_blank(
    *,
    title: str = "",
    text: str = "",
    image_bytes_len: int | None = None,
) -> bool:
    """Return True when headless output is blank or an anti-bot interstitial."""
    lowered = f"{title}\n{text}".lower()
    if any(
        marker in lowered
        for marker in (
            "just a moment...",
            "checking your browser",
            "verify you are human",
            "enable javascript and cookies to continue",
        )
    ):
        return True
    if image_bytes_len is not None and image_bytes_len < 9_000:
        return True
    return False


async def _browser_screenshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    result = await runtime.screenshot(
        arguments.get("full_page", False),
        arguments.get("selector"),
        arguments.get("type", "png"),
        arguments.get("quality"),
        arguments.get("viewport_width"),
        arguments.get("viewport_height"),
        arguments.get("clip"),
    )
    if not arguments.get("selector") and arguments.get("clip") is None:
        title = ""
        if runtime.page is not None:
            with contextlib.suppress(Exception):
                title = str(await runtime.page.title() or "")
        if _is_interstitial_or_blank(
            title=title,
            image_bytes_len=int(result.get("bytes") or 0),
        ):
            sidebar_state = await _load_sidebar_storage_state(context)
            sidebar_b64 = (
                sidebar_state.get("screenshot_base64") if isinstance(sidebar_state, dict) else None
            )
            if isinstance(sidebar_b64, str) and sidebar_b64:
                result["image_base64"] = sidebar_b64
                result["format"] = "png"
                result["bytes"] = len(base64.b64decode(sidebar_b64))
                result["source"] = "sidebar_browser_snapshot"
                return result
    result["format"] = arguments.get("type", "png")
    return result


async def _browser_evaluate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.evaluate(
        arguments["expression"], arguments.get("arg"), arguments.get("timeout_ms")
    )


async def _browser_get_content(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    result = await runtime.get_content(arguments.get("format", "html"), arguments.get("selector"))
    if not arguments.get("selector"):
        content = str(result.get("content") or "")
        title = ""
        if runtime.page is not None:
            with contextlib.suppress(Exception):
                title = str(await runtime.page.title() or "")
        if not content.strip() or _is_interstitial_or_blank(title=title, text=content):
            sidebar_state = await _load_sidebar_storage_state(context)
            snapshot = (
                sidebar_state.get("text_snapshot") if isinstance(sidebar_state, dict) else None
            )
            if isinstance(snapshot, str) and snapshot.strip():
                return {
                    "url": (
                        sidebar_state.get("active_url") if isinstance(sidebar_state, dict) else None
                    )
                    or (runtime.page.url if runtime.page else None),
                    "content": snapshot,
                    "source": "sidebar_browser_snapshot",
                }
    return result


async def _browser_pdf(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.pdf(**{k: v for k, v in arguments.items() if k != "selector"})


async def _browser_new_tab(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.new_tab(arguments.get("url"))


async def _browser_switch_tab(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.switch_tab(arguments["index"])


async def _browser_close_tab(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.close_tab()


async def _browser_download(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.download_file(
        arguments["selector"], arguments.get("save_as"), arguments.get("timeout_ms")
    )


async def _browser_drag_drop(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.drag_drop(
        arguments["source_selector"], arguments["target_selector"], arguments.get("timeout_ms")
    )


# ──────────────────────────────────────────────────────────────────────
# Tool Arguments
# ──────────────────────────────────────────────────────────────────────


class BrowserNavigateArguments(ToolArguments):
    """Navigate the browser to a URL."""

    url: str = Field(
        min_length=1,
        max_length=2048,
        description="HTTP(S) web URL only. Never use file:// for local files.",
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] | None = None
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)

    @model_validator(mode="after")
    def validate_web_url(self) -> BrowserNavigateArguments:
        """Reject unsupported schemes before browser automation starts."""
        from app.deepspace.services.browser_runtime import (
            BrowserNavigationError,
            validate_browser_url,
        )

        try:
            validate_browser_url(self.url)
        except BrowserNavigationError as exc:
            raise ValueError(
                f"{exc} Browser automation accepts HTTP(S) web URLs only; use an authorized "
                "file or document reader for local files."
            ) from exc
        return self


class BrowserClickArguments(ToolArguments):
    """Click one element."""

    selector: str = Field(min_length=1, max_length=1024)
    button: Literal["left", "right", "middle"] | None = None
    click_count: int | None = Field(default=None, ge=1, le=3)
    delay_ms: int | None = Field(default=None, ge=0, le=5_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserTypeArguments(ToolArguments):
    """Type text into one element."""

    selector: str = Field(min_length=1, max_length=1024)
    text: str = Field(min_length=1, max_length=10_000)
    delay_ms: int | None = Field(default=None, ge=0, le=1_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserFillArguments(ToolArguments):
    """Replace the value of one input."""

    selector: str = Field(min_length=1, max_length=1024)
    value: str = Field(max_length=10_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserSelectArguments(ToolArguments):
    """Select options in a ``<select>``."""

    selector: str = Field(min_length=1, max_length=1024)
    values: list[str] = Field(min_length=1, max_length=50)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserHoverArguments(ToolArguments):
    """Hover one element."""

    selector: str = Field(min_length=1, max_length=1024)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserWaitArguments(ToolArguments):
    """Wait for a page event or element."""

    wait_for: Literal["load", "domcontentloaded", "networkidle"] | None = None
    selector: str | None = Field(default=None, max_length=1024)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserScreenshotClipArguments(ToolArguments):
    """Optional rectangular clip region for a screenshot."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0, le=8192)
    height: float = Field(gt=0, le=8192)


class BrowserScreenshotArguments(ToolArguments):
    """Capture the page, a custom viewport/clip size, or one element."""

    full_page: bool | None = None
    selector: str | None = Field(default=None, max_length=1024)
    type: Literal["png", "jpeg"] | None = None
    quality: int | None = Field(default=None, ge=0, le=100)
    viewport_width: int | None = Field(default=None, ge=16, le=4096)
    viewport_height: int | None = Field(default=None, ge=16, le=4096)
    clip: BrowserScreenshotClipArguments | None = None

    @model_validator(mode="after")
    def validate_screenshot_options(self) -> BrowserScreenshotArguments:
        if (self.viewport_width is None) != (self.viewport_height is None):
            raise ValueError("Provide both viewport_width and viewport_height together.")
        if self.clip is not None and self.full_page:
            raise ValueError("clip cannot be combined with full_page=true.")
        if self.clip is not None and self.selector:
            raise ValueError("clip cannot be combined with selector.")
        return self


class BrowserEvaluateArguments(ToolArguments):
    """Evaluate JavaScript in the page."""

    expression: str = Field(min_length=1, max_length=50_000)
    arg: dict[str, Any] | None = None
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserGetContentArguments(ToolArguments):
    """Read the page as HTML, text, or JSON."""

    format: Literal["html", "text", "json"] | None = None
    selector: str | None = Field(default=None, max_length=1024)


class BrowserPdfArguments(ToolArguments):
    """Render the page as a PDF."""

    format: Literal["A4", "A3", "A2", "A1", "A0", "Letter", "Legal", "Tabloid"] | None = None
    landscape: bool | None = None
    print_background: bool | None = None
    margin_top: str | None = None
    margin_bottom: str | None = None
    margin_left: str | None = None
    margin_right: str | None = None


class BrowserNewTabArguments(ToolArguments):
    """Open another tab."""

    url: str | None = Field(default=None, max_length=2048)


class BrowserSwitchTabArguments(ToolArguments):
    """Switch to an existing tab."""

    index: int = Field(ge=0, le=50)


class BrowserCloseTabArguments(ToolArguments):
    """Close the current tab."""


class BrowserDownloadArguments(ToolArguments):
    """Download a file through a page control."""

    selector: str = Field(min_length=1, max_length=1024)
    save_as: str | None = Field(default=None, max_length=255)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


class BrowserDragDropArguments(ToolArguments):
    """Drag one element onto another."""

    source_selector: str = Field(min_length=1, max_length=1024)
    target_selector: str = Field(min_length=1, max_length=1024)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


# ──────────────────────────────────────────────────────────────────────
# Tool Specs (OpenAPI schema properties)
# ──────────────────────────────────────────────────────────────────────

BROWSER_AUTOMATION_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="browser_navigate",
        description=(
            "Navigate to an HTTP(S) web URL in the real browser. Never use file:// for a "
            "local file; use an authorized file or document reader instead."
        ),
        properties={
            "url": string_property(
                "HTTP(S) web URL only. Never use file:// for a local file.",
                min_length=1,
                max_length=2048,
            ),
            "wait_until": string_property(
                "Wait condition", enum=["load", "domcontentloaded", "networkidle", "commit"]
            ),
            "timeout_ms": integer_property(
                "Navigation timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["url"],
        arguments_model=BrowserNavigateArguments,
        mode="read",
        handler=_browser_navigate,
    ),
    define_native_tool(
        name="browser_click",
        description="Click an element in the real browser",
        properties={
            "selector": string_property("CSS or XPath selector", min_length=1, max_length=1024),
            "button": string_property("Mouse button", enum=["left", "right", "middle"]),
            "click_count": integer_property("Number of clicks", minimum=1, maximum=3),
            "delay_ms": integer_property("Delay between clicks in ms", minimum=0, maximum=5_000),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector"],
        arguments_model=BrowserClickArguments,
        mode="write",
        handler=_browser_click,
    ),
    define_native_tool(
        name="browser_type",
        description="Type text into an element",
        properties={
            "selector": string_property("CSS or XPath selector", min_length=1, max_length=1024),
            "text": string_property("Text to type", min_length=1, max_length=10_000),
            "delay_ms": integer_property(
                "Delay between keystrokes in ms", minimum=0, maximum=1_000
            ),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector", "text"],
        arguments_model=BrowserTypeArguments,
        mode="write",
        handler=_browser_type,
    ),
    define_native_tool(
        name="browser_fill",
        description="Fill an input field",
        properties={
            "selector": string_property("CSS or XPath selector", min_length=1, max_length=1024),
            "value": string_property("Value to fill", max_length=10_000),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector", "value"],
        arguments_model=BrowserFillArguments,
        mode="write",
        handler=_browser_fill,
    ),
    define_native_tool(
        name="browser_select",
        description="Select option(s) from a dropdown",
        properties={
            "selector": string_property(
                "CSS or XPath selector for <select>", min_length=1, max_length=1024
            ),
            "values": array_property("Values to select", min_items=1, max_items=50),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector", "values"],
        arguments_model=BrowserSelectArguments,
        mode="write",
        handler=_browser_select,
    ),
    define_native_tool(
        name="browser_hover",
        description="Hover over an element",
        properties={
            "selector": string_property("CSS or XPath selector", min_length=1, max_length=1024),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector"],
        arguments_model=BrowserHoverArguments,
        mode="read",
        handler=_browser_hover,
    ),
    define_native_tool(
        name="browser_wait",
        description="Wait for navigation or element",
        properties={
            "wait_for": string_property(
                "Wait condition", enum=["load", "domcontentloaded", "networkidle"]
            ),
            "selector": string_property("CSS or XPath selector to wait for", max_length=1024),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=[],
        arguments_model=BrowserWaitArguments,
        mode="read",
        handler=_browser_wait,
    ),
    define_native_tool(
        name="browser_screenshot",
        description=(
            "Take a screenshot of the current page, a specific element, a clip region, "
            "or a custom viewport size, and attach it to the chat and model vision input."
        ),
        properties={
            "full_page": boolean_property("Capture full page"),
            "selector": string_property("CSS or XPath selector for element", max_length=1024),
            "type": string_property("Image format", enum=["png", "jpeg"]),
            "quality": integer_property("JPEG quality 0-100", minimum=0, maximum=100),
            "viewport_width": integer_property(
                "Optional temporary viewport width in pixels (16-4096)",
                minimum=16,
                maximum=4096,
            ),
            "viewport_height": integer_property(
                "Optional temporary viewport height in pixels (16-4096)",
                minimum=16,
                maximum=4096,
            ),
            "clip": object_property(
                "Optional rectangular clip region {x, y, width, height} in pixels"
            ),
        },
        required=[],
        arguments_model=BrowserScreenshotArguments,
        mode="read",
        handler=_browser_screenshot,
    ),
    define_native_tool(
        name="browser_evaluate",
        description="Evaluate JavaScript in the page context",
        properties={
            "expression": string_property("JavaScript expression", min_length=1, max_length=50_000),
            "arg": object_property("Argument to pass to expression"),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["expression"],
        arguments_model=BrowserEvaluateArguments,
        mode="write",
        handler=_browser_evaluate,
    ),
    define_native_tool(
        name="browser_get_content",
        description="Get page content (HTML, text, or JSON)",
        properties={
            "format": string_property("Output format", enum=["html", "text", "json"]),
            "selector": string_property("CSS or XPath selector", max_length=1024),
        },
        required=[],
        arguments_model=BrowserGetContentArguments,
        mode="read",
        handler=_browser_get_content,
    ),
    define_native_tool(
        name="browser_pdf",
        description="Generate PDF of the page",
        properties={
            "format": string_property(
                "Paper format", enum=["A4", "A3", "A2", "A1", "A0", "Letter", "Legal", "Tabloid"]
            ),
            "landscape": boolean_property("Landscape orientation"),
            "print_background": boolean_property("Print background"),
            "margin_top": string_property("Top margin"),
            "margin_bottom": string_property("Bottom margin"),
            "margin_left": string_property("Left margin"),
            "margin_right": string_property("Right margin"),
        },
        required=[],
        arguments_model=BrowserPdfArguments,
        mode="read",
        handler=_browser_pdf,
    ),
    define_native_tool(
        name="browser_new_tab",
        description="Open a new tab",
        properties={
            "url": string_property("Initial URL", max_length=2048),
        },
        required=[],
        arguments_model=BrowserNewTabArguments,
        mode="read",
        handler=_browser_new_tab,
    ),
    define_native_tool(
        name="browser_switch_tab",
        description="Switch to a tab by index",
        properties={
            "index": integer_property("Tab index (0-based)", minimum=0, maximum=50),
        },
        required=["index"],
        arguments_model=BrowserSwitchTabArguments,
        mode="read",
        handler=_browser_switch_tab,
    ),
    define_native_tool(
        name="browser_close_tab",
        description="Close current tab",
        properties={},
        required=[],
        arguments_model=BrowserCloseTabArguments,
        mode="read",
        handler=_browser_close_tab,
    ),
    define_native_tool(
        name="browser_download",
        description="Download a file",
        properties={
            "selector": string_property(
                "Download link/button selector", min_length=1, max_length=1024
            ),
            "save_as": string_property("Save as filename", max_length=255),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["selector"],
        arguments_model=BrowserDownloadArguments,
        mode="write",
        handler=_browser_download,
    ),
    define_native_tool(
        name="browser_drag_drop",
        description="Drag and drop an element",
        properties={
            "source_selector": string_property(
                "Source element selector", min_length=1, max_length=1024
            ),
            "target_selector": string_property(
                "Target element selector", min_length=1, max_length=1024
            ),
            "timeout_ms": integer_property(
                "Timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["source_selector", "target_selector"],
        arguments_model=BrowserDragDropArguments,
        mode="write",
        handler=_browser_drag_drop,
    ),
]

__all__ = [
    "BROWSER_AUTOMATION_TOOLS",
    "BrowserAutomationConfig",
    "BrowserAutomationRuntime",
    "browser_automation_unavailable_reason",
]
