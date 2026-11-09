"""Exercise the Playwright browser runtime against a local page.

These tests drive the real browser process: navigation, form entry, clicks,
JavaScript evaluation, tab management, screenshots, and PDF generation. They
use ``data:`` URLs so no network service or credential is involved, and skip
when the Playwright package or its Chromium build is unavailable.

Ownership: ``app/deepspace/services/tools/browser_automation.py``.
"""

from __future__ import annotations

import base64
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.core.config import get_settings
from app.deepspace.services.tools import browser_automation

pytestmark = [pytest.mark.external, pytest.mark.unit_no_db]

pytest.importorskip("playwright", reason="Playwright is not installed")

PAGE = """
<!doctype html>
<html>
  <body>
    <input id="name" />
    <button id="greet" onclick="document.querySelector('#out').textContent =
      document.querySelector('#name').value">Greet</button>
    <p id="out"></p>
  </body>
</html>
"""


@pytest.fixture
def local_page_url():
    """Serve the fixture page on loopback, the only target used by this test."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler.
            markup = "<html><body><h1>second</h1></body></html>" if self.path == "/second" else PAGE
            body = markup.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def runtime():
    """Start one isolated browser runtime and stop it after the test."""
    settings = get_settings()
    config = browser_automation.BrowserAutomationConfig(
        enabled=True,
        browser_type="chromium",
        headless=True,
        default_timeout_ms=15_000,
        max_timeout_ms=30_000,
    )
    instance = browser_automation.BrowserAutomationRuntime(config, settings)
    await instance.start(uuid.uuid4())
    try:
        yield instance
    finally:
        await instance.stop()


async def test_navigation_fill_click_and_evaluate(runtime, local_page_url: str) -> None:
    """A typed value reaches the page only after the real click runs its script."""
    result = await runtime.navigate(f"{local_page_url}/", wait_until="load")

    assert result["success"] is True
    assert result["url"].startswith("http://127.0.0.1:")

    await runtime.fill("#name", "Ada")
    await runtime.click("#greet")

    assert await runtime.evaluate("document.querySelector('#out').textContent") == {"result": "Ada"}


async def test_screenshot_returns_a_real_png(runtime, local_page_url: str) -> None:
    """The screenshot tool returns decoded PNG bytes, not a placeholder string."""
    await runtime.navigate(f"{local_page_url}/", wait_until="load")

    result = await runtime.screenshot(full_page=True)

    raw = base64.b64decode(result["image_base64"])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(raw) > 1_000


async def test_tabs_open_switch_and_close(runtime, local_page_url: str) -> None:
    """Tab tools mutate the live session without raising on a frozen record."""
    await runtime.navigate(f"{local_page_url}/", wait_until="load")

    await runtime.new_tab(f"{local_page_url}/second")
    assert await runtime.evaluate("document.querySelector('h1').textContent") == {
        "result": "second"
    }

    await runtime.switch_tab(0)
    assert await runtime.evaluate("document.querySelector('#name').tagName") == {"result": "INPUT"}

    await runtime.close_tab()
    assert runtime.is_running


async def test_pdf_returns_pdf_bytes(runtime, local_page_url: str) -> None:
    """PDF generation produces a document with the PDF file signature."""
    await runtime.navigate(f"{local_page_url}/", wait_until="load")

    result = await runtime.pdf(format="A4")

    assert base64.b64decode(result["pdf_base64"]).startswith(b"%PDF")


async def test_page_content_reports_real_markup(runtime, local_page_url: str) -> None:
    """Page content is read from the live DOM."""
    await runtime.navigate(f"{local_page_url}/", wait_until="load")

    result = await runtime.get_content("html")

    assert 'id="greet"' in result["content"]
