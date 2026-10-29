"""Desktop computer-use tools for DeepSpace.

Two runtimes implement the same desktop operations:

* :class:`LocalComputerRuntime` drives the X display of the backend host with
  ``xdotool`` and a screenshot utility (``scrot``, ImageMagick ``import``,
  ``gnome-screenshot``, or ``xwd``).
* :class:`VNCComputerRuntime` starts its own ``Xvfb`` display plus ``x11vnc``
  and speaks RFB 3.8 directly, so the desktop does not appear on the host.

Both are opt-in: ``DEEPSPACE_COMPUTER_USE_ENABLED`` defaults to false, and a
runtime that cannot find its system packages fails at start with the package
names to install instead of failing later inside a model tool call.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from app.core.config import Settings
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    integer_property,
    number_property,
    object_property,
    string_property,
)

logger = logging.getLogger(__name__)


class ComputerUseUnavailableError(RuntimeError):
    """The configured desktop runtime cannot start on this host."""


# ──────────────────────────────────────────────────────────────────────
# Optional Python packages
# ──────────────────────────────────────────────────────────────────────

try:
    import cv2
    import numpy as np

    OPENCV_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the dependency probe
    cv2 = None
    np = None
    OPENCV_AVAILABLE = False

try:
    import pytesseract

    TESSERACT_AVAILABLE = bool(shutil.which("tesseract"))
except ImportError:  # pragma: no cover - exercised by the dependency probe
    pytesseract = None
    TESSERACT_AVAILABLE = False

try:
    from Crypto.Cipher import DES

    PYCRYPTODOME_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the dependency probe
    DES = None
    PYCRYPTODOME_AVAILABLE = False

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the dependency probe
    Image = None
    PIL_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ComputerUseConfig:
    """Computer-use runtime configuration."""

    enabled: bool = False
    provider: Literal["local", "vnc"] = "local"
    vnc_host: str = "127.0.0.1"
    vnc_port: int = 5900
    vnc_password: str | None = None
    display_width: int = 1920
    display_height: int = 1080
    max_screenshot_size_mb: int = 10
    action_timeout_ms: int = 30_000
    click_delay_ms: int = 100
    type_delay_ms: int = 50
    temp_dir: str | None = None

    def with_provider(self, provider: Literal["local", "vnc"]) -> ComputerUseConfig:
        """Return a copy configured for another provider.

        Args:
            provider: The provider to use in the copy.

        Returns:
            A new configuration with ``provider`` replaced.
        """
        return replace(self, provider=provider)


# ──────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────


def _tool_path(name: str) -> str | None:
    """Return the absolute path of an external command, if installed."""
    return shutil.which(name)


async def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run one external command without a shell.

    Args:
        argv: Program and arguments.
        timeout: Seconds before the call is abandoned.

    Returns:
        The completed process with captured text output.
    """
    return await asyncio.to_thread(
        subprocess.run,
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read the pixel dimensions of a rendered screenshot."""
    if PIL_AVAILABLE:
        with Image.open(path) as image:
            return image.size
    identify = _tool_path("identify")
    if identify:
        result = subprocess.run(
            [identify, "-format", "%w %h", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            width, height = result.stdout.split()
            return int(width), int(height)
    return 0, 0


def _render_screenshot(source: Path, target: Path, quality: int) -> Path:
    """Convert a screenshot to the requested format, applying JPEG quality."""
    if target.suffix == ".png" and source.suffix == ".png":
        return source
    convert = _tool_path("convert") or _tool_path("magick")
    if not convert:
        return source
    argv = [convert, str(source)]
    if target.suffix in {".jpg", ".jpeg"}:
        argv.extend(["-quality", str(quality)])
    argv.append(str(target))
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise ComputerUseUnavailableError(
            f"Could not convert the screenshot: {result.stderr.strip()}"
        )
    return target


# ──────────────────────────────────────────────────────────────────────
# Local X11 runtime
# ──────────────────────────────────────────────────────────────────────


class LocalComputerRuntime:
    """Drive the host's X display through ``xdotool`` and a screenshot tool."""

    def __init__(self, config: ComputerUseConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.session_id = str(uuid.uuid4())
        self._temp_dir = (
            Path(config.temp_dir)
            if config.temp_dir
            else Path(tempfile.mkdtemp(prefix=f"deepspace_computer_{self.session_id}_"))
        )
        self._screenshot_tool = self._select_screenshot_tool()

    @staticmethod
    def _select_screenshot_tool() -> str | None:
        """Return the first available screenshot program."""
        for tool in ("scrot", "import", "gnome-screenshot", "xfce4-screenshooter", "xwd"):
            if _tool_path(tool):
                return tool
        return None

    def preflight(self) -> None:
        """Fail now, with install instructions, when the host cannot be driven.

        Raises:
            ComputerUseUnavailableError: A required program or display is missing.
        """
        missing: list[str] = []
        if not os.environ.get("DISPLAY"):
            missing.append("an X display (DISPLAY is unset)")
        if not _tool_path("xdotool"):
            missing.append("xdotool")
        if self._screenshot_tool is None:
            missing.append("scrot or ImageMagick import")
        if missing:
            raise ComputerUseUnavailableError(
                "Computer use needs "
                + ", ".join(missing)
                + ". Install with: sudo apt-get install xdotool scrot imagemagick"
            )

    async def start(self) -> None:
        """Validate the host before the first tool call."""
        self.preflight()

    async def stop(self) -> None:
        """Remove this session's temporary screenshots."""
        if self.config.temp_dir is None:
            await asyncio.to_thread(shutil.rmtree, self._temp_dir, True)

    async def screenshot(
        self,
        format: str = "png",
        quality: int = 85,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Capture the display or one region.

        Args:
            format: ``png`` or ``jpeg``.
            quality: JPEG quality, 1-100.
            region: Optional ``{x, y, width, height}`` crop.

        Returns:
            The base64 image, its format, and its pixel dimensions.
        """
        self.preflight()
        extension = "jpeg" if format in {"jpeg", "jpg"} else "png"
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        raw_path = (
            self._temp_dir
            / f"screenshot_{stamp}.{extension if self._screenshot_tool != 'xwd' else 'xwd'}"
        )
        tool = self._screenshot_tool
        crop = ""
        if region:
            crop = (
                f"{int(region['width'])}x{int(region['height'])}"
                f"+{int(region['x'])}+{int(region['y'])}"
            )
        if tool == "scrot":
            argv = ["scrot", "-o", str(raw_path)]
            if crop:
                argv = [
                    "scrot",
                    "-a",
                    crop.replace("x", ",").replace("+", ","),
                    "-o",
                    str(raw_path),
                ]
        elif tool == "import":
            argv = ["import", "-window", "root"]
            if crop:
                argv.extend(["-crop", crop])
            argv.append(str(raw_path))
        elif tool == "gnome-screenshot":
            argv = ["gnome-screenshot", "-f", str(raw_path)]
        elif tool == "xfce4-screenshooter":
            argv = ["xfce4-screenshooter", "-f", "-s", str(raw_path)]
        elif tool == "xwd":
            argv = ["xwd", "-root", "-silent", "-out", str(raw_path)]
        else:  # pragma: no cover - preflight rejects this state
            raise ComputerUseUnavailableError("No screenshot program is installed")

        result = await _run(argv, timeout=15.0)
        if result.returncode != 0:
            raise ComputerUseUnavailableError(
                f"{tool} could not capture the display: {result.stderr.strip()}"
            )

        final_path = _render_screenshot(
            raw_path, self._temp_dir / f"screenshot_{stamp}.{extension}", quality
        )
        width, height = _image_dimensions(final_path)
        data = await asyncio.to_thread(final_path.read_bytes)
        if len(data) > self.config.max_screenshot_size_mb * 1024 * 1024:
            final_path.unlink(missing_ok=True)
            raise ComputerUseUnavailableError(
                f"Screenshot exceeds {self.config.max_screenshot_size_mb} MB"
            )
        encoded = base64.b64encode(data).decode()
        for path in {raw_path, final_path}:
            path.unlink(missing_ok=True)
        return {
            "image_base64": encoded,
            "width": width,
            "height": height,
            "format": extension,
        }

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        delay_ms: int = 100,
    ) -> dict[str, Any]:
        """Move the pointer and click at one screen position."""
        self.preflight()
        button_number = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        await _run(["xdotool", "mousemove", "--sync", str(x), str(y)])
        for index in range(clicks):
            await _run(["xdotool", "click", button_number])
            if index + 1 < clicks:
                await asyncio.sleep(delay_ms / 1000)
        return {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}

    async def double_click(self, x: int, y: int) -> dict[str, Any]:
        """Double-click at one screen position."""
        return await self.click(x, y, clicks=2, delay_ms=self.config.click_delay_ms)

    async def right_click(self, x: int, y: int) -> dict[str, Any]:
        """Right-click at one screen position."""
        return await self.click(x, y, button="right")

    async def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 500,
    ) -> dict[str, Any]:
        """Press at the start, move with intermediate steps, and release."""
        self.preflight()
        steps = max(1, duration_ms // 50)
        await _run(["xdotool", "mousemove", "--sync", str(x1), str(y1)])
        await _run(["xdotool", "mousedown", "1"])
        try:
            for step in range(1, steps + 1):
                x = int(x1 + (x2 - x1) * step / steps)
                y = int(y1 + (y2 - y1) * step / steps)
                await _run(["xdotool", "mousemove", "--sync", str(x), str(y)])
                await asyncio.sleep(duration_ms / 1000 / steps)
        finally:
            await _run(["xdotool", "mouseup", "1"])
        return {"success": True}

    async def scroll(
        self,
        x: int,
        y: int,
        direction: str = "down",
        steps: int = 3,
        step_delay_ms: int = 100,
    ) -> dict[str, Any]:
        """Send real wheel events at one position.

        X11 maps wheel up/down to buttons 4/5 and wheel left/right to 6/7.
        """
        self.preflight()
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
        await _run(["xdotool", "mousemove", "--sync", str(x), str(y)])
        for _ in range(steps):
            await _run(["xdotool", "click", button])
            await asyncio.sleep(step_delay_ms / 1000)
        return {"success": True, "direction": direction, "steps": steps}

    async def type_text(self, text: str, delay_ms: int = 50) -> dict[str, Any]:
        """Type text into the focused window.

        ``--`` ends option parsing, so text starting with ``-`` is typed
        verbatim instead of being read as an xdotool flag.
        """
        self.preflight()
        await _run(
            ["xdotool", "type", "--delay", str(delay_ms), "--", text],
            timeout=max(30.0, len(text) * delay_ms / 1000 + 10),
        )
        return {"success": True, "chars_typed": len(text)}

    async def key(self, key: str, modifiers: list[str] | None = None) -> dict[str, Any]:
        """Press one key, optionally with modifiers held."""
        self.preflight()
        chord = "+".join([*(modifiers or []), key])
        await _run(["xdotool", "key", "--clearmodifiers", chord])
        return {"success": True}

    async def hotkey(self, keys: list[str]) -> dict[str, Any]:
        """Press several keys as one chord."""
        self.preflight()
        await _run(["xdotool", "key", "--clearmodifiers", "+".join(keys)])
        return {"success": True}

    async def wait(self, seconds: float = 1.0, condition: str | None = None) -> dict[str, Any]:
        """Sleep for a bounded interval."""
        await asyncio.sleep(max(0.0, min(seconds, 300.0)))
        return {"success": True, "waited_seconds": seconds}

    async def find_image(
        self,
        template_base64: str,
        threshold: float = 0.8,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Locate a template image on screen with normalized correlation."""
        if not OPENCV_AVAILABLE:
            raise ComputerUseUnavailableError(
                "Image search needs OpenCV. Install with: pip install opencv-python"
            )
        template_bytes = base64.b64decode(template_base64)
        template = cv2.imdecode(np.frombuffer(template_bytes, np.uint8), cv2.IMREAD_COLOR)
        if template is None:
            raise ValueError("template_base64 is not a decodable image")
        screenshot = await self.screenshot(region=region)
        screen_bytes = base64.b64decode(screenshot["image_base64"])
        screen = cv2.imdecode(np.frombuffer(screen_bytes, np.uint8), cv2.IMREAD_COLOR)
        if screen is None:  # pragma: no cover - screenshot always renders an image
            raise ComputerUseUnavailableError("The screenshot could not be decoded")
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        height, width = template.shape[:2]
        found = bool(score >= threshold)
        offset_x = int(region["x"]) if region else 0
        offset_y = int(region["y"]) if region else 0
        return {
            "found": found,
            "confidence": float(score),
            "location": (
                {"x": int(location[0]) + offset_x, "y": int(location[1]) + offset_y}
                if found
                else None
            ),
            "size": {"width": int(width), "height": int(height)},
        }

    async def ocr(
        self,
        region: dict[str, int] | None = None,
        language: str = "eng",
    ) -> dict[str, Any]:
        """Read text from the screen with Tesseract."""
        if not (TESSERACT_AVAILABLE and pytesseract is not None):
            raise ComputerUseUnavailableError(
                "OCR needs Tesseract. Install with: "
                "sudo apt-get install tesseract-ocr && pip install pytesseract"
            )
        screenshot = await self.screenshot(region=region)
        handle, path = tempfile.mkstemp(suffix=".png", dir=self._temp_dir)
        os.close(handle)
        try:
            Path(path).write_bytes(base64.b64decode(screenshot["image_base64"]))
            text = await asyncio.to_thread(pytesseract.image_to_string, path, lang=language)
        finally:
            Path(path).unlink(missing_ok=True)
        return {"text": text.strip()}


# ──────────────────────────────────────────────────────────────────────
# RFB (VNC) client
# ──────────────────────────────────────────────────────────────────────

#: X11 keysym values for the control keys a model can name.
_KEYSYMS = {
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "tab": 0xFF09,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "space": 0x0020,
    "shift": 0xFFE1,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "super": 0xFFEB,
    "meta": 0xFFEB,
    "win": 0xFFEB,
    **{f"f{number}": 0xFFBD + number for number in range(1, 13)},
}


def _keysym(name: str) -> int:
    """Map a key name or single character to its X11 keysym."""
    lowered = name.lower()
    if lowered in _KEYSYMS:
        return _KEYSYMS[lowered]
    if len(name) == 1:
        code = ord(name)
        return code if code < 0x100 else 0x01000000 + code
    raise ValueError(f"Unknown key name: {name}")


class RFBClient:
    """Minimal RFB 3.8 client that decodes raw and copy rectangles.

    The client requests 32-bit true colour pixels and only the Raw and
    CopyRect encodings, so every update it receives can be decoded without
    implementing the optional compression codecs.
    """

    def __init__(self, host: str, port: int, password: str | None = None):
        self.host = host
        self.port = port
        self.password = password
        self.width = 0
        self.height = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def connected(self) -> bool:
        """Whether the TCP session and handshake are complete."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 10.0) -> None:
        """Open the connection, authenticate, and select the pixel format."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout
        )
        await self._handshake()
        await self._set_pixel_format()
        await self._set_encodings()

    async def _handshake(self) -> None:
        """Negotiate the version and complete the chosen security type."""
        assert self._reader is not None and self._writer is not None
        version = await self._reader.readexactly(12)
        if not version.startswith(b"RFB "):
            raise ComputerUseUnavailableError("The VNC server did not send an RFB banner")
        if version >= b"RFB 003.008":
            self._writer.write(b"RFB 003.008\n")
            await self._writer.drain()
            count = (await self._reader.readexactly(1))[0]
            if count == 0:
                reason_length = struct.unpack(">I", await self._reader.readexactly(4))[0]
                reason = await self._reader.readexactly(reason_length)
                raise ComputerUseUnavailableError(
                    f"VNC server refused the connection: {reason.decode(errors='replace')}"
                )
            offered = set(await self._reader.readexactly(count))
            if 2 in offered and self.password:
                security = 2
            elif 1 in offered:
                security = 1
            else:
                raise ComputerUseUnavailableError(
                    f"VNC server offered unsupported security types: {sorted(offered)}"
                )
            self._writer.write(bytes([security]))
            await self._writer.drain()
            if security == 2:
                await self._vnc_auth()
            elif security == 1:
                result = struct.unpack(">I", await self._reader.readexactly(4))[0]
                if result != 0:
                    raise ComputerUseUnavailableError("The VNC server rejected the session")
        else:
            self._writer.write(b"RFB 003.003\n")
            await self._writer.drain()
            security = struct.unpack(">I", await self._reader.readexactly(4))[0]
            if security == 1:
                pass
            elif security == 2:
                await self._vnc_auth()
            else:
                raise ComputerUseUnavailableError(
                    f"VNC server offered unsupported security type {security}"
                )
        self._writer.write(b"\x01")
        await self._writer.drain()
        header = await self._reader.readexactly(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name_length = struct.unpack(">I", header[20:24])[0]
        if name_length:
            # The server name is UTF-8 text the client must consume to keep
            # the stream aligned; it is not needed for input or screenshots.
            await self._reader.readexactly(name_length)

    async def _vnc_auth(self) -> None:
        """Answer the DES challenge with the configured password."""
        assert self._reader is not None and self._writer is not None
        challenge = await self._reader.readexactly(16)
        if not PYCRYPTODOME_AVAILABLE:
            raise ComputerUseUnavailableError(
                "VNC password authentication needs pycryptodome. "
                "Install with: pip install pycryptodome"
            )
        key = (self.password or "")[:8].ljust(8, "\x00").encode()
        # VNC uses the first eight bytes of the password as a DES key with
        # each byte's bits reversed.
        key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in key)
        response = DES.new(key, DES.MODE_ECB).encrypt(challenge)
        self._writer.write(response)
        await self._writer.drain()
        result = struct.unpack(">I", await self._reader.readexactly(4))[0]
        if result != 0:
            raise ComputerUseUnavailableError("VNC authentication failed")

    async def _set_pixel_format(self) -> None:
        """Ask for 32-bit true colour in the server's native byte order."""
        assert self._writer is not None
        self._writer.write(
            struct.pack(
                ">BxxxBBBBHHHBBBxxx",
                0,
                32,
                24,
                1,
                1,
                255,
                255,
                255,
                16,
                8,
                0,
            )
        )
        await self._writer.drain()

    async def _set_encodings(self) -> None:
        """Restrict updates to Raw and CopyRect."""
        assert self._writer is not None
        self._writer.write(struct.pack(">BxH", 2, 2) + struct.pack(">ii", 0, 1))
        await self._writer.drain()

    async def request_update(self, incremental: bool = False) -> None:
        """Ask for a full or incremental framebuffer update."""
        assert self._writer is not None
        self._writer.write(
            struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height)
        )
        await self._writer.drain()

    async def read_update(self) -> tuple[bytes, int, int]:
        """Read one framebuffer update and return tight RGBA pixels."""
        assert self._reader is not None
        message = (await self._reader.readexactly(1))[0]
        if message != 0:
            raise ComputerUseUnavailableError(f"Unexpected RFB message type {message}")
        await self._reader.readexactly(1)
        rectangles = struct.unpack(">H", await self._reader.readexactly(2))[0]
        canvas = bytearray(self.width * self.height * 4)
        for _ in range(rectangles):
            x, y, width, height, encoding = struct.unpack(
                ">HHHHi", await self._reader.readexactly(12)
            )
            if encoding == 0:
                raw = await self._reader.readexactly(width * height * 4)
                for row in range(height):
                    source = row * width * 4
                    target = ((y + row) * self.width + x) * 4
                    canvas[target : target + width * 4] = raw[source : source + width * 4]
                # RFB true colour arrives as BGRX; convert after all
                # rectangles so CopyRect sources are already in RGBA order.
            elif encoding == 1:
                source_x, source_y = struct.unpack(">HH", await self._reader.readexactly(4))
                for row in range(height):
                    source = ((source_y + row) * self.width + source_x) * 4
                    target = ((y + row) * self.width + x) * 4
                    canvas[target : target + width * 4] = canvas[source : source + width * 4]
            else:
                raise ComputerUseUnavailableError(
                    f"VNC server used unsupported encoding {encoding}"
                )
        for offset in range(0, len(canvas), 4):
            canvas[offset], canvas[offset + 2] = canvas[offset + 2], canvas[offset]
            canvas[offset + 3] = 255
        return bytes(canvas), self.width, self.height

    async def pointer(self, x: int, y: int, button_mask: int = 0) -> None:
        """Send a pointer position and button mask."""
        assert self._writer is not None
        self._writer.write(struct.pack(">BBHH", 5, button_mask, x, y))
        await self._writer.drain()

    async def key(self, keysym: int, down: bool) -> None:
        """Send one key press or release."""
        assert self._writer is not None
        self._writer.write(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))
        await self._writer.drain()

    async def close(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None


class VNCComputerRuntime:
    """Run a private Xvfb display, expose it over x11vnc, and drive it by RFB."""

    def __init__(self, config: ComputerUseConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.session_id = str(uuid.uuid4())
        self._display = f":{max(1, config.vnc_port - 5900)}"
        self._xvfb: asyncio.subprocess.Process | None = None
        self._x11vnc: asyncio.subprocess.Process | None = None
        self._client = RFBClient(config.vnc_host, config.vnc_port, config.vnc_password)
        self._temp_dir = Path(
            config.temp_dir or tempfile.mkdtemp(prefix=f"deepspace_vnc_{self.session_id}_")
        )

    async def start(self) -> None:
        """Start Xvfb and x11vnc and connect to the new display.

        Raises:
            ComputerUseUnavailableError: A required program is missing or a
                process exits before accepting a connection.
        """
        missing = [tool for tool in ("Xvfb", "x11vnc") if not _tool_path(tool)]
        if missing:
            raise ComputerUseUnavailableError(
                "The VNC provider needs "
                + " and ".join(missing)
                + ". Install with: sudo apt-get install xvfb x11vnc"
            )
        self._xvfb = await asyncio.create_subprocess_exec(
            "Xvfb",
            self._display,
            "-screen",
            "0",
            f"{self.config.display_width}x{self.config.display_height}x24",
            "-nolisten",
            "tcp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.5)
        if self._xvfb.returncode is not None:
            error = (await self._xvfb.stderr.read()).decode(errors="replace")
            raise ComputerUseUnavailableError(f"Xvfb exited at startup: {error.strip()}")
        password = self.config.vnc_password
        authentication = ["-passwd", password] if password else ["-nopw"]
        self._x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self._display,
            "-rfbport",
            str(self.config.vnc_port),
            "-localhost",
            "-forever",
            "-shared",
            "-noxdamage",
            *authentication,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(40):
            if self._x11vnc.returncode is not None:
                error = (await self._x11vnc.stderr.read()).decode(errors="replace")
                await self.stop()
                raise ComputerUseUnavailableError(f"x11vnc exited at startup: {error.strip()}")
            try:
                await self._client.connect(timeout=1.0)
                return
            except (OSError, TimeoutError):
                await asyncio.sleep(0.25)
        await self.stop()
        raise ComputerUseUnavailableError("x11vnc did not accept a connection")

    async def stop(self) -> None:
        """Close RFB and stop both background processes."""
        await self._client.close()
        for process in (self._x11vnc, self._xvfb):
            if process is None or process.returncode is not None:
                continue
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self.config.temp_dir is None:
            await asyncio.to_thread(shutil.rmtree, self._temp_dir, True)

    async def screenshot(
        self,
        format: str = "png",
        quality: int = 85,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Capture the RFB framebuffer."""
        if not PIL_AVAILABLE:
            raise ComputerUseUnavailableError(
                "VNC screenshots need Pillow. Install with: pip install Pillow"
            )
        await self._client.request_update(incremental=False)
        pixels, width, height = await self._client.read_update()
        image = Image.frombytes("RGBA", (width, height), pixels)
        if region:
            left = int(region["x"])
            top = int(region["y"])
            image = image.crop(
                (left, top, left + int(region["width"]), top + int(region["height"]))
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG" if format in {"jpeg", "jpg"} else "PNG", quality=quality)
        data = buffer.getvalue()
        return {
            "image_base64": base64.b64encode(data).decode(),
            "width": image.width,
            "height": image.height,
            "format": "jpeg" if format in {"jpeg", "jpg"} else "png",
        }

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        delay_ms: int = 100,
    ) -> dict[str, Any]:
        """Click through RFB pointer events."""
        mask = {"left": 1, "middle": 2, "right": 4}[button]
        await self._client.pointer(x, y, 0)
        for index in range(clicks):
            await self._client.pointer(x, y, mask)
            await self._client.pointer(x, y, 0)
            if index + 1 < clicks:
                await asyncio.sleep(delay_ms / 1000)
        return {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}

    async def double_click(self, x: int, y: int) -> dict[str, Any]:
        """Double-click through RFB pointer events."""
        return await self.click(x, y, clicks=2)

    async def right_click(self, x: int, y: int) -> dict[str, Any]:
        """Right-click through RFB pointer events."""
        return await self.click(x, y, button="right")

    async def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 500,
    ) -> dict[str, Any]:
        """Drag with the primary button held."""
        steps = max(1, duration_ms // 50)
        await self._client.pointer(x1, y1, 1)
        for step in range(1, steps + 1):
            await self._client.pointer(
                int(x1 + (x2 - x1) * step / steps),
                int(y1 + (y2 - y1) * step / steps),
                1,
            )
            await asyncio.sleep(duration_ms / 1000 / steps)
        await self._client.pointer(x2, y2, 0)
        return {"success": True}

    async def scroll(
        self,
        x: int,
        y: int,
        direction: str = "down",
        steps: int = 3,
        step_delay_ms: int = 100,
    ) -> dict[str, Any]:
        """Scroll with X11 wheel button masks."""
        button_mask = {"up": 8, "down": 16, "left": 32, "right": 64}[direction]
        await self._client.pointer(x, y, 0)
        for _ in range(steps):
            await self._client.pointer(x, y, button_mask)
            await self._client.pointer(x, y, 0)
            await asyncio.sleep(step_delay_ms / 1000)
        return {"success": True, "direction": direction, "steps": steps}

    async def type_text(self, text: str, delay_ms: int = 50) -> dict[str, Any]:
        """Type text one keysym at a time."""
        for character in text:
            keysym = _keysym(character)
            await self._client.key(keysym, down=True)
            await self._client.key(keysym, down=False)
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
        return {"success": True, "chars_typed": len(text)}

    async def key(self, key: str, modifiers: list[str] | None = None) -> dict[str, Any]:
        """Press one key with optional modifiers."""
        pressed = [_keysym(modifier) for modifier in (modifiers or [])]
        target = _keysym(key)
        for keysym in pressed:
            await self._client.key(keysym, down=True)
        await self._client.key(target, down=True)
        await self._client.key(target, down=False)
        for keysym in reversed(pressed):
            await self._client.key(keysym, down=False)
        return {"success": True}

    async def hotkey(self, keys: list[str]) -> dict[str, Any]:
        """Press several keys as one chord."""
        return await self.key(keys[-1], modifiers=keys[:-1])

    async def wait(self, seconds: float = 1.0, condition: str | None = None) -> dict[str, Any]:
        """Sleep for a bounded interval."""
        await asyncio.sleep(max(0.0, min(seconds, 300.0)))
        return {"success": True, "waited_seconds": seconds}

    async def find_image(
        self,
        template_base64: str,
        threshold: float = 0.8,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Locate a template image in the framebuffer."""
        if not OPENCV_AVAILABLE:
            raise ComputerUseUnavailableError(
                "Image search needs OpenCV. Install with: pip install opencv-python"
            )
        template = cv2.imdecode(
            np.frombuffer(base64.b64decode(template_base64), np.uint8), cv2.IMREAD_COLOR
        )
        if template is None:
            raise ValueError("template_base64 is not a decodable image")
        screenshot = await self.screenshot(region=region)
        screen = cv2.imdecode(
            np.frombuffer(base64.b64decode(screenshot["image_base64"]), np.uint8),
            cv2.IMREAD_COLOR,
        )
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        height, width = template.shape[:2]
        found = bool(score >= threshold)
        offset_x = int(region["x"]) if region else 0
        offset_y = int(region["y"]) if region else 0
        return {
            "found": found,
            "confidence": float(score),
            "location": (
                {"x": int(location[0]) + offset_x, "y": int(location[1]) + offset_y}
                if found
                else None
            ),
            "size": {"width": int(width), "height": int(height)},
        }

    async def ocr(
        self,
        region: dict[str, int] | None = None,
        language: str = "eng",
    ) -> dict[str, Any]:
        """Read text from the framebuffer with Tesseract."""
        if not (TESSERACT_AVAILABLE and pytesseract is not None):
            raise ComputerUseUnavailableError(
                "OCR needs Tesseract. Install with: "
                "sudo apt-get install tesseract-ocr && pip install pytesseract"
            )
        screenshot = await self.screenshot(region=region)
        handle, path = tempfile.mkstemp(suffix=".png", dir=self._temp_dir)
        os.close(handle)
        try:
            Path(path).write_bytes(base64.b64decode(screenshot["image_base64"]))
            text = await asyncio.to_thread(pytesseract.image_to_string, path, lang=language)
        finally:
            Path(path).unlink(missing_ok=True)
        return {"text": text.strip()}


# ──────────────────────────────────────────────────────────────────────
# Runtime selection
# ──────────────────────────────────────────────────────────────────────

_RUNTIME: LocalComputerRuntime | VNCComputerRuntime | None = None
_RUNTIME_LOCK = asyncio.Lock()


_PROVIDER_ALIASES: dict[str, Literal["local", "vnc"]] = {
    "local": "local",
    "vnc": "vnc",
    "cua": "vnc",
    "screenshot_vnc": "vnc",
}


def _config_for(settings: Settings) -> ComputerUseConfig:
    """Build the runtime configuration from deployment settings.

    ``screenshot_rdp`` is not implemented; it stays unmapped so startup
    reports the unsupported provider instead of silently using another one.
    """
    raw_provider = str(getattr(settings, "deepspace_computer_use_provider", "local"))
    provider = _PROVIDER_ALIASES.get(raw_provider, raw_provider)
    return ComputerUseConfig(
        enabled=bool(getattr(settings, "deepspace_computer_use_enabled", False)),
        provider=provider,  # type: ignore[arg-type]
        vnc_host=str(getattr(settings, "deepspace_computer_use_vnc_host", "127.0.0.1")),
        vnc_port=int(getattr(settings, "deepspace_computer_use_vnc_port", 5900)),
        vnc_password=getattr(settings, "deepspace_computer_use_vnc_password", None),
        display_width=int(getattr(settings, "deepspace_computer_use_display_width", 1920)),
        display_height=int(getattr(settings, "deepspace_computer_use_display_height", 1080)),
        max_screenshot_size_mb=int(
            getattr(settings, "deepspace_computer_use_max_screenshot_size_mb", 10)
        ),
        action_timeout_ms=int(
            getattr(settings, "deepspace_computer_use_action_timeout_ms", 30_000)
        ),
        click_delay_ms=int(getattr(settings, "deepspace_computer_use_click_delay_ms", 100)),
        type_delay_ms=int(getattr(settings, "deepspace_computer_use_type_delay_ms", 50)),
        temp_dir=getattr(settings, "deepspace_computer_use_temp_dir", None),
    )


async def get_computer_runtime(settings: Settings) -> LocalComputerRuntime | VNCComputerRuntime:
    """Return the process-wide desktop runtime, starting it on first use.

    Args:
        settings: Deployment settings that choose and configure the provider.

    Returns:
        The running local or VNC runtime.

    Raises:
        ComputerUseUnavailableError: Computer use is disabled or cannot start.
    """
    global _RUNTIME
    async with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        config = _config_for(settings)
        if not config.enabled:
            raise ComputerUseUnavailableError(
                "Computer use is disabled. Set DEEPSPACE_COMPUTER_USE_ENABLED=true."
            )
        if config.provider == "vnc":
            runtime: LocalComputerRuntime | VNCComputerRuntime = VNCComputerRuntime(
                config, settings
            )
        elif config.provider == "local":
            runtime = LocalComputerRuntime(config, settings)
        else:
            raise ComputerUseUnavailableError(
                f"Unsupported computer-use provider: {config.provider}. "
                "Use 'local', 'vnc', 'cua', or 'screenshot_vnc'."
            )
        await runtime.start()
        _RUNTIME = runtime
        return runtime


async def shutdown_computer_runtime() -> None:
    """Stop the process-wide desktop runtime, if one started."""
    global _RUNTIME
    async with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
    if runtime is not None:
        await runtime.stop()


async def _runtime(context: ToolContext) -> LocalComputerRuntime | VNCComputerRuntime:
    """Tool-handler shim that reaches the runtime for this request."""
    return await get_computer_runtime(context.settings)


# ──────────────────────────────────────────────────────────────────────
# Tool Arguments
# ──────────────────────────────────────────────────────────────────────


class ComputerScreenshotArguments(ToolArguments):
    """Capture the desktop or one region."""

    format: Literal["png", "jpeg"] | None = None
    quality: int | None = Field(default=None, ge=1, le=100)
    region: dict[str, int] | None = None


class ComputerClickArguments(ToolArguments):
    """Click at one screen position."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    button: Literal["left", "right", "middle"] | None = None
    clicks: int | None = Field(default=None, ge=1, le=3)
    delay_ms: int | None = Field(default=None, ge=0, le=5_000)


class ComputerDoubleClickArguments(ToolArguments):
    """Double-click at one screen position."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ComputerRightClickArguments(ToolArguments):
    """Right-click at one screen position."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ComputerDragArguments(ToolArguments):
    """Drag between two screen positions."""

    x1: int = Field(ge=0)
    y1: int = Field(ge=0)
    x2: int = Field(ge=0)
    y2: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=50, le=10_000)


class ComputerScrollArguments(ToolArguments):
    """Scroll with the wheel at one position."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    direction: Literal["up", "down", "left", "right"]
    steps: int | None = Field(default=None, ge=1, le=50)
    step_delay_ms: int | None = Field(default=None, ge=10, le=1_000)


class ComputerTypeArguments(ToolArguments):
    """Type text into the focused window."""

    text: str = Field(min_length=1, max_length=10_000)
    delay_ms: int | None = Field(default=None, ge=0, le=1_000)


class ComputerKeyArguments(ToolArguments):
    """Press one key with optional modifiers."""

    key: str = Field(min_length=1, max_length=50)
    modifiers: list[str] | None = Field(default=None, max_length=4)


class ComputerHotkeyArguments(ToolArguments):
    """Press several keys as one chord."""

    keys: list[str] = Field(min_length=2, max_length=5)


class ComputerWaitArguments(ToolArguments):
    """Wait before the next desktop action."""

    seconds: float | None = Field(default=None, ge=0, le=300)
    condition: str | None = Field(default=None, max_length=500)


class ComputerFindImageArguments(ToolArguments):
    """Find a template image on screen."""

    template_base64: str = Field(min_length=1)
    threshold: float | None = Field(default=None, ge=0, le=1)
    region: dict[str, int] | None = None


class ComputerOcrArguments(ToolArguments):
    """Read text from the screen."""

    region: dict[str, int] | None = None
    language: str | None = Field(default=None, max_length=40)


# ──────────────────────────────────────────────────────────────────────
# Tool handlers
# ──────────────────────────────────────────────────────────────────────


async def _computer_screenshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.screenshot(
        arguments.get("format", "png"), arguments.get("quality", 85), arguments.get("region")
    )


async def _computer_click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.click(
        arguments["x"],
        arguments["y"],
        arguments.get("button", "left"),
        arguments.get("clicks", 1),
        arguments.get("delay_ms", 100),
    )


async def _computer_double_click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.double_click(arguments["x"], arguments["y"])


async def _computer_right_click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.right_click(arguments["x"], arguments["y"])


async def _computer_drag(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.drag(
        arguments["x1"],
        arguments["y1"],
        arguments["x2"],
        arguments["y2"],
        arguments.get("duration_ms", 500),
    )


async def _computer_scroll(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.scroll(
        arguments["x"],
        arguments["y"],
        arguments.get("direction", "down"),
        arguments.get("steps", 3),
        arguments.get("step_delay_ms", 100),
    )


async def _computer_type(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.type_text(arguments["text"], arguments.get("delay_ms", 50))


async def _computer_key(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.key(arguments["key"], arguments.get("modifiers"))


async def _computer_hotkey(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.hotkey(arguments["keys"])


async def _computer_wait(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.wait(arguments.get("seconds", 1.0), arguments.get("condition"))


async def _computer_find_image(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.find_image(
        arguments["template_base64"],
        arguments.get("threshold", 0.8),
        arguments.get("region"),
    )


async def _computer_ocr(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    runtime = await _runtime(context)
    return await runtime.ocr(arguments.get("region"), arguments.get("language", "eng"))


COMPUTER_USE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="computer_screenshot",
        description="Capture the desktop or one region of it",
        properties={
            "format": string_property("Image format", enum=["png", "jpeg"]),
            "quality": integer_property("JPEG quality", minimum=1, maximum=100),
            "region": object_property("Crop region {x, y, width, height}"),
        },
        required=[],
        arguments_model=ComputerScreenshotArguments,
        mode="read",
        handler=_computer_screenshot,
    ),
    define_native_tool(
        name="computer_click",
        description="Click at screen coordinates",
        properties={
            "x": integer_property("X coordinate", minimum=0),
            "y": integer_property("Y coordinate", minimum=0),
            "button": string_property("Mouse button", enum=["left", "right", "middle"]),
            "clicks": integer_property("Number of clicks", minimum=1, maximum=3),
            "delay_ms": integer_property(
                "Delay between clicks in milliseconds", minimum=0, maximum=5_000
            ),
        },
        required=["x", "y"],
        arguments_model=ComputerClickArguments,
        mode="write",
        handler=_computer_click,
    ),
    define_native_tool(
        name="computer_double_click",
        description="Double-click at screen coordinates",
        properties={
            "x": integer_property("X coordinate", minimum=0),
            "y": integer_property("Y coordinate", minimum=0),
        },
        required=["x", "y"],
        arguments_model=ComputerDoubleClickArguments,
        mode="write",
        handler=_computer_double_click,
    ),
    define_native_tool(
        name="computer_right_click",
        description="Right-click at screen coordinates",
        properties={
            "x": integer_property("X coordinate", minimum=0),
            "y": integer_property("Y coordinate", minimum=0),
        },
        required=["x", "y"],
        arguments_model=ComputerRightClickArguments,
        mode="write",
        handler=_computer_right_click,
    ),
    define_native_tool(
        name="computer_drag",
        description="Drag the pointer from one position to another",
        properties={
            "x1": integer_property("Start X", minimum=0),
            "y1": integer_property("Start Y", minimum=0),
            "x2": integer_property("End X", minimum=0),
            "y2": integer_property("End Y", minimum=0),
            "duration_ms": integer_property(
                "Drag duration in milliseconds", minimum=50, maximum=10_000
            ),
        },
        required=["x1", "y1", "x2", "y2"],
        arguments_model=ComputerDragArguments,
        mode="write",
        handler=_computer_drag,
    ),
    define_native_tool(
        name="computer_scroll",
        description="Scroll with the mouse wheel at a position",
        properties={
            "x": integer_property("X coordinate", minimum=0),
            "y": integer_property("Y coordinate", minimum=0),
            "direction": string_property("Scroll direction", enum=["up", "down", "left", "right"]),
            "steps": integer_property("Wheel steps", minimum=1, maximum=50),
            "step_delay_ms": integer_property(
                "Delay between wheel steps in milliseconds", minimum=10, maximum=1_000
            ),
        },
        required=["x", "y", "direction"],
        arguments_model=ComputerScrollArguments,
        mode="write",
        handler=_computer_scroll,
    ),
    define_native_tool(
        name="computer_type",
        description="Type text into the focused window",
        properties={
            "text": string_property("Text to type", min_length=1, max_length=10_000),
            "delay_ms": integer_property(
                "Delay between keystrokes in milliseconds", minimum=0, maximum=1_000
            ),
        },
        required=["text"],
        arguments_model=ComputerTypeArguments,
        mode="write",
        handler=_computer_type,
    ),
    define_native_tool(
        name="computer_key",
        description="Press one key, optionally with modifiers",
        properties={
            "key": string_property("Key name", min_length=1, max_length=50),
            "modifiers": array_property("Modifier keys", max_items=4),
        },
        required=["key"],
        arguments_model=ComputerKeyArguments,
        mode="write",
        handler=_computer_key,
    ),
    define_native_tool(
        name="computer_hotkey",
        description="Press several keys as one chord",
        properties={
            "keys": array_property("Keys in chord order", min_items=2, max_items=5),
        },
        required=["keys"],
        arguments_model=ComputerHotkeyArguments,
        mode="write",
        handler=_computer_hotkey,
    ),
    define_native_tool(
        name="computer_wait",
        description="Wait before the next desktop action",
        properties={
            "seconds": number_property("Seconds to wait", minimum=0, maximum=300),
            "condition": string_property(
                "Optional note describing what is awaited", max_length=500
            ),
        },
        required=[],
        arguments_model=ComputerWaitArguments,
        mode="read",
        handler=_computer_wait,
    ),
    define_native_tool(
        name="computer_find_image",
        description="Find a template image on screen",
        properties={
            "template_base64": string_property("Template image as base64", min_length=1),
            "threshold": number_property("Match threshold", minimum=0, maximum=1),
            "region": object_property("Search region {x, y, width, height}"),
        },
        required=["template_base64"],
        arguments_model=ComputerFindImageArguments,
        mode="read",
        handler=_computer_find_image,
    ),
    define_native_tool(
        name="computer_ocr",
        description="Read text from the screen with OCR",
        properties={
            "region": object_property("OCR region {x, y, width, height}"),
            "language": string_property("Tesseract language", max_length=40),
        },
        required=[],
        arguments_model=ComputerOcrArguments,
        mode="read",
        handler=_computer_ocr,
    ),
]

__all__ = [
    "COMPUTER_USE_TOOLS",
    "ComputerUseConfig",
    "ComputerUseUnavailableError",
    "ComputerScreenshotArguments",
    "LocalComputerRuntime",
    "RFBClient",
    "VNCComputerRuntime",
    "get_computer_runtime",
    "shutdown_computer_runtime",
]
