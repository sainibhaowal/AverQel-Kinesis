"""The DeepSpace browser runtime.

The Browser tab of the side panel shows a real page.  This module owns the
Chromium process the deployment runs and the DevTools protocol connection the
panel's commands travel over.

One Chromium process serves the whole deployment.  A browser process costs
hundreds of megabytes, and the panel's pages do not need separate processes,
so the runtime starts one browser and gives every conversation a page target
inside a browser context of its own.  A context has its own cookies and its
own storage, so one conversation's pages cannot read another's.

A session belongs to the tenant, the user, and the conversation that opened
it.  The manager answers a request for a session that belongs to somebody
else exactly as it answers a request for a session that does not exist, so a
caller cannot learn that another tenant's session exists.

The browser's own history is the history.  Going back and going forward move
through the entries the browser recorded, not through a list of addresses the
caller typed, so a redirect and a link inside the page are steps in the
history like any other step.

The Chromium process never inherits the deployment's environment.  It receives
a reduced environment built the way a terminal session's child receives one,
so a credential held by the server is not readable from inside the browser.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import importlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from weakref import WeakKeyDictionary

from app.deepspace.services.terminals import build_session_environment

#: The idle time after which a session is swept, in seconds.
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0

#: The number of live sessions one deployment may hold.
DEFAULT_MAX_SESSIONS = 16

#: The time between two sweeps of the idle sessions, in seconds.
DEFAULT_SWEEP_INTERVAL_SECONDS = 30.0

#: The time the browser may take to open its debugging port, in seconds.
DEFAULT_LAUNCH_TIMEOUT_SECONDS = 30.0

#: The time a page may take to finish loading, in seconds.
DEFAULT_LOAD_TIMEOUT_SECONDS = 20.0

#: The width of the captured page, in pixels.
VIEWPORT_WIDTH = 1280

#: The height of the captured page, in pixels.
VIEWPORT_HEIGHT = 900

#: The largest message the browser connection accepts, in bytes.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

#: The most characters one typing request may insert.
MAX_TYPE_TEXT_LENGTH = 4_000

#: The farthest one scroll request may move the page, in pixels.
MAX_SCROLL_DELTA = 4_000

#: The time an input command waits for the page to react, in seconds.
INPUT_SETTLE_SECONDS = 0.15

#: The keys a request may press and the DevTools values each one sends.
PRESSABLE_KEYS: dict[str, tuple[str, str, int]] = {
    "Enter": ("Enter", "Enter", 13),
    "Tab": ("Tab", "Tab", 9),
    "Escape": ("Escape", "Escape", 27),
    "Backspace": ("Backspace", "Backspace", 8),
    "Delete": ("Delete", "Delete", 46),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
    "Home": ("Home", "Home", 36),
    "End": ("End", "End", 35),
    "PageUp": ("PageUp", "PageUp", 33),
    "PageDown": ("PageDown", "PageDown", 34),
    "Space": (" ", "Space", 32),
}

#: The modifier keys a request may hold and the DevTools bit each one sets.
KEY_MODIFIERS: dict[str, int] = {
    "Alt": 1,
    "Control": 2,
    "Meta": 4,
    "Shift": 8,
}

#: The browser executables a host is asked for, in the order they are tried.
BROWSER_EXECUTABLE_NAMES: tuple[str, ...] = (
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
    "chrome",
)

#: The places a host keeps a browser that is not on the program path.
BROWSER_EXECUTABLE_PATHS: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
)

#: The file the browser writes the port it listens on into.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"

#: The address of the page a session starts on.
BLANK_URL = "about:blank"

#: The prefix of the private directory one browser keeps its profile in.
BROWSER_PROFILE_PREFIX = "averqel-br-"

#: The longest path the browser accepts for its temporary directory.
#:
#: A browser whose temporary directory path is longer than this stops with a
#: trace trap before it opens its debugging port, so the runtime keeps the
#: profile in a short directory.
MAX_BROWSER_TEMP_PATH = 60


class BrowserRuntimeError(RuntimeError):
    """A browser session could not be opened, used, or closed."""


class BrowserUnavailableError(BrowserRuntimeError):
    """The deployment has no browser runtime, or the host has no browser."""


class BrowserSessionNotFoundError(BrowserRuntimeError):
    """No live browser session with that id belongs to the caller."""


class BrowserSessionLimitError(BrowserRuntimeError):
    """The deployment's browser session limit is reached."""


class BrowserNavigationError(BrowserRuntimeError):
    """The browser refused the address it was asked to open."""


class BrowserProtocolError(BrowserRuntimeError):
    """The browser did not answer a DevTools protocol command."""


def _setting_bool(settings: Any, name: str, default: bool) -> bool:
    """Read one boolean setting, falling back to a default.

    Args:
        settings: The deployment settings in force.
        name: The setting's name.
        default: The value to use when the setting is absent.

    Returns:
        The setting's value.
    """
    value = getattr(settings, name, None)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _setting_float(settings: Any, name: str, default: float) -> float:
    """Read one number setting, falling back to a default.

    Args:
        settings: The deployment settings in force.
        name: The setting's name.
        default: The value to use when the setting is absent or unusable.

    Returns:
        The setting's value.
    """
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _setting_str(settings: Any, name: str, default: str = "") -> str:
    """Read one text setting, falling back to a default.

    Args:
        settings: The deployment settings in force.
        name: The setting's name.
        default: The value to use when the setting is absent.

    Returns:
        The setting's value.
    """
    value = getattr(settings, name, None)
    if value is None:
        return default
    return str(value).strip()


