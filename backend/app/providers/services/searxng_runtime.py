"""Manage the optional native SearXNG runtime used by local AverQel.

The local desktop deployment has no Docker network or hosted search service.
This module installs SearXNG into the user's AverQel home on first use and
runs one loopback-only Granian process for the lifetime of the backend. A
deployment that supplies a non-loopback ``searxng_base_url`` keeps ownership
of that external instance and this manager does not start anything.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Literal
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from app.core.averqel_home import get_averqel_home
from app.core.config import Settings

DEFAULT_SEARXNG_SOURCE_URL = "https://github.com/searxng/searxng.git"
DEFAULT_SEARXNG_SOURCE_REF = "master"
DEFAULT_SEARXNG_PORT = 8888
DEFAULT_INSTALL_TIMEOUT_SECONDS = 900
DEFAULT_START_TIMEOUT_SECONDS = 90
PROBE_TIMEOUT_SECONDS = 3


class SearXNGRuntimeError(RuntimeError):
    """Raised when the managed SearXNG runtime cannot be prepared or started."""


RuntimeState = Literal["disabled", "external", "not_installed", "running", "failed"]


def _is_loopback_host(hostname: str | None) -> bool:
    """Return whether a URL hostname is a loopback address."""
    normalized = (hostname or "").strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _python_executable(virtualenv: Path) -> Path:
    """Return the Python executable inside a virtual environment."""
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return virtualenv / relative


def _binary_path(virtualenv: Path, name: str) -> Path:
    """Return a virtual-environment command path for the current platform."""
    suffix = ".exe" if os.name == "nt" else ""
    relative = Path("Scripts") if os.name == "nt" else Path("bin")
    return virtualenv / relative / f"{name}{suffix}"


class SearXNGRuntime:
    """Own one local SearXNG process and its per-user installation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: BinaryIO | None = None
        self._lock = threading.RLock()
        self._last_error: str | None = None

    @property
    def base_url(self) -> str:
        """Return the configured SearXNG endpoint without a trailing slash."""
        return str(self.settings.searxng_base_url or "").strip().rstrip("/")

    @property
    def runtime_dir(self) -> Path:
        """Return the private directory that holds SearXNG runtime files."""
        configured = str(getattr(self.settings, "searxng_runtime_dir", "") or "").strip()
        return (
            Path(configured).expanduser().resolve()
            if configured
            else get_averqel_home() / "searxng"
        )

    @property
    def source_dir(self) -> Path:
        """Return the checked-out SearXNG source directory."""
        return self.runtime_dir / "source"

    @property
    def virtualenv_dir(self) -> Path:
        """Return the private SearXNG Python virtual environment directory."""
        return self.runtime_dir / "venv"

    @property
    def settings_path(self) -> Path:
        """Return the SearXNG settings file controlled by AverQel."""
        return self.runtime_dir / "settings.yml"

    @property
    def log_path(self) -> Path:
        """Return the SearXNG process log path."""
        return self.runtime_dir / "searxng.log"

    @property
    def pid_path(self) -> Path:
        """Return the private marker for the process started by this manager."""
        return self.runtime_dir / "searxng.pid"

    def _endpoint_is_local(self) -> bool:
        parsed = urlparse(self.base_url)
        return (
            parsed.scheme in {"http", "https"}
            and _is_loopback_host(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not parsed.path
        )

    def _port(self) -> int:
        parsed = urlparse(self.base_url)
        return int(parsed.port or DEFAULT_SEARXNG_PORT)

    def _probe(self) -> bool:
        """Check the local HTTP endpoint without issuing a search query."""
        if not self.base_url:
            return False
        request = urllib_request.Request(
            f"{self.base_url}/",
            headers={"Accept": "text/html", "User-Agent": "AverQel-DeepSpace/1.0"},
        )
        try:
            with urllib_request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
                return 200 <= int(response.status) < 300
        except (OSError, urllib_error.URLError):
            return False

    def _installed(self) -> bool:
        python = _python_executable(self.virtualenv_dir)
        if not python.is_file() or not (self.source_dir / "setup.py").is_file():
            return False
        try:
            result = subprocess.run(
                [str(python), "-c", "import searx"],
                cwd=self.source_dir,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _run_install_command(self, command: list[str], *, cwd: Path) -> None:
        timeout = int(
            getattr(
                self.settings,
                "searxng_install_timeout_seconds",
                DEFAULT_INSTALL_TIMEOUT_SECONDS,
            )
        )
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SearXNGRuntimeError(
                "SearXNG installation timed out. Check the local network and try again."
            ) from exc
        except OSError as exc:
            raise SearXNGRuntimeError(
                f"Could not run the SearXNG installer command {command[0]!r}."
            ) from exc
        if result.returncode == 0:
            return
        output = "\n".join(
            part.strip() for part in (result.stdout or "", result.stderr or "") if part.strip()
        )
        detail = output[-4000:] if output else "no installer output"
        raise SearXNGRuntimeError(f"SearXNG installation failed: {detail}")

    def _clone_source(self) -> None:
        if self.source_dir.exists():
            if (self.source_dir / "setup.py").is_file():
                return
            raise SearXNGRuntimeError(
                f"SearXNG source directory is not a valid checkout: {self.source_dir}"
            )
        git = shutil.which("git")
        if git is None:
            raise SearXNGRuntimeError(
                "SearXNG is not installed and Git is unavailable. Install Git or set "
                "AKS_SEARXNG_LOCAL_RUNTIME_ENABLED=false to use another provider."
            )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        source_url = str(
            getattr(self.settings, "searxng_source_url", DEFAULT_SEARXNG_SOURCE_URL)
            or DEFAULT_SEARXNG_SOURCE_URL
        ).strip()
        source_ref = str(
            getattr(self.settings, "searxng_source_ref", DEFAULT_SEARXNG_SOURCE_REF)
            or DEFAULT_SEARXNG_SOURCE_REF
        ).strip()
        self._run_install_command(
            [
                git,
                "clone",
                "--depth",
                "1",
                "--branch",
                source_ref,
                source_url,
                str(self.source_dir),
            ],
            cwd=self.runtime_dir,
        )

    def _install(self) -> None:
        self._clone_source()
        python = _python_executable(self.virtualenv_dir)
        if not python.is_file():
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._run_install_command(
                [sys.executable, "-m", "venv", str(self.virtualenv_dir)],
                cwd=self.runtime_dir,
            )
        if not self._installed():
            # Python 3.12 virtual environments may contain pip without the
            # setuptools backend required by SearXNG's setup.py package.
            self._run_install_command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "setuptools",
                    "wheel",
                ],
                cwd=self.source_dir,
            )
            requirements = self.source_dir / "requirements.txt"
            if requirements.is_file():
                self._run_install_command(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "-r",
                        str(requirements),
                    ],
                    cwd=self.source_dir,
                )
            self._run_install_command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-build-isolation",
                    "--no-deps",
                    ".",
                ],
                cwd=self.source_dir,
            )
        server_requirements = self.source_dir / "requirements-server.txt"
        if (
            not _binary_path(self.virtualenv_dir, "granian").is_file()
            and server_requirements.is_file()
        ):
            self._run_install_command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "-r",
                    str(server_requirements),
                ],
                cwd=self.source_dir,
            )
        if not _binary_path(self.virtualenv_dir, "granian").is_file():
            raise SearXNGRuntimeError(
                "SearXNG installed without its HTTP server. The Granian dependency is missing."
            )

    def _write_settings(self) -> None:
        if self.settings_path.exists():
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        secret_key = secrets.token_hex(32)
        content = (
            "# Managed by AverQel Kinesis. Edit only if you understand SearXNG settings.\n"
            "use_default_settings: true\n\n"
            "search:\n"
            "  formats:\n"
            "    - html\n"
            "    - json\n\n"
            "server:\n"
            '  bind_address: "127.0.0.1"\n'
            f"  port: {self._port()}\n"
            "  limiter: false\n"
            "  public_instance: false\n"
            "  image_proxy: false\n"
            f'  secret_key: "{secret_key}"\n'
        )
        self.settings_path.write_text(content, encoding="utf-8")
        try:
            self.settings_path.chmod(0o600)
        except OSError:
            # Windows does not expose POSIX file modes; the file remains in the
            # private AverQel home directory and is not returned by the API.
            pass

    def _start(self) -> None:
        python = _python_executable(self.virtualenv_dir)
        granian = _binary_path(self.virtualenv_dir, "granian")
        if not python.is_file() or not granian.is_file():
            raise SearXNGRuntimeError("The managed SearXNG runtime is not installed.")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        log_stream = self.log_path.open("ab", buffering=0)
        environment = os.environ.copy()
        environment.update(
            {
                "SEARXNG_SETTINGS_PATH": str(self.settings_path),
                "SEARXNG_DEBUG": "false",
                "GRANIAN_INTERFACE": "wsgi",
                "GRANIAN_HOST": "127.0.0.1",
                "GRANIAN_PORT": str(self._port()),
                "GRANIAN_WORKERS": "1",
                "GRANIAN_WEBSOCKETS": "false",
                "PYTHONUNBUFFERED": "1",
            }
        )
        try:
            process = subprocess.Popen(
                [str(granian), "searx.webapp:app"],
                cwd=self.source_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            log_stream.close()
            raise SearXNGRuntimeError("Could not start the managed SearXNG process.") from exc
        self._process = process
        self._log_stream = log_stream
        self.pid_path.write_text(str(process.pid), encoding="ascii")

    def _wait_until_ready(self) -> None:
        timeout = int(
            getattr(self.settings, "searxng_start_timeout_seconds", DEFAULT_START_TIMEOUT_SECONDS)
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise SearXNGRuntimeError(
                    f"SearXNG exited during startup; inspect {self.log_path}."
                )
            if self._probe():
                return
            time.sleep(0.25)
        raise SearXNGRuntimeError(
            f"SearXNG did not become ready within {timeout} seconds; inspect {self.log_path}."
        )

    def ensure_running(self) -> None:
        """Install and start local SearXNG when the local runtime is enabled."""
        with self._lock:
            self._last_error = None
            if not bool(getattr(self.settings, "searxng_local_runtime_enabled", False)):
                return
            if not self._endpoint_is_local():
                return
            if self._probe():
                return
            try:
                self._install()
                self._write_settings()
                self._start()
                self._wait_until_ready()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self.shutdown()
                raise

    def shutdown(self) -> None:
        """Stop only the SearXNG process started by this manager."""
        with self._lock:
            process = self._process
            self._process = None
            if process is not None and process.poll() is None:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
            if self._log_stream is not None:
                self._log_stream.close()
                self._log_stream = None
            try:
                self.pid_path.unlink(missing_ok=True)
            except OSError:
                pass

    def status(self) -> dict[str, object]:
        """Return bounded diagnostic state safe for the local UI/API."""
        enabled = bool(getattr(self.settings, "searxng_local_runtime_enabled", False))
        if not enabled:
            state: RuntimeState = "disabled"
        elif not self._endpoint_is_local():
            state = "external"
        elif self._probe():
            state = "running"
        elif self._last_error:
            state = "failed"
        elif not (self.source_dir / "setup.py").is_file():
            state = "not_installed"
        else:
            state = "not_installed"
        return {
            "provider": "searxng",
            "state": state,
            "enabled": enabled,
            "base_url": self.base_url,
            "local_only": self._endpoint_is_local(),
            "pid": self._process.pid if self._process is not None else None,
            "runtime_dir": str(self.runtime_dir),
            "log_path": str(self.log_path),
            "error": self._last_error,
        }


__all__ = ["SearXNGRuntime", "SearXNGRuntimeError"]
