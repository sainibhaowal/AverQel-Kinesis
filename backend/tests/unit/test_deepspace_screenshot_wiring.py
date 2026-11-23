"""Tests for end-to-end DeepSpace screenshot capture, sizing, and chat artifact wiring."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.tools import browser_automation

pytestmark = pytest.mark.unit_no_db

_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_browser_screenshot_arguments_validate_sizes_and_combinations() -> None:
    small = browser_automation.BrowserScreenshotArguments(
        viewport_width=16,
        viewport_height=16,
        type="png",
    )
    assert small.viewport_width == 16
    assert small.viewport_height == 16

    large = browser_automation.BrowserScreenshotArguments(
        viewport_width=4096,
        viewport_height=2160,
        type="jpeg",
        quality=90,
    )
    assert large.viewport_width == 4096
    assert large.viewport_height == 2160

    clipped = browser_automation.BrowserScreenshotArguments(
        clip={"x": 10, "y": 20, "width": 320, "height": 180},
    )
    assert clipped.clip is not None
    assert clipped.clip.width == 320

    with pytest.raises(ValueError, match="viewport_width and viewport_height"):
        browser_automation.BrowserScreenshotArguments(viewport_width=640)

    with pytest.raises(ValueError, match="full_page"):
        browser_automation.BrowserScreenshotArguments(
            full_page=True,
            clip={"x": 0, "y": 0, "width": 100, "height": 100},
        )

    with pytest.raises(ValueError, match="selector"):
        browser_automation.BrowserScreenshotArguments(
            selector="#card",
            clip={"x": 0, "y": 0, "width": 100, "height": 100},
        )


@pytest.mark.asyncio
async def test_browser_runtime_screenshot_supports_small_large_clip_and_restores_viewport() -> None:
    calls: list[tuple[str, Any]] = []

    class FakeElement:
        async def screenshot(self, **kwargs: Any) -> bytes:
            calls.append(("element.screenshot", kwargs))
            return b"elem-bytes"

    class FakePage:
        def __init__(self) -> None:
            self.viewport_size = {"width": 1280, "height": 720}

        async def set_viewport_size(self, size: dict[str, int]) -> None:
            calls.append(("set_viewport_size", dict(size)))
            self.viewport_size = dict(size)

        async def screenshot(self, **kwargs: Any) -> bytes:
            calls.append(("page.screenshot", kwargs))
            return base64.b64decode(_PNG_1X1_B64)

        async def wait_for_selector(self, selector: str) -> FakeElement:
            calls.append(("wait_for_selector", selector))
            return FakeElement()

    page = FakePage()
    runtime = browser_automation.BrowserAutomationRuntime(
        browser_automation.BrowserAutomationConfig(),
        SimpleNamespace(),
    )
    runtime._session = SimpleNamespace(page=page)

    small_res = await runtime.screenshot(
        viewport_width=64,
        viewport_height=48,
        image_type="png",
    )
    assert small_res["format"] == "png"
    assert small_res["bytes"] > 0
    assert base64.b64decode(small_res["image_base64"]) == base64.b64decode(_PNG_1X1_B64)
    assert page.viewport_size == {"width": 1280, "height": 720}
    assert ("set_viewport_size", {"width": 64, "height": 48}) in calls
    assert ("set_viewport_size", {"width": 1280, "height": 720}) in calls

    large_res = await runtime.screenshot(
        viewport_width=3840,
        viewport_height=2160,
        clip={"x": 12, "y": 24, "width": 800, "height": 600},
        image_type="jpeg",
        quality=85,
    )
    assert large_res["format"] == "jpeg"
    assert (
        "page.screenshot",
        {
            "type": "jpeg",
            "full_page": False,
            "quality": 85,
            "clip": {"x": 12.0, "y": 24.0, "width": 800.0, "height": 600.0},
        },
    ) in calls
    assert page.viewport_size == {"width": 1280, "height": 720}

    elem_res = await runtime.screenshot(selector="#hero", image_type="png")
    assert base64.b64decode(elem_res["image_base64"]) == b"elem-bytes"


def test_chat_service_wires_browser_and_computer_screenshots_to_vision_and_artifacts() -> None:
    persisted_calls: list[dict[str, Any]] = []
    artifact_id = str(uuid.uuid4())

    class FakeMediaArtifacts:
        def persist_content_base64(self, **kwargs: Any) -> dict[str, Any]:
            persisted_calls.append(kwargs)
            return {
                "id": artifact_id,
                "kind": "image",
                "status": "ready",
                "title": kwargs["title"],
                "content_type": kwargs["content_type"],
                "size_bytes": 68,
                "url": f"/api/v1/deepspace/artifacts/{artifact_id}/content",
            }

    service = object.__new__(DeepSpaceChatService)
    service.media_artifacts = FakeMediaArtifacts()

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()

    state = SimpleNamespace(
        tool_name="browser_screenshot",
        tool_payload={"image_base64": _PNG_1X1_B64, "format": "png", "bytes": 68},
        pending_images=[],
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        assistant_message=SimpleNamespace(id=message_id),
        round_index=2,
        generated_artifacts=[],
    )

    artifact = service._consume_screenshot_tool_result(state)
    assert artifact is not None
    assert artifact["id"] == artifact_id
    assert state.pending_images == [f"data:image/png;base64,{_PNG_1X1_B64}"]
    assert state.generated_artifacts == [artifact]
    assert "image_base64" not in state.tool_payload
    assert state.tool_payload["artifact_id"] == artifact_id
    assert state.tool_payload["content_type"] == "image/png"
    assert persisted_calls[0]["tenant_id"] == tenant_id
    assert persisted_calls[0]["user_id"] == user_id
    assert persisted_calls[0]["conversation_id"] == conversation_id
    assert persisted_calls[0]["message_id"] == message_id
    assert persisted_calls[0]["requested_kind"] == "image"

    # Verify computer_screenshot with jpg format normalizes to image/jpeg
    computer_state = SimpleNamespace(
        tool_name="computer_screenshot",
        tool_payload={"image_base64": _PNG_1X1_B64, "format": "jpg"},
        pending_images=[],
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        assistant_message=SimpleNamespace(id=message_id),
        round_index=3,
        generated_artifacts=[],
    )
    computer_artifact = service._consume_screenshot_tool_result(computer_state)
    assert computer_artifact is not None
    assert computer_state.pending_images == [f"data:image/jpeg;base64,{_PNG_1X1_B64}"]
    assert "image_base64" not in computer_state.tool_payload


def test_chat_service_screenshot_strips_base64_even_if_artifact_persistence_fails() -> None:
    class FailingMediaArtifacts:
        def persist_content_base64(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("quota exceeded")

    service = object.__new__(DeepSpaceChatService)
    service.media_artifacts = FailingMediaArtifacts()

    state = SimpleNamespace(
        tool_name="browser_screenshot",
        tool_payload={"image_base64": _PNG_1X1_B64, "format": "png"},
        pending_images=[],
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        assistant_message=SimpleNamespace(id=uuid.uuid4()),
        round_index=1,
        generated_artifacts=[],
    )

    artifact = service._consume_screenshot_tool_result(state)
    assert artifact is None
    assert state.pending_images == [f"data:image/png;base64,{_PNG_1X1_B64}"]
    assert state.generated_artifacts == []
    assert "image_base64" not in state.tool_payload
