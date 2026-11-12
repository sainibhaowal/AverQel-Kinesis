"""Exercise the Playwright browser runtime against a local page.

These tests drive the real browser process: navigation, form entry, clicks,
JavaScript evaluation, tab management, screenshots, and PDF generation. They
use a disposable loopback HTTP server so they exercise the same URL policy as
production without contacting an external service, and skip when the
Playwright package or its Chromium build is unavailable.

Ownership: ``app/deepspace/services/tools/browser_automation.py``.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deepspace.services.browser_runtime import find_browser_executable
from app.deepspace.services.tools import browser_automation

pytestmark = [
    pytest.mark.external,
    pytest.mark.unit_no_db,
    pytest.mark.skipif(
        find_browser_executable() is None,
        reason="No Chromium executable is installed in this isolated test image.",
    ),
]

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


class _QuietPageHandler(SimpleHTTPRequestHandler):
    """Serve test pages without writing one access log line per browser request."""

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture(scope="module")
def local_page_urls(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Serve the test pages from a disposable loopback-only HTTP server."""
    root = Path(tmp_path_factory.mktemp("browser-pages"))
    (root / "page.html").write_text(PAGE, encoding="utf-8")
    (root / "second.html").write_text("<html><body><h1>second</h1></body></html>", encoding="utf-8")
    handler = partial(_QuietPageHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield f"{base_url}/page.html", f"{base_url}/second.html"
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


async def test_navigation_fill_click_and_evaluate(runtime, local_page_urls) -> None:
    """A typed value reaches the page only after the real click runs its script."""
    page_url, _ = local_page_urls
    result = await runtime.navigate(page_url, wait_until="load")

    assert result["success"] is True
    assert result["url"] == page_url

    await runtime.fill("#name", "Ada")
    await runtime.click("#greet")

    assert await runtime.evaluate("document.querySelector('#out').textContent") == {"result": "Ada"}


async def test_screenshot_returns_a_real_png(runtime, local_page_urls) -> None:
    """The screenshot tool returns decoded PNG bytes, not a placeholder string."""
    page_url, _ = local_page_urls
    await runtime.navigate(page_url, wait_until="load")

    result = await runtime.screenshot(full_page=True)

    raw = base64.b64decode(result["image_base64"])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(raw) > 1_000


async def test_tabs_open_switch_and_close(runtime, local_page_urls) -> None:
    """Tab tools mutate the live session without raising on a frozen record."""
    page_url, second_page_url = local_page_urls
    await runtime.navigate(page_url, wait_until="load")

    await runtime.new_tab(second_page_url)
    assert await runtime.evaluate("document.querySelector('h1').textContent") == {
        "result": "second"
    }

    await runtime.switch_tab(0)
    assert await runtime.evaluate("document.querySelector('#name').tagName") == {"result": "INPUT"}

    await runtime.close_tab()
    assert runtime.is_running


async def test_pdf_returns_pdf_bytes(runtime, local_page_urls) -> None:
    """PDF generation produces a document with the PDF file signature."""
    page_url, _ = local_page_urls
    await runtime.navigate(page_url, wait_until="load")

    result = await runtime.pdf(format="A4")

    assert base64.b64decode(result["pdf_base64"]).startswith(b"%PDF")


async def test_page_content_reports_real_markup(runtime, local_page_urls) -> None:
    """Page content is read from the live DOM."""
    page_url, _ = local_page_urls
    await runtime.navigate(page_url, wait_until="load")

    result = await runtime.get_content("html")

    assert 'id="greet"' in result["content"]
