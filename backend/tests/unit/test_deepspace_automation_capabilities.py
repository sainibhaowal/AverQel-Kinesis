from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import config as config_module
from app.deepspace.services import tools as tool_package
from app.deepspace.services.tools import browser_automation, computer_use

pytestmark = pytest.mark.unit_no_db


def test_linux_desktop_defaults_to_full_mode_and_local_computer_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVERQEL_DESKTOP_LOCAL_RUNTIME", "1")
    monkeypatch.delenv("AVERQEL_DESKTOP_PACKAGED", raising=False)
    monkeypatch.setattr(config_module.sys, "platform", "linux")

    assert config_module._default_sandbox_policy() == "danger-full-access"
    assert config_module._default_computer_use_enabled() is True
    assert config_module._default_computer_use_provider() == "local"


def test_runtime_preflight_reports_missing_browser_and_desktop_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        deepspace_browser_automation_enabled=True,
        deepspace_browser_automation_browser_type="chromium",
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="local",
    )
    monkeypatch.setattr(browser_automation, "PLAYWRIGHT_AVAILABLE", False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(computer_use, "_tool_path", lambda _name: None)

    assert browser_automation.browser_automation_unavailable_reason(settings) == (
        "The Playwright Python runtime is not installed."
    )
    reason = computer_use.computer_use_unavailable_reason(settings)
    assert reason is not None
    assert "xdotool" in reason
    assert "scrot or ImageMagick import" in reason


def test_unavailable_automation_is_not_offered_or_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        deepspace_browser_automation_enabled=True,
        deepspace_browser_automation_browser_type="chromium",
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="local",
    )
    monkeypatch.setattr(
        browser_automation,
        "browser_automation_unavailable_reason",
        lambda _settings: "The Playwright Python runtime is not installed.",
    )
    monkeypatch.setattr(
        computer_use,
        "computer_use_unavailable_reason",
        lambda _settings, _tool_name=None: "The local desktop driver needs xdotool.",
    )

    unavailable = tool_package.unavailable_tool_names(settings)
    browser_names = {spec.name for spec in browser_automation.BROWSER_AUTOMATION_TOOLS}
    computer_names = {spec.name for spec in computer_use.COMPUTER_USE_TOOLS}

    assert browser_names <= unavailable
    assert computer_names <= unavailable

    async def dispatch() -> tuple[dict[str, object], dict[str, object]]:
        context = SimpleNamespace(settings=settings)
        browser = await tool_package.dispatch_native_tool("browser_navigate", {}, context)
        computer = await tool_package.dispatch_native_tool("computer_screenshot", {}, context)
        assert browser is not None
        assert computer is not None
        return browser, computer

    browser_result, computer_result = asyncio.run(dispatch())
    assert browser_result["error_code"] == "FEATURE_UNAVAILABLE"
    assert browser_result["message"] == "The Playwright Python runtime is not installed."
    assert computer_result["error_code"] == "FEATURE_UNAVAILABLE"
    assert computer_result["message"] == "The local desktop driver needs xdotool."


def test_available_automation_stays_in_the_tool_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        deepspace_browser_automation_enabled=True,
        deepspace_browser_automation_browser_type="chromium",
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="local",
    )
    monkeypatch.setattr(
        browser_automation, "browser_automation_unavailable_reason", lambda _s: None
    )
    monkeypatch.setattr(
        computer_use,
        "computer_use_unavailable_reason",
        lambda _s, _tool_name=None: None,
    )

    unavailable = tool_package.unavailable_tool_names(settings)

    assert not any(name.startswith("browser_") for name in unavailable)
    assert not any(name.startswith("computer_") for name in unavailable)


@pytest.mark.parametrize(
    ("tool_name", "attribute", "message"),
    [
        (
            "computer_find_image",
            "OPENCV_AVAILABLE",
            "Image search needs OpenCV, which is not installed in this runtime.",
        ),
        (
            "computer_ocr",
            "TESSERACT_AVAILABLE",
            "OCR needs both the pytesseract package and the Tesseract executable.",
        ),
    ],
)
def test_computer_use_preflight_reports_tool_specific_native_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    attribute: str,
    message: str,
) -> None:
    settings = SimpleNamespace(
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="local",
    )
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        computer_use,
        "_tool_path",
        lambda name: f"/usr/bin/{name}" if name in {"xdotool", "scrot"} else None,
    )
    monkeypatch.setattr(computer_use, attribute, False)

    assert computer_use.computer_use_unavailable_reason(settings, tool_name) == message


def test_vnc_screenshot_preflight_requires_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="vnc",
    )
    monkeypatch.setattr(computer_use, "_tool_path", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(computer_use, "PIL_AVAILABLE", False)

    assert computer_use.computer_use_unavailable_reason(settings, "computer_screenshot") == (
        "VNC screenshots need Pillow, which is not installed in this runtime."
    )


def test_tool_specific_computer_prerequisites_do_not_hide_other_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        deepspace_computer_use_enabled=True,
        deepspace_computer_use_provider="local",
    )
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        computer_use,
        "_tool_path",
        lambda name: "/usr/bin/tool" if name in {"xdotool", "scrot"} else None,
    )
    monkeypatch.setattr(computer_use, "OPENCV_AVAILABLE", False)

    assert computer_use.computer_use_unavailable_reason(settings, "computer_click") is None
    assert (
        computer_use.computer_use_unavailable_reason(settings, "computer_find_image")
        == "Image search needs OpenCV, which is not installed in this runtime."
    )


def test_browser_navigate_rejects_local_file_before_starting_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def browser_must_not_start(_context: object) -> None:
        pytest.fail("The browser must not start for a file:// URL.")

    monkeypatch.setattr(browser_automation, "_get_runtime", browser_must_not_start)

    result = asyncio.run(
        browser_automation._browser_navigate(
            {"url": "file:///home/AverQel/Downloads/Q4-FY23-Revenue-and-Earnings.pdf"},
            SimpleNamespace(),
        )
    )

    assert result["status"] == "invalid_input"
    assert result["error_code"] == "BROWSER_URL_UNSUPPORTED"
    assert "HTTP(S) web pages only" in result["recovery"]
