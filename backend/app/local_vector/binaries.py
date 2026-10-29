"""
Local binary management for LibreOffice and Tesseract.

Downloads and manages self-contained binaries for offline operation.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.averqel_home import averqel_cache_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIBREOFFICE_VERSION = "24.8.3"
LIBREOFFICE_URLS = {
    "linux": f"https://download.documentfoundation.org/libreoffice/stable/{LIBREOFFICE_VERSION}/rpm/x86_64/LibreOffice_{LIBREOFFICE_VERSION}_Linux_x86-64_rpm.tar.gz",
    "darwin": f"https://download.documentfoundation.org/libreoffice/stable/{LIBREOFFICE_VERSION}/mac/x86_64/LibreOffice_{LIBREOFFICE_VERSION}_MacOS_x86-64.dmg",
    "win32": f"https://download.documentfoundation.org/libreoffice/stable/{LIBREOFFICE_VERSION}/win/x86_64/LibreOffice_{LIBREOFFICE_VERSION}_Win_x86-64.msi",
}

TESSERACT_VERSION = "5.3.4"
TESSERACT_URLS = {
    "linux": f"https://github.com/UB-Mannheim/tesseract/releases/download/v{TESSERACT_VERSION}/tesseract-ocr-w64-setup-{TESSERACT_VERSION}.exe",
    "darwin": "brew install tesseract",  # Use Homebrew on macOS
    "win32": f"https://github.com/UB-Mannheim/tesseract/releases/download/v{TESSERACT_VERSION}/tesseract-ocr-w64-setup-{TESSERACT_VERSION}.exe",
}

TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main"

# ---------------------------------------------------------------------------
# Binary Manager
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BinaryInfo:
    name: str
    path: Path
    version: str
    checksum: str | None = None


class LocalBinaryManager:
    """Manages local binaries for offline operation."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (averqel_cache_path() / "binaries")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._libreoffice_path: Path | None = None
        self._tesseract_path: Path | None = None
        self._tessdata_dir: Path | None = None

    def ensure_libreoffice(self) -> Path:
        """Ensure LibreOffice is available, download if needed."""
        if self._libreoffice_path and self._libreoffice_path.exists():
            return self._libreoffice_path

        system = platform.system().lower()
        if system == "darwin":
            system = "darwin"
        elif system == "windows":
            system = "win32"
        else:
            system = "linux"

        # Check system PATH first
        for cmd in ("soffice", "libreoffice"):
            path = shutil.which(cmd)
            if path:
                self._libreoffice_path = Path(path)
                logger.info(f"Found system LibreOffice: {self._libreoffice_path}")
                return self._libreoffice_path

        # Download and extract portable version
        logger.info("Downloading LibreOffice portable...")
        self._libreoffice_path = self._download_libreoffice(system)
        return self._libreoffice_path

    def _download_libreoffice(self, system: str) -> Path:
        """Download and extract LibreOffice portable."""
        # For simplicity, use AppImage on Linux
        if system == "linux":
            return self._download_libreoffice_appimage()

        raise RuntimeError(f"LibreOffice auto-download not implemented for {system}")

    def _download_libreoffice_appimage(self) -> Path:
        """Download LibreOffice AppImage for Linux."""
        # Use a known LibreOffice AppImage mirror
        appimage_url = (
            "https://github.com/LibreOffice/libreoffice/releases/download/"
            f"{LIBREOFFICE_VERSION}/LibreOffice-{LIBREOFFICE_VERSION}-x86_64.AppImage"
        )

        appimage_path = self.cache_dir / f"LibreOffice-{LIBREOFFICE_VERSION}-x86_64.AppImage"

        if not appimage_path.exists():
            logger.info(f"Downloading LibreOffice AppImage from {appimage_url}")
            self._download_file(appimage_url, appimage_url)
            appimage_path.chmod(0o755)

        # Test it works
        try:
            result = subprocess.run(
                [str(appimage_path), "--version"],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(f"LibreOffice AppImage ready: {result.stdout.decode().strip()}")
                return appimage_path
        except Exception as exc:
            logger.warning(f"LibreOffice AppImage test failed: {exc}")

        raise RuntimeError("Could not obtain working LibreOffice binary")

    def ensure_tesseract(self) -> tuple[Path, Path]:
        """Ensure Tesseract and tessdata are available.

        Returns:
            tuple of (tesseract_binary_path, tessdata_dir_path)
        """
        if self._tesseract_path and self._tessdata_dir:
            if self._tesseract_path.exists() and self._tessdata_dir.exists():
                return self._tesseract_path, self._tessdata_dir

        system = platform.system().lower()
        if system == "darwin":
            system = "darwin"
        elif system == "windows":
            system = "win32"
        else:
            system = "linux"

        # Check system PATH first
        path = shutil.which("tesseract")
        if path:
            self._tesseract_path = Path(path)
            # Find tessdata
            tessdata = self._find_tessdata(Path(path))
            if tessdata:
                self._tessdata_dir = tessdata
                logger.info(
                    f"Found system Tesseract: {self._tesseract_path}, tessdata: {self._tessdata_dir}"
                )
                return self._tesseract_path, self._tessdata_dir

        # Download portable version
        self._tesseract_path, self._tessdata_dir = self._download_tesseract(system)
        return self._tesseract_path, self._tessdata_dir

    def _find_tessdata(self, tesseract_bin: Path) -> Path | None:
        """Find tessdata directory relative to tesseract binary."""
        # Common locations
        for base in [tesseract_bin.parent.parent, Path("/usr"), Path("/usr/local"), Path("/opt")]:
            for tessdata in base.glob("**/tessdata"):
                if tessdata.is_dir() and any(tessdata.glob("*.traineddata")):
                    return tessdata
        return None

    def _download_tesseract(self, system: str) -> tuple[Path, Path]:
        """Download Tesseract binary and tessdata."""
        if system == "linux":
            return self._download_tesseract_linux()
        raise RuntimeError(f"Tesseract auto-download not implemented for {system}")

    def _download_tesseract_linux(self) -> tuple[Path, Path]:
        """Download Tesseract AppImage or static binary for Linux."""
        # Use a static Tesseract build
        # For now, we'll download tessdata and assume tesseract is installed via package manager
        # In production, you'd bundle a static binary

        tessdata_dir = self.cache_dir / "tessdata"
        tessdata_dir.mkdir(exist_ok=True)

        # Download essential language models
        langs = ["eng", "osd"]  # English + orientation script detection
        for lang in langs:
            traineddata = tessdata_dir / f"{lang}.traineddata"
            if not traineddata.exists():
                url = f"{TESSDATA_URL}/{lang}.traineddata"
                logger.info(f"Downloading tessdata: {lang}")
                self._download_file(url, traineddata)

        # Return dummy binary path - in production this would be a real binary
        # For now, we rely on system tesseract
        tesseract_bin = shutil.which("tesseract") or Path("/usr/bin/tesseract")
        if not tesseract_bin.exists():
            raise RuntimeError(
                "Tesseract not found. Install via package manager: "
                "apt-get install tesseract-ocr (Debian/Ubuntu) or "
                "brew install tesseract (macOS)"
            )

        return tesseract_bin, tessdata_dir

    def _download_file(self, url: str, dest: Path) -> None:
        """Download a file with progress."""
        logger.info(f"Downloading {url} -> {dest}")
        with urllib.request.urlopen(url) as response, open(dest, "wb") as out_file:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 8192
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = downloaded * 100 // total
                    if percent % 10 == 0:
                        logger.info(f"Download progress: {percent}%")

    def get_conversion_command(self, input_path: Path, output_dir: Path) -> list[str]:
        """Get the LibreOffice conversion command for the current platform."""
        soffice = self.ensure_libreoffice()
        return [
            str(soffice),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(input_path),
        ]

    def get_tesseract_command(
        self, input_path: Path, output_base: Path, lang: str = "eng"
    ) -> list[str]:
        """Get the Tesseract OCR command."""
        tesseract_bin, tessdata_dir = self.ensure_tesseract()
        return [
            str(tesseract_bin),
            str(input_path),
            str(output_base),
            "-l",
            lang,
            "--tessdata-dir",
            str(tessdata_dir),
            "pdf",
        ]


# ---------------------------------------------------------------------------
# Conversion Service (Local)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConvertedArtifact:
    filename: str
    content_type: str
    payload: bytes
    warnings: list[str]


class LocalConversionService:
    """Local document conversion using bundled LibreOffice."""

    # Formats supported by LibreOffice
    _PDF_EXTENSIONS = frozenset(
        {
            ".doc",
            ".docx",
            ".docm",
            ".dot",
            ".dotx",
            ".dotm",
            ".odt",
            ".ott",
            ".odm",
            ".oth",
            ".rtf",
            ".ppt",
            ".pptx",
            ".pptm",
            ".pot",
            ".potx",
            ".potm",
            ".pps",
            ".ppsx",
            ".ppsm",
            ".odp",
            ".otp",
            ".xls",
            ".xlsx",
            ".xlsm",
            ".xlt",
            ".xltx",
            ".xltm",
            ".xlm",
            ".xla",
            ".xlw",
            ".xlsb",
            ".ods",
            ".ots",
            ".odg",
            ".otg",
            ".odc",
            ".otc",
            ".odf",
            ".otf",
            ".odi",
            ".oti",
        }
    )

    _TARGET_MAP: dict[str, tuple[str, str]] = {
        ".doc": (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ".ppt": (
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ".xls": (
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings
        self.binary_manager = LocalBinaryManager()
        self._conversion_timeout = (
            getattr(settings, "legacy_conversion_timeout_seconds", 120) if settings else 120
        )

    def can_render_pdf(self, filename: str) -> bool:
        from pathlib import Path

        return Path(filename).suffix.lower() in self._PDF_EXTENSIONS

    def can_convert_to_pdf(self, filename: str) -> bool:
        return self.can_render_pdf(filename)

    @property
    def renderable_extensions(self) -> frozenset[str]:
        return self._PDF_EXTENSIONS

    def convert_to_pdf(self, *, filename: str, payload: bytes) -> bytes:
        """Convert a supported Office/ODF document to PDF using local LibreOffice."""
        from pathlib import Path

        from app.core.errors import ApiError

        if not self.can_render_pdf(filename):
            raise ApiError(
                code="PREVIEW_UNSUPPORTED",
                message="Visual preview is not available for this file type.",
                status_code=415,
            )

        soffice = self.binary_manager.ensure_libreoffice()
        safe_name = Path(filename).name

        with tempfile.TemporaryDirectory(
            dir=str(averqel_cache_path()), prefix="aks-office-preview-"
        ) as tmp_dir:
            input_path = Path(tmp_dir) / safe_name
            output_dir = Path(tmp_dir) / "out"
            output_dir.mkdir()

            input_path.write_bytes(payload)

            cmd = [
                str(soffice),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(input_path),
            ]

            env = {
                "HOME": tmp_dir,
                "TMPDIR": tmp_dir,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }

            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    timeout=self._conversion_timeout,
                    env=env,
                    cwd=tmp_dir,
                )
            except subprocess.TimeoutExpired as exc:
                raise ApiError(
                    code="PREVIEW_RENDER_TIMEOUT",
                    message="Office preview conversion timed out.",
                    status_code=503,
                ) from exc
            except subprocess.CalledProcessError as exc:
                raise ApiError(
                    code="PREVIEW_RENDER_FAILED",
                    message="Office preview conversion failed.",
                    status_code=422,
                ) from exc

            output_path = output_dir / f"{Path(safe_name).stem}.pdf"
            if not output_path.exists():
                matches = list(output_dir.glob("*.pdf"))
                if matches:
                    output_path = matches[0]

            if not output_path.exists():
                raise ApiError(
                    code="PREVIEW_RENDER_FAILED",
                    message="Office preview conversion produced no PDF.",
                    status_code=422,
                )

            return output_path.read_bytes()

    def convert_legacy(self, *, filename: str, payload: bytes) -> ConvertedArtifact:
        """Convert legacy Office formats to modern equivalents."""
        from pathlib import Path

        from app.core.errors import ApiError

        extension = Path(filename).suffix.lower()
        if extension not in self._TARGET_MAP:
            raise ApiError(
                code="LEGACY_CONVERSION_UNSUPPORTED",
                message="File is not a supported legacy Office format.",
                status_code=400,
            )

        if self.settings and not getattr(self.settings, "legacy_conversion_enabled", True):
            raise ApiError(
                code="LEGACY_CONVERSION_DISABLED",
                message="Legacy Office conversion is disabled.",
                status_code=422,
            )

        target_extension, target_mime = self._TARGET_MAP[extension]
        soffice = self.binary_manager.ensure_libreoffice()
        safe_name = Path(filename).name
        stem = Path(safe_name).stem or "legacy-input"

        with tempfile.TemporaryDirectory(
            dir=str(averqel_cache_path()), prefix="aks-legacy-convert-"
        ) as tmp_dir:
            input_path = Path(tmp_dir) / safe_name
            output_dir = Path(tmp_dir) / "out"
            output_dir.mkdir(parents=True, exist_ok=True)

            input_path.write_bytes(payload)
            cmd = [
                str(soffice),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--norestore",
                "--convert-to",
                target_extension,
                "--outdir",
                str(output_dir),
                str(input_path),
            ]
            env = {
                "HOME": tmp_dir,
                "TMPDIR": tmp_dir,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }

            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    timeout=self._conversion_timeout,
                    env=env,
                    cwd=tmp_dir,
                )
            except subprocess.TimeoutExpired as exc:
                raise ApiError(
                    code="LEGACY_CONVERSION_TIMEOUT",
                    message="Legacy Office conversion timed out.",
                    status_code=503,
                ) from exc
            except subprocess.CalledProcessError as exc:
                raise ApiError(
                    code="LEGACY_CONVERSION_FAILED",
                    message="Legacy Office conversion failed.",
                    status_code=422,
                ) from exc

            output_path = output_dir / f"{stem}.{target_extension}"
            if not output_path.exists():
                converted = list(output_dir.glob(f"*.{target_extension}"))
                if converted:
                    output_path = converted[0]

            if not output_path.exists():
                raise ApiError(
                    code="LEGACY_CONVERSION_FAILED",
                    message="Legacy Office conversion did not produce output.",
                    status_code=422,
                )

            return ConvertedArtifact(
                filename=output_path.name,
                content_type=target_mime,
                payload=output_path.read_bytes(),
                warnings=["legacy_conversion_applied"],
            )
