"""Real Browser Automation for DeepSpace using Playwright.

Provides a real browser (Playwright) driven as an external process with full tool bindings.
This is the "real browser" alternative to the embedded browser panel.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

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

try:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None
    Browser = None
    BrowserContext = None
    Page = None
    Playwright = None

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
        self._start_lock = asyncio.Lock()

    async def start(self, conversation_id: UUID) -> BrowserSession:
        """Start a new browser automation session."""
        if self._session is not None:
            await self.stop()

        self._playwright = await async_playwright().start()

        # Launch browser
        browser_type = getattr(self._playwright, self.config.browser_type)
        from app.deepspace.services.browser_runtime import (
            find_browser_executable,
        )

        executable_path = find_browser_executable()
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "args": list(self.config.extra_args) if self.config.extra_args else None,
            "proxy": self.config.proxy,
        }
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = await browser_type.launch(**launch_kwargs)

        # Create context with viewport and settings
        context = await browser.new_context(
            viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
            user_agent=self.config.user_agent,
            locale=self.config.locale,
            timezone_id=self.config.timezone_id,
            permissions=list(self.config.permissions),
            record_video_dir=self.config.video_dir,
            accept_downloads=True,
        )

        # Set default timeout
        context.set_default_timeout(self.config.default_timeout_ms)
        context.set_default_navigation_timeout(self.config.default_timeout_ms)

        # Create initial page
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
            f"Started browser automation session {self._session.session_id} for conversation {conversation_id}"
        )
        return self._session

    async def stop(self) -> None:
        """Stop the browser automation session."""
        if self._session:
            try:
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
                pass
            self._playwright = None

        self._session = None
        logger.info("Browser automation session stopped")

    async def ensure_started(self, conversation_id: UUID) -> BrowserSession:
        """Start the session on first use, serializing concurrent tool calls."""
        async with self._start_lock:
            if not self.is_running:
                return await self.start(conversation_id)
            assert self._session is not None
            self._session.last_activity = datetime.now(UTC)
            return self._session

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
        if not self.is_running:
            raise RuntimeError("Browser automation not running. Start a session first.")
        return self._session

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
    ) -> dict[str, Any]:
        """Take a screenshot."""
        session = await self._ensure_session()
        options: dict[str, Any] = {"type": image_type, "full_page": full_page}
        if quality is not None:
            options["quality"] = quality
        if selector:
            element = await session.page.wait_for_selector(selector)
            image_bytes = await element.screenshot(**options)
        else:
            image_bytes = await session.page.screenshot(**options)
        b64 = base64.b64encode(image_bytes).decode()
        return {"image_base64": b64, "format": image_type}

    async def evaluate(
        self, expression: str, arg: dict | None = None, timeout_ms: int | None = None
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

    async def pdf(self, **options) -> dict[str, Any]:
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

_BROWSER_RUNTIMES: dict[UUID, BrowserAutomationRuntime] = {}
_BROWSER_RUNTIMES_LOCK = asyncio.Lock()


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
    )


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
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. Run: "
            "pip install playwright && playwright install chromium"
        )
    conversation_id = context.conversation_id
    async with _BROWSER_RUNTIMES_LOCK:
        runtime = _BROWSER_RUNTIMES.get(conversation_id)
        if runtime is None:
            config = _config_for(context.settings)
            if not config.enabled:
                raise RuntimeError(
                    "Browser automation is disabled. Set "
                    "DEEPSPACE_BROWSER_AUTOMATION_ENABLED=true."
                )
            runtime = BrowserAutomationRuntime(config, context.settings)
            _BROWSER_RUNTIMES[conversation_id] = runtime
    await runtime.ensure_started(conversation_id)
    return runtime


async def shutdown_browser_runtimes() -> None:
    """Close every conversation's browser; used at application shutdown."""
    async with _BROWSER_RUNTIMES_LOCK:
        runtimes = list(_BROWSER_RUNTIMES.values())
        _BROWSER_RUNTIMES.clear()
    for runtime in runtimes:
        await runtime.stop()


# ──────────────────────────────────────────────────────────────────────
# Tool Handlers
# ──────────────────────────────────────────────────────────────────────


async def _browser_navigate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.navigate(
        arguments["url"],
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


async def _browser_screenshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    result = await runtime.screenshot(
        arguments.get("full_page", False),
        arguments.get("selector"),
        arguments.get("type", "png"),
        arguments.get("quality"),
    )
    result["format"] = arguments.get("type", "png")
    return result


async def _browser_evaluate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.evaluate(
        arguments["expression"], arguments.get("arg"), arguments.get("timeout_ms")
    )


async def _browser_get_content(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _get_runtime(context)
    return await runtime.get_content(arguments.get("format", "html"), arguments.get("selector"))


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

    url: str = Field(min_length=1, max_length=2048)
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] | None = None
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)


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


class BrowserScreenshotArguments(ToolArguments):
    """Capture the page or one element."""

    full_page: bool | None = None
    selector: str | None = Field(default=None, max_length=1024)
    type: Literal["png", "jpeg"] | None = None
    quality: int | None = Field(default=None, ge=0, le=100)


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
        description="Navigate to a URL in the real browser",
        properties={
            "url": string_property("URL to navigate to", min_length=1, max_length=2048),
            "wait_until": string_property(
                "Wait condition", enum=["load", "domcontentloaded", "networkidle", "commit"]
            ),
            "timeout_ms": integer_property(
                "Navigation timeout in milliseconds", minimum=1_000, maximum=300_000
            ),
        },
        required=["url"],
        arguments_model=BrowserNavigateArguments,
        mode="write",
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
        description="Take a screenshot",
        properties={
            "full_page": boolean_property("Capture full page"),
            "selector": string_property("CSS or XPath selector for element", max_length=1024),
            "type": string_property("Image format", enum=["png", "jpeg"]),
            "quality": integer_property("JPEG quality 0-100", minimum=0, maximum=100),
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
        mode="read",
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
        mode="write",
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
        mode="write",
        handler=_browser_switch_tab,
    ),
    define_native_tool(
        name="browser_close_tab",
        description="Close current tab",
        properties={},
        required=[],
        arguments_model=BrowserCloseTabArguments,
        mode="write",
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
]