def browser_runtime_enabled(settings: Any) -> bool:
    """Report whether the deployment turned the browser runtime on.

    Args:
        settings: The deployment settings in force.

    Returns:
        True when the deployment runs a browser runtime.
    """
    return _setting_bool(settings, "deepspace_browser_enabled", False)


def validate_browser_url(value: str) -> str:
    """Return the address the browser may open.

    Only ``http`` and ``https`` addresses are opened.  The rule keeps a
    ``javascript:`` address and a ``file:`` address out of the browser, and
    the browser itself has no business reading the deployment's own disk.

    Args:
        value: The address the caller asked for.

    Returns:
        The normalized address.

    Raises:
        BrowserNavigationError: When the address is empty, is not a URL, uses
            another scheme, or carries a user name or a password.
    """
    raw = str(value or "").strip()
    if not raw:
        raise BrowserNavigationError("Enter a URL to open.")
    if any(character.isspace() for character in raw):
        raise BrowserNavigationError("Enter a full URL, for example http://localhost:3000.")
    has_scheme = bool(
        re.match(r"^(https?|javascript|about|data|file|blob|mailto):", raw, re.I)
    ) or bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw))
    if not has_scheme:
        if (
            raw.startswith("localhost")
            or raw.startswith("127.0.0.1")
            or (":" in raw and not raw.startswith("http"))
        ):
            raw = f"http://{raw}"
        else:
            raw = f"https://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme and scheme not in {"http", "https"}:
        raise BrowserNavigationError("Only http:// and https:// URLs can be opened.")
    if not parsed.netloc:
        raise BrowserNavigationError("Enter a full URL, for example http://localhost:3000.")
    if parsed.username or parsed.password:
        raise BrowserNavigationError("A URL with a user name or a password cannot be opened.")
    return urlunparse(
        (scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, parsed.fragment)
    )


def _playwright_browser_candidates() -> list[Path]:
    """Return the Chromium builds a Playwright cache holds.

    Returns:
        The candidate paths, newest first.
    """
    cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.is_dir():
        return []
    candidates: list[Path] = []
    for directory in sorted(cache.glob("chromium-*"), reverse=True):
        candidates.append(directory / "chrome-linux64" / "chrome")
        candidates.append(
            directory / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        )
        candidates.append(directory / "chrome-win" / "chrome.exe")
    return candidates


