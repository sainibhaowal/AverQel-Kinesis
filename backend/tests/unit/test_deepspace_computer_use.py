"""Behavior tests for the desktop computer-use runtimes.

The RFB test drives the real client code against an in-process server that
speaks the same wire protocol as x11vnc, so pixel decoding, pointer events,
and key events are verified without a display or a network service.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from app.core.config import get_settings
from app.deepspace.services.tools import computer_use

pytestmark = pytest.mark.unit_no_db


def _settings_with(**overrides):
    """Return a copy of the deployment settings with values replaced."""
    return get_settings().model_copy(update=overrides)


async def test_disabled_computer_use_fails_before_starting(monkeypatch) -> None:
    """A disabled deployment reports the setting instead of a later tool error."""
    monkeypatch.setattr(computer_use, "_RUNTIME", None)
    settings = _settings_with(deepspace_computer_use_enabled=False)

    with pytest.raises(computer_use.ComputerUseUnavailableError, match="disabled"):
        await computer_use.get_computer_runtime(settings)


async def test_local_preflight_names_every_missing_package(monkeypatch) -> None:
    """Startup lists the packages to install, not just the first failure."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(computer_use, "_tool_path", lambda name: None)
    runtime = computer_use.LocalComputerRuntime(
        computer_use.ComputerUseConfig(enabled=True), get_settings()
    )

    with pytest.raises(computer_use.ComputerUseUnavailableError) as error:
        await runtime.start()

    message = str(error.value)
    assert "DISPLAY" in message
    assert "xdotool" in message
    assert "scrot" in message


async def test_scroll_sends_wheel_buttons(monkeypatch) -> None:
    """Scrolling uses X11 wheel buttons 4/5 rather than arrow keys."""
    calls: list[list[str]] = []

    async def record(argv: list[str], timeout: float = 30.0) -> None:
        calls.append(argv)

    monkeypatch.setattr(computer_use, "_run", record)
    monkeypatch.setattr(computer_use, "_tool_path", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("DISPLAY", ":99")
    runtime = computer_use.LocalComputerRuntime(
        computer_use.ComputerUseConfig(enabled=True), get_settings()
    )

    await runtime.scroll(10, 20, direction="up", steps=2, step_delay_ms=1)

    assert calls[0] == ["xdotool", "mousemove", "--sync", "10", "20"]
    assert [call[2] for call in calls[1:]] == ["4", "4"]


async def test_type_text_keeps_leading_dash_literal(monkeypatch) -> None:
    """Text that starts with '-' is still typed as text."""
    calls: list[list[str]] = []

    async def record(argv: list[str], timeout: float = 30.0) -> None:
        calls.append(argv)

    monkeypatch.setattr(computer_use, "_run", record)
    monkeypatch.setattr(computer_use, "_tool_path", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("DISPLAY", ":99")
    runtime = computer_use.LocalComputerRuntime(
        computer_use.ComputerUseConfig(enabled=True), get_settings()
    )

    await runtime.type_text("-rf /", delay_ms=5)

    assert calls == [["xdotool", "type", "--delay", "5", "--", "-rf /"]]


def test_keysym_mapping_covers_control_and_unicode() -> None:
    """Public key names and Unicode characters map to X11 keysyms."""
    assert computer_use._keysym("Return") == 0xFF0D
    assert computer_use._keysym("ctrl") == 0xFFE3
    assert computer_use._keysym("f5") == 0xFFC2
    assert computer_use._keysym("a") == ord("a")
    assert computer_use._keysym("é") == ord("é")
    assert computer_use._keysym("€") == 0x01000000 + ord("€")

    with pytest.raises(ValueError, match="Unknown key name"):
        computer_use._keysym("not-a-key")


async def test_rfb_handshake_decodes_pixels_and_sends_input() -> None:
    """The RFB client negotiates 3.8, decodes BGRX, and frames input events."""
    captured: dict[str, bytes] = {}
    captured_event = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            assert await reader.readexactly(12) == b"RFB 003.008\n"

            writer.write(bytes([1, 1]))  # one security type: None
            await writer.drain()
            assert await reader.readexactly(1) == b"\x01"

            writer.write(struct.pack(">I", 0))  # SecurityResult: OK
            await writer.drain()
            assert await reader.readexactly(1) == b"\x01"  # ClientInit: shared

            name = b"fake"
            pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 1, 1, 255, 255, 255, 16, 8, 0)
            writer.write(
                struct.pack(">HH", 2, 1) + pixel_format + struct.pack(">I", len(name)) + name
            )
            await writer.drain()

            assert len(await reader.readexactly(20)) == 20  # SetPixelFormat
            assert len(await reader.readexactly(12)) == 12  # SetEncodings

            request = await reader.readexactly(10)
            assert request[0] == 3

            pixels = bytes([0x10, 0x20, 0x30, 0x00, 0x40, 0x50, 0x60, 0x00])
            writer.write(
                struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", 0, 0, 2, 1, 0) + pixels
            )
            await writer.drain()

            captured["pointer"] = await reader.readexactly(6)
            captured["key"] = await reader.readexactly(8)
            captured_event.set()
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = computer_use.RFBClient("127.0.0.1", port)
    try:
        await client.connect(timeout=5)

        assert (client.width, client.height) == (2, 1)

        await client.request_update()
        pixels, width, height = await client.read_update()

        assert (width, height) == (2, 1)
        assert pixels[0:4] == bytes([0x30, 0x20, 0x10, 0xFF])
        assert pixels[4:8] == bytes([0x60, 0x50, 0x40, 0xFF])

        await client.pointer(3, 4, 1)
        await client.key(0xFF0D, True)
        await asyncio.wait_for(captured_event.wait(), timeout=5)

        assert captured["pointer"] == struct.pack(">BBHH", 5, 1, 3, 4)
        assert captured["key"] == struct.pack(">BBxxI", 4, 1, 0xFF0D)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