def _puppeteer_browser_candidates() -> list[Path]:
    """Return the Chromium builds a Puppeteer cache holds.

    A host that ran Puppeteer or Chrome for Testing already holds a Chromium
    that speaks the DevTools protocol, so the runtime uses it without a
    deployment naming a path.

    Returns:
        The candidate paths, newest first.
    """
    cache = Path.home() / ".cache" / "puppeteer" / "chrome"
    if not cache.is_dir():
        return []
    candidates: list[Path] = []
    for directory in sorted(cache.glob("linux-*"), reverse=True):
        candidates.append(directory / "chrome-linux64" / "chrome")
        candidates.append(directory / "chrome-linux" / "chrome")
    for directory in sorted(cache.glob("win*"), reverse=True):
        candidates.append(directory / "chrome-win64" / "chrome.exe")
        candidates.append(directory / "chrome-win" / "chrome.exe")
    for directory in sorted(cache.glob("mac*"), reverse=True):
        candidates.append(
            directory
            / "chrome-mac-x64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
        candidates.append(
            directory
            / "chrome-mac-arm64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
    return candidates


def find_browser_executable(configured: str | None = None) -> str | None:
    """Return the browser this host can run.

    Args:
        configured: The path the deployment named, when it named one.

    Returns:
        The path of the browser, or None when the host has none.
    """
    if configured and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    for name in BROWSER_EXECUTABLE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSER_EXECUTABLE_PATHS:
        if Path(path).is_file():
            return path
    for candidate in (*_playwright_browser_candidates(), *_puppeteer_browser_candidates()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def build_browser_environment(
    user_data_dir: Path, *, mode: str = "workspace-write"
) -> dict[str, str]:
    """Build the reduced environment the Chromium process receives.

    The browser must not inherit the server's environment, because that
    environment holds the deployment's credentials.  The browser therefore
    receives the same reduced environment a terminal session's child
    receives, with the browser's own profile directory as its home.

    The browser's scratch files live in the profile directory itself.  A
    browser whose temporary directory path is long stops before it opens its
    debugging port, so the temporary directory is kept as short as the
    profile path allows.

    Args:
        user_data_dir: The directory the browser keeps its profile in.
        mode: The deployment's containment mode, reported to the browser.

    Returns:
        The environment for the browser process.
    """
    environment = build_session_environment(workspace=user_data_dir, shell="bash", mode=mode)
    environment.pop("DEEPSPACE_SHELL", None)
    environment.pop("DEEPSPACE_SHELL_KIND", None)
    environment.pop("PS1", None)
    environment["DEEPSPACE_BROWSER"] = "1"
    environment["TMPDIR"] = str(user_data_dir)
    return environment


def _browser_profile_dir() -> Path:
    """Return a private directory for one browser's profile.

    The directory is created in the shortest writable directory the host
    offers, because the browser stops before it opens its debugging port when
    the path of its temporary directory is long.

    Returns:
        The empty directory the browser keeps its profile in.

    Raises:
        BrowserUnavailableError: When the host has no writable directory
            short enough for the browser's profile.
    """
    for base in (Path("/tmp"), Path(tempfile.gettempdir())):
        if not base.is_dir() or not os.access(base, os.W_OK):
            continue
        candidate = Path(tempfile.mkdtemp(prefix=BROWSER_PROFILE_PREFIX, dir=str(base)))
        if len(str(candidate)) <= MAX_BROWSER_TEMP_PATH:
            return candidate
        shutil.rmtree(candidate, ignore_errors=True)
    raise BrowserUnavailableError(
        "This host has no writable directory short enough for the browser's profile."
    )


class DevToolsConnection:
    """The DevTools protocol connection to one browser process.

    The connection carries commands and their answers.  Every command carries
    an identifier, and the reader task hands each answer to the caller that
    waits for it.
    """

    def __init__(self, socket: Any) -> None:
        """Create a connection over an open socket.

        Args:
            socket: The open websocket to the browser.
        """
        self._socket = socket
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._reader: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, url: str, *, timeout_seconds: float = 20.0) -> DevToolsConnection:
        """Open a connection to a browser's debugging endpoint.

        Args:
            url: The websocket URL the browser published.
            timeout_seconds: The time the connection may take to open.

        Returns:
            The open connection.

        Raises:
            BrowserProtocolError: When the websocket client is not installed
                or the connection cannot be opened.
        """
        try:
            client = importlib.import_module("websockets.asyncio.client")
        except ImportError as exc:
            raise BrowserProtocolError(
                "The browser runtime needs the 'websockets' package to speak the DevTools protocol."
            ) from exc
        try:
            socket = await client.connect(
                url,
                proxy=None,
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=timeout_seconds,
            )
        except Exception as exc:
            raise BrowserProtocolError(
                "The browser's debugging endpoint refused the connection."
            ) from exc
        connection = cls(socket)
        connection._reader = asyncio.create_task(connection._read())
        return connection

    async def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Send one command and wait for its answer.

        Args:
            method: The DevTools method to call.
            params: The command's parameters.
            session_id: The attached session the command belongs to, when the
                command is addressed to a page.
            timeout_seconds: The time the answer may take.

        Returns:
            The answer's result.

        Raises:
            BrowserProtocolError: When the browser refuses the command or does
                not answer it in time.
        """
        if self._closed:
            raise BrowserProtocolError("The browser connection is closed.")
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._next_id += 1
            message_id = self._next_id
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[message_id] = future
            message: dict[str, Any] = {
                "id": message_id,
                "method": method,
                "params": dict(params or {}),
            }
            if session_id:
                message["sessionId"] = session_id
            try:
                await self._socket.send(json.dumps(message))
            except Exception as exc:
                self._pending.pop(message_id, None)
                raise BrowserProtocolError(
                    f"The browser connection failed while sending {method}."
                ) from exc
        try:
            return await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(message_id, None)
            raise BrowserProtocolError(f"The browser did not answer {method}.") from exc

    async def close(self) -> None:
        """Close the connection and release its reader."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        with contextlib.suppress(Exception):
            await self._socket.close()
        self._fail_pending()

    async def _read(self) -> None:
        """Hand every answer to the caller that waits for it."""
        try:
            async for raw in self._socket:
                self._dispatch(raw)
        except Exception:
            # A closed socket ends the reader; the pending calls are failed
            # with the reason below.
            pass
        finally:
            self._fail_pending()

    def _dispatch(self, raw: str | bytes) -> None:
        """Route one message to the caller that waits for it.

        Args:
            raw: The message the browser sent.
        """
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "the browser refused the command")
            future.set_exception(
                BrowserProtocolError(f"The browser refused the command: {detail}.")
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _fail_pending(self) -> None:
        """Fail every command that is still waiting for an answer."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(
                    BrowserProtocolError("The browser closed the connection before it answered.")
                )
        self._pending.clear()


#: The browser processes this process started, so none is left running.
_LIVE_PROCESSES: set[BrowserProcess] = set()


def _terminate_live_processes() -> None:
    """Stop every browser this process started."""
    for browser in list(_LIVE_PROCESSES):
        browser.terminate()


atexit.register(_terminate_live_processes)


@dataclass(slots=True, eq=False)
class BrowserProcess:
    """The Chromium process the deployment's browser sessions share."""

    #: The browser binary the process runs.
    executable: str
    #: The running browser process.
    process: asyncio.subprocess.Process
    #: The directory the browser keeps its profile in.
    user_data_dir: Path
    #: The port the browser's DevTools endpoint listens on.
    port: int
    #: The connection to the browser's DevTools endpoint.
    connection: DevToolsConnection

    @classmethod
    async def start(
        cls,
        *,
        executable: str,
        launch_timeout_seconds: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
        mode: str = "workspace-write",
    ) -> BrowserProcess:
        """Start one browser and connect to its DevTools endpoint.

        The browser is started with a debugging port the operating system
        chooses, and the port it chose is read from the profile directory.

        Args:
            executable: The browser binary to run.
            launch_timeout_seconds: The time the browser may take to open its
                debugging port.
            mode: The deployment's containment mode, reported to the browser.

        Returns:
            The running browser and its connection.

        Raises:
            BrowserUnavailableError: When the browser ends before it opens
                its debugging port, or does not open it in time.
            BrowserProtocolError: When the DevTools endpoint cannot be reached.
        """
        user_data_dir = _browser_profile_dir()
        environment = build_browser_environment(user_data_dir, mode=mode)
        argv = [
            executable,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise BrowserUnavailableError(
                f"The browser at {executable} could not be started on this host."
            ) from exc
        port = await _wait_for_devtools_port(process, user_data_dir, launch_timeout_seconds)
        endpoint = await _read_browser_endpoint(port)
        connection = await DevToolsConnection.connect(endpoint)
        browser = cls(
            executable=executable,
            process=process,
            user_data_dir=user_data_dir,
            port=port,
            connection=connection,
        )
        _LIVE_PROCESSES.add(browser)
        return browser

    def terminate(self) -> None:
        """Stop the browser and every process it started."""
        if self.process.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.terminate()

    async def close(self) -> None:
        """Close the connection and stop the browser."""
        _LIVE_PROCESSES.discard(self)
        await self.connection.close()
        self.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        shutil.rmtree(self.user_data_dir, ignore_errors=True)


async def _wait_for_devtools_port(
    process: asyncio.subprocess.Process,
    user_data_dir: Path,
    timeout_seconds: float,
) -> int:
    """Wait until the browser publishes the port its DevTools endpoint uses.

    Args:
        process: The running browser process.
        user_data_dir: The directory the browser keeps its profile in.
        timeout_seconds: The time the browser may take.

    Returns:
        The port the browser listens on.

    Raises:
        BrowserUnavailableError: When the browser ends or does not publish a
            port in time.
    """
    port_file = user_data_dir / DEVTOOLS_PORT_FILE
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise BrowserUnavailableError("The browser ended before it opened its debugging port.")
        port = _read_devtools_port(port_file)
        if port:
            return port
        await asyncio.sleep(0.1)
    raise BrowserUnavailableError("The browser did not open its debugging port in time.")


def _read_devtools_port(port_file: Path) -> int:
    """Read the port the browser published.

    Args:
        port_file: The file the browser writes the port into.

    Returns:
        The port, or 0 when the browser has not published one yet.
    """
    try:
        record = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    if not record:
        return 0
    try:
        return int(record[0].strip())
    except ValueError:
        return 0


async def _read_browser_endpoint(port: int) -> str:
    """Read the websocket URL the browser's DevTools endpoint listens on.

    Args:
        port: The port the browser listens on.

    Returns:
        The websocket URL.

    Raises:
        BrowserUnavailableError: When the browser published no endpoint.
    """
    try:
        httpx = importlib.import_module("httpx")
    except ImportError as exc:  # pragma: no cover - httpx is a declared dependency.
        raise BrowserUnavailableError(
            "The browser runtime needs the 'httpx' package to reach the browser."
        ) from exc
    async with httpx.AsyncClient(trust_env=False, timeout=10.0) as client:
        response = await client.get(f"http://127.0.0.1:{port}/json/version")
        response.raise_for_status()
        payload = response.json()
    url = str(payload.get("webSocketDebuggerUrl") or "")
    if not url:
        raise BrowserUnavailableError("The browser published no debugging endpoint.")
    return url


@dataclass(slots=True)
class BrowserSession:
    """One live browser session."""

    #: The session's opaque identifier.
    id: str
    #: The tenant that owns the session.
    tenant_id: uuid.UUID
    #: The user that owns the session.
    user_id: uuid.UUID
    #: The conversation that owns the session.
    conversation_id: uuid.UUID
    #: The browser context the session's page lives in.
    browser_context_id: str
    #: The page target the session drives.
    target_id: str
    #: The attached DevTools session the session's commands travel over.
    cdp_session_id: str
    #: The wall-clock time the session was opened.
    created_at: datetime
    #: The wall-clock time of the session's last use.
    last_used_at: datetime
    #: The monotonic deadline after which the session is swept.
    idle_deadline: float = field(default_factory=time.monotonic)

    def owned_by(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Report whether one caller owns the session.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            True when the session belongs to the caller.
        """
        return (
            self.tenant_id == tenant_id
            and self.user_id == user_id
            and self.conversation_id == conversation_id
        )

    def touch(self, idle_timeout_seconds: float) -> None:
        """Record this use and extend the idle deadline.

        Args:
            idle_timeout_seconds: The idle time after which the session is
                swept.
        """
        self.last_used_at = datetime.now(UTC)
        self.idle_deadline = time.monotonic() + idle_timeout_seconds

    def idle_seconds(self) -> float:
        """Return the time since the session was last used.

        Returns:
            The idle time in seconds.
        """
        return max(0.0, (datetime.now(UTC) - self.last_used_at).total_seconds())


@dataclass(frozen=True, slots=True)
class BrowserPage:
    """What one page of a browser session looks like at one moment."""

    #: The address the browser is showing.
    url: str
    #: The title of the page.
    title: str
    #: The screenshot of the page, as PNG bytes.
    screenshot: bytes
    #: Whether the browser's history holds an earlier page.
    can_go_back: bool
    #: Whether the browser's history holds a later page.
    can_go_forward: bool


class BrowserManager:
    """The registry of live browser sessions and the browser behind them.

    The manager starts one Chromium process for the deployment, gives every
    conversation a page of its own, sweeps the sessions that went idle, and
    answers every request for a session only to the tenant, the user, and the
    conversation that own it.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        launch_timeout_seconds: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
        load_timeout_seconds: float = DEFAULT_LOAD_TIMEOUT_SECONDS,
        sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        """Create the manager.

        Args:
            executable: The browser binary to run, when the deployment named
                one.  Without it the manager looks for a browser on the host.
            idle_timeout_seconds: The idle time after which a session is
                swept.
            max_sessions: The number of live sessions the deployment may
                hold.
            launch_timeout_seconds: The time the browser may take to open its
                debugging port.
            load_timeout_seconds: The time a page may take to finish loading.
            sweep_interval_seconds: The time between two sweeps of the idle
                sessions.
        """
        self._sessions: dict[str, BrowserSession] = {}
        self._process: BrowserProcess | None = None
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self.executable = executable
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self.launch_timeout_seconds = max(1.0, float(launch_timeout_seconds))
        self.load_timeout_seconds = max(1.0, float(load_timeout_seconds))
        self.sweep_interval_seconds = max(0.05, float(sweep_interval_seconds))

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    @property
    def browser_pid(self) -> int | None:
        """Return the process id of the browser, when one is running."""
        if self._process is None:
            return None
        return self._process.process.pid

    async def open(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        settings: Any,
    ) -> BrowserSession:
        """Open the browser session of one conversation.

        A conversation holds one session.  Opening it again returns the
        session it already holds.

        Args:
            tenant_id: The tenant that owns the session.
            user_id: The user that owns the session.
            conversation_id: The conversation that owns the session.
            settings: The deployment settings in force.

        Returns:
            The live session.

        Raises:
            BrowserUnavailableError: When the deployment has no browser
                runtime, or the host has no browser to run.
            BrowserSessionLimitError: When the deployment's session limit is
                reached.
            BrowserProtocolError: When the browser does not answer.
        """
        if not browser_runtime_enabled(settings):
            raise BrowserUnavailableError(
                "This deployment has no browser runtime. Turn it on with the "
                "deepspace_browser_enabled setting."
            )
        async with self._lock:
            await self._sweep_locked()
            existing = self.session_for_conversation(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            idle_timeout = _setting_float(
                settings,
                "deepspace_browser_idle_timeout_seconds",
                self.idle_timeout_seconds,
            )
            if existing is not None:
                existing.touch(idle_timeout)
                return existing
            if len(self._sessions) >= self.max_sessions:
                raise BrowserSessionLimitError(
                    "This deployment already holds its maximum number of live browser sessions."
                )
            browser = await self._ensure_process(settings)
            context = await browser.connection.send(
                "Target.createBrowserContext",
                {"disposeOnDetach": False},
            )
            browser_context_id = str(context.get("browserContextId") or "")
            target = await browser.connection.send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": browser_context_id},
            )
            target_id = str(target.get("targetId") or "")
            attached = await browser.connection.send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            cdp_session_id = str(attached.get("sessionId") or "")
            await browser.connection.send("Page.enable", session_id=cdp_session_id)
            await browser.connection.send("Runtime.enable", session_id=cdp_session_id)
            await browser.connection.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": VIEWPORT_WIDTH,
                    "height": VIEWPORT_HEIGHT,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
                session_id=cdp_session_id,
            )
            now = datetime.now(UTC)
            session = BrowserSession(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                browser_context_id=browser_context_id,
                target_id=target_id,
                cdp_session_id=cdp_session_id,
                created_at=now,
                last_used_at=now,
                idle_deadline=time.monotonic() + idle_timeout,
            )
            self._sessions[session.id] = session
            self._start_sweeper()
            return session

    def get(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserSession:
        """Return one session the caller owns.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The live session.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise BrowserSessionNotFoundError("No such browser session.")
        return session

    def session_for_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserSession | None:
        """Return the session one conversation holds.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The session, or None when the conversation holds none.
        """
        for session in self._sessions.values():
            if session.owned_by(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            ):
                return session
        return None

    async def require_session(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        session_id: str | None = None,
    ) -> BrowserSession:
        """Return the session a request addresses.

        A request that names a session is answered only when that session
        belongs to the caller.  A request that names none is answered with
        the session the conversation holds.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            session_id: The session the request names, when it names one.

        Returns:
            The live session.

        Raises:
            BrowserSessionNotFoundError: When the request names no session
                the caller owns.
        """
        if session_id:
            return self.get(
                session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        session = self.session_for_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if session is None:
            raise BrowserSessionNotFoundError("No such browser session.")
        return session

    async def close(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        """Close one session and forget it.

        Closing a session that is already gone is not an error.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            True when a session was closed.
        """
        session = self._sessions.get(str(session_id))
        if session is None or not session.owned_by(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return False
        await self._close_session(session)
        return True

    async def close_all_for_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> int:
        """Close every live session of one conversation.

        Args:
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The number of sessions that were closed.
        """
        sessions = [
            session
            for session in self._sessions.values()
            if session.owned_by(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        ]
        for session in sessions:
            await self._close_session(session)
        return len(sessions)

    async def sweep(self) -> Sequence[str]:
        """Close the sessions that exceeded their idle lifetime.

        Returns:
            The identifiers of the sessions that were closed.
        """
        async with self._lock:
            return await self._sweep_locked()

    async def shutdown(self) -> None:
        """Close every session and stop the browser process."""
        async with self._lock:
            for session in list(self._sessions.values()):
                await self._close_session(session)
            self._stop_sweeper()
            if self._process is not None:
                await self._process.close()
                self._process = None

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    async def current(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Return what the session's page looks like now.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return await self._capture(session)

    async def navigate(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        url: str,
    ) -> BrowserPage:
        """Open an address in the session's page.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.
            url: The address to open.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the address may not be opened.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        target = validate_browser_url(url)
        await self._send(session, "Page.navigate", {"url": target})
        await self._wait_until_ready(session)
        return await self._capture(session)

    async def back(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Move to the earlier page in the browser's own history.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the browser's history holds no
                earlier page.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        history = await self._history(session)
        entries = history["entries"]
        current = history["currentIndex"]
        earlier = self._earlier_entry(entries, current)
        if earlier is None:
            raise BrowserNavigationError("There is no earlier page in this browser's history.")
        await self._send(session, "Page.navigateToHistoryEntry", {"entryId": earlier["id"]})
        await self._wait_until_ready(session)
        return await self._capture(session)

    async def forward(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Move to the later page in the browser's own history.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the browser's history holds no later
                page.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        history = await self._history(session)
        entries = history["entries"]
        current = history["currentIndex"]
        later = self._later_entry(entries, current)
        if later is None:
            raise BrowserNavigationError("There is no later page in this browser's history.")
        await self._send(session, "Page.navigateToHistoryEntry", {"entryId": later["id"]})
        await self._wait_until_ready(session)
        return await self._capture(session)

    async def reload(
        self,
        session_id: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Load the session's page again.

        Args:
            session_id: The session's identifier.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        await self._send(session, "Page.reload", {})
        await self._wait_until_ready(session)
        return await self._capture(session)

    async def click(
        self,
        session_id: str,
        *,
        x: float,
        y: float,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Click one point of the session's page.

        Args:
            session_id: The session's identifier.
            x: The horizontal position in the page's viewport, in pixels.
            y: The vertical position in the page's viewport, in pixels.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the point is outside the viewport.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        point_x = float(x)
        point_y = float(y)
        if not (0 <= point_x < VIEWPORT_WIDTH and 0 <= point_y < VIEWPORT_HEIGHT):
            raise BrowserNavigationError(
                f"The click position is outside the {VIEWPORT_WIDTH} by {VIEWPORT_HEIGHT} page."
            )
        await self._send(
            session,
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": point_x, "y": point_y},
        )
        await self._send(
            session,
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": point_x,
                "y": point_y,
                "button": "left",
                "clickCount": 1,
            },
        )
        await self._send(
            session,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": point_x,
                "y": point_y,
                "button": "left",
                "clickCount": 1,
            },
        )
        await asyncio.sleep(INPUT_SETTLE_SECONDS)
        return await self._capture(session)

    async def type_text(
        self,
        session_id: str,
        *,
        text: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Insert text into the focused field of the session's page.

        Args:
            session_id: The session's identifier.
            text: The text to insert.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the text is empty or too long.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not text:
            raise BrowserNavigationError("Enter text to type into the page.")
        if len(text) > MAX_TYPE_TEXT_LENGTH:
            raise BrowserNavigationError(f"Type at most {MAX_TYPE_TEXT_LENGTH} characters at once.")
        await self._send(session, "Input.insertText", {"text": text})
        await asyncio.sleep(INPUT_SETTLE_SECONDS)
        return await self._capture(session)

    async def press_key(
        self,
        session_id: str,
        *,
        key: str,
        modifiers: Sequence[str] = (),
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Press one key in the session's page.

        Args:
            session_id: The session's identifier.
            key: The key to press.
            modifiers: The modifier keys held while the key is pressed.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
            BrowserNavigationError: When the key or a modifier is not
                supported.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        definition = PRESSABLE_KEYS.get(key)
        if definition is None:
            raise BrowserNavigationError(f"The {key!r} key is not supported.")
        modifier_bits = 0
        for modifier in modifiers:
            bit = KEY_MODIFIERS.get(modifier)
            if bit is None:
                raise BrowserNavigationError(f"The {modifier!r} modifier is not supported.")
            modifier_bits |= bit
        key_value, code, virtual_key_code = definition
        key_down: dict[str, Any] = {
            "type": "keyDown",
            "key": key_value,
            "code": code,
            "windowsVirtualKeyCode": virtual_key_code,
            "modifiers": modifier_bits,
        }
        if key == "Enter":
            key_down["text"] = "\r"
        elif key == "Space":
            key_down["text"] = " "
        await self._send(session, "Input.dispatchKeyEvent", key_down)
        await self._send(
            session,
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key_value,
                "code": code,
                "windowsVirtualKeyCode": virtual_key_code,
                "modifiers": modifier_bits,
            },
        )
        await asyncio.sleep(INPUT_SETTLE_SECONDS)
        return await self._capture(session)

    async def scroll(
        self,
        session_id: str,
        *,
        delta_x: float,
        delta_y: float,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> BrowserPage:
        """Scroll the session's page from its centre.

        Args:
            session_id: The session's identifier.
            delta_x: The horizontal distance to scroll, in pixels.
            delta_y: The vertical distance to scroll, in pixels.
            tenant_id: The calling tenant.
            user_id: The calling user.
            conversation_id: The calling conversation.

        Returns:
            The page's address, title, and screenshot.

        Raises:
            BrowserSessionNotFoundError: When no session with that id belongs
                to the caller.
        """
        session = self.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        scroll_x = max(-MAX_SCROLL_DELTA, min(MAX_SCROLL_DELTA, float(delta_x)))
        scroll_y = max(-MAX_SCROLL_DELTA, min(MAX_SCROLL_DELTA, float(delta_y)))
        await self._send(
            session,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": VIEWPORT_WIDTH // 2,
                "y": VIEWPORT_HEIGHT // 2,
                "deltaX": scroll_x,
                "deltaY": scroll_y,
            },
        )
        await asyncio.sleep(INPUT_SETTLE_SECONDS)
        return await self._capture(session)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_process(self, settings: Any) -> BrowserProcess:
        """Return the browser process, starting one when it is needed.

        Args:
            settings: The deployment settings in force.

        Returns:
            The running browser.

        Raises:
            BrowserUnavailableError: When the host has no browser to run.
        """
        if self._process is not None:
            if self._process.process.returncode is None:
                return self._process
            # The browser is gone, and the pages it held went with it.
            for session in list(self._sessions.values()):
                self._sessions.pop(session.id, None)
            await self._process.close()
            self._process = None
        configured = _setting_str(settings, "deepspace_browser_executable", "")
        if configured:
            executable = find_browser_executable(configured)
            if executable is None:
                raise BrowserUnavailableError(
                    f"The browser at {configured} cannot be run on this host."
                )
        else:
            executable = self.executable or find_browser_executable()
            if executable is None:
                raise BrowserUnavailableError(
                    "No Chromium browser was found on this host. Name one with the "
                    "deepspace_browser_executable setting."
                )
        self.executable = executable
        self._process = await BrowserProcess.start(
            executable=executable,
            launch_timeout_seconds=self.launch_timeout_seconds,
            mode=_sandbox_mode(settings),
        )
        return self._process

    async def _send(
        self,
        session: BrowserSession,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one command to the session's page.

        Args:
            session: The session the command belongs to.
            method: The DevTools method to call.
            params: The command's parameters.

        Returns:
            The answer's result.

        Raises:
            BrowserProtocolError: When the browser does not answer.
        """
        if self._process is None:
            raise BrowserProtocolError("The browser is not running.")
        return await self._process.connection.send(
            method,
            params,
            session_id=session.cdp_session_id,
            timeout_seconds=self.load_timeout_seconds,
        )

    async def _history(self, session: BrowserSession) -> dict[str, Any]:
        """Read the browser's own history for the session's page.

        Args:
            session: The session the history belongs to.

        Returns:
            The history's entries and the index of the current entry.
        """
        result = await self._send(session, "Page.getNavigationHistory")
        entries = result.get("entries")
        if not isinstance(entries, list) or not entries:
            return {"entries": [], "currentIndex": 0}
        current = result.get("currentIndex")
        index = int(current) if isinstance(current, int) else 0
        index = max(0, min(index, len(entries) - 1))
        return {"entries": entries, "currentIndex": index}

    async def _wait_until_ready(self, session: BrowserSession) -> None:
        """Wait until the session's page has finished loading.

        A page that never finishes loading does not hold the caller past the
        load timeout.

        Args:
            session: The session whose page is loading.
        """
        deadline = time.monotonic() + self.load_timeout_seconds
        while time.monotonic() < deadline:
            try:
                result = await self._send(
                    session,
                    "Runtime.evaluate",
                    {"expression": "document.readyState", "returnByValue": True},
                )
            except BrowserProtocolError:
                # The page is replacing its execution context; try again.
                await asyncio.sleep(0.1)
                continue
            value = result.get("result")
            if isinstance(value, dict) and value.get("value") == "complete":
                return
            await asyncio.sleep(0.1)

    async def _capture(self, session: BrowserSession) -> BrowserPage:
        """Read what the session's page looks like now.

        Args:
            session: The session whose page is read.

        Returns:
            The page's address, title, and screenshot.
        """
        history = await self._history(session)
        entries = history["entries"]
        current = history["currentIndex"]
        url = str(entries[current].get("url") or "") if entries else ""
        title = await self._title(session)
        screenshot = await self._screenshot(session)
        return BrowserPage(
            url=url,
            title=title,
            screenshot=screenshot,
            can_go_back=self._earlier_entry(entries, current) is not None,
            can_go_forward=self._later_entry(entries, current) is not None,
        )

    @staticmethod
    def _earlier_entry(entries: list[dict[str, Any]], current: int) -> dict[str, Any] | None:
        """Return the entry before the current one that is a real page.

        The page a session starts on is a blank page, and it is not a page the
        user can go back to.

        Args:
            entries: The browser's history entries.
            current: The index of the current entry.

        Returns:
            The entry, or None when the history holds no earlier page.
        """
        for index in range(current - 1, -1, -1):
            if str(entries[index].get("url") or "") != BLANK_URL:
                return entries[index]
        return None

    @staticmethod
    def _later_entry(entries: list[dict[str, Any]], current: int) -> dict[str, Any] | None:
        """Return the entry after the current one.

        Args:
            entries: The browser's history entries.
            current: The index of the current entry.

        Returns:
            The entry, or None when the history holds no later page.
        """
        for index in range(current + 1, len(entries)):
            return entries[index]
        return None

    async def _title(self, session: BrowserSession) -> str:
        """Read the title of the session's page.

        Args:
            session: The session whose page is read.

        Returns:
            The page's title, or an empty string when it cannot be read.
        """
        try:
            result = await self._send(
                session,
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
            )
        except BrowserProtocolError:
            return ""
        value = result.get("result")
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return str(value["value"])
        return ""

    async def _screenshot(self, session: BrowserSession) -> bytes:
        """Take a screenshot of the session's page.

        Args:
            session: The session whose page is captured.

        Returns:
            The PNG bytes of the screenshot.

        Raises:
            BrowserProtocolError: When the browser returned no image.
        """
        result = await self._send(session, "Page.captureScreenshot", {"format": "png"})
        data = result.get("data")
        if not isinstance(data, str) or not data:
            raise BrowserProtocolError("The browser returned no screenshot.")
        try:
            return base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            raise BrowserProtocolError("The browser returned an unreadable screenshot.") from exc

    async def _close_session(self, session: BrowserSession) -> None:
        """Close one session's page and forget the session.

        Args:
            session: The session to close.
        """
        self._sessions.pop(session.id, None)
        browser = self._process
        if browser is None:
            return
        with contextlib.suppress(BrowserProtocolError):
            await browser.connection.send("Target.closeTarget", {"targetId": session.target_id})
        with contextlib.suppress(BrowserProtocolError):
            await browser.connection.send(
                "Target.disposeBrowserContext",
                {"browserContextId": session.browser_context_id},
            )

    async def _sweep_locked(self) -> Sequence[str]:
        """Close the sessions that exceeded their idle lifetime.

        Returns:
            The identifiers of the sessions that were closed.
        """
        now = time.monotonic()
        expired = [session for session in self._sessions.values() if session.idle_deadline <= now]
        for session in expired:
            await self._close_session(session)
        return [session.id for session in expired]

    def _start_sweeper(self) -> None:
        """Start the task that closes the sessions that went idle."""
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    def _stop_sweeper(self) -> None:
        """Stop the task that closes the sessions that went idle."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        """Close the sessions that went idle, until the manager stops."""
        try:
            while True:
                await asyncio.sleep(self.sweep_interval_seconds)
                await self.sweep()
        except asyncio.CancelledError:
            return


def _sandbox_mode(settings: Any) -> str:
    """Return the containment mode the deployment runs in.

    Args:
        settings: The deployment settings in force.

    Returns:
        ``read-only``, ``workspace-write``, or ``danger-full-access``.
    """
    return str(
        getattr(settings, "deepspace_sandbox_policy", None)
        or getattr(settings, "deepspace_permission_mode", "workspace-write")
    )


#: The browser managers of the event loops this process runs.
#:
#: A manager owns a browser process and a DevTools connection, and both belong
#: to the event loop that started them.  One manager per event loop keeps a
#: manager from being used from a loop it does not belong to.
_MANAGERS: WeakKeyDictionary[asyncio.AbstractEventLoop, BrowserManager] = WeakKeyDictionary()


def browser_manager() -> BrowserManager:
    """Return the browser manager of the running event loop.

    Returns:
        The manager that owns this event loop's browser sessions.
    """
    loop = asyncio.get_running_loop()
    manager = _MANAGERS.get(loop)
    if manager is None:
        manager = BrowserManager()
        _MANAGERS[loop] = manager
    return manager


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_LAUNCH_TIMEOUT_SECONDS",
    "DEFAULT_LOAD_TIMEOUT_SECONDS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_SWEEP_INTERVAL_SECONDS",
    "MAX_MESSAGE_BYTES",
    "MAX_SCROLL_DELTA",
    "MAX_TYPE_TEXT_LENGTH",
    "VIEWPORT_HEIGHT",
    "VIEWPORT_WIDTH",
    "BrowserManager",
    "BrowserNavigationError",
    "BrowserPage",
    "BrowserProcess",
    "BrowserProtocolError",
    "BrowserRuntimeError",
    "BrowserSession",
    "BrowserSessionLimitError",
    "BrowserSessionNotFoundError",
    "BrowserUnavailableError",
    "DevToolsConnection",
    "browser_manager",
    "browser_runtime_enabled",
    "build_browser_environment",
    "find_browser_executable",
    "validate_browser_url",
]
