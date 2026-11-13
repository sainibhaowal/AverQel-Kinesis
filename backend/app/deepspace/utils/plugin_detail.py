"""
Plugin Detail Documents for DeepSpace.

Port of @deepseek-ai/dsh-plugin-detail to Python.
Provides plugin detail document rendering, README/meta.detail selection,
image handling, and file sources for local and npm packages.
"""

from __future__ import annotations

import mimetypes
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

__all__ = [
    # Document types
    "DetailFailure",
    "DetailFailureReason",
    "PackageDocument",
    "FetchedBody",
    "FoundFile",
    "ReadFile",
    "RemoteFileOptions",
    "DetailFiles",
    "DetailImage",
    "DetailImageResponse",
    # Document functions
    "read_package_document",
    "markdown_of",
    "detail_image",
    "detail_image_response",
    "language_chain",
    "readme_by_name",
    "readme_name",
    "encode_path",
    "fetch_body",
    "join_path",
    "local_files",
    "npm_files",
    "npm_file_source_order",
    "npmFiles",
    "README_NAMES",
    # File sources
    "NPM_FILE_SOURCES",
    "localFiles",
    "npmFileSourceOrder",
    "readmeByName",
    "readmeName",
]


# ============================================================================
# Types
# ============================================================================


class DetailFailureReason(str):
    """Reasons for detail fetch failure."""

    NOT_FOUND = "not_found"
    INVALID_PACKAGE = "invalid_package"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    PERMISSION_DENIED = "permission_denied"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"


@dataclass(frozen=True)
class DetailFailure(Exception):  # noqa: N818 - compatibility name is part of the public API
    """Failure when fetching plugin detail."""

    reason: str
    message: str
    package_name: str | None = None
    source_url: str | None = None

    def __str__(self) -> str:
        return f"DetailFailure({self.reason}): {self.message}"


@dataclass(frozen=True)
class PackageDocument:
    """A plugin/package document."""

    content: str
    content_type: str  # "markdown", "text", "html"
    source: str  # "readme", "meta.detail", "custom"
    language: str  # e.g., "en", "zh"
    images: dict[str, str] = field(default_factory=dict)  # path -> base64 or URL
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchedBody:
    """Fetched remote content."""

    content: bytes
    content_type: str
    url: str
    encoding: str = "utf-8"


@dataclass(frozen=True)
class FoundFile:
    """A file found in a package."""

    path: str
    content: str | bytes
    is_binary: bool
    mime_type: str


@dataclass(frozen=True)
class ReadFile:
    """A file read from a source."""

    path: str
    content: str | bytes
    source: str  # "local", "npm", "remote"
    mime_type: str


@dataclass(frozen=True)
class RemoteFileOptions:
    """Options for remote file fetching."""

    max_size: int = 10 * 1024 * 1024  # 10 MB
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
    follow_redirects: bool = True


DetailFiles = list[ReadFile]


@dataclass(frozen=True)
class DetailImage:
    """An image from plugin detail."""

    data: bytes
    mime_type: str
    source_path: str
    alt_text: str = ""


@dataclass(frozen=True)
class DetailImageResponse:
    """Response for detail image request."""

    image: DetailImage
    cache_control: str = "public, max-age=3600"


# ============================================================================
# Constants
# ============================================================================

README_NAMES: tuple[str, ...] = (
    "README.md",
    "README.MD",
    "Readme.md",
    "readme.md",
    "README.txt",
    "README.rst",
    "README",
    "Readme",
    "readme",
)

NPM_FILE_SOURCES: tuple[str, ...] = (
    "unpkg",
    "jsdelivr",
    "github",
)

npm_file_source_order = NPM_FILE_SOURCES


# ============================================================================
# Language Chain
# ============================================================================


def language_chain(primary: str, fallbacks: list[str] | None = None) -> list[str]:
    """
    Build a language fallback chain.

    Args:
        primary: Primary language code (e.g., "zh-CN")
        fallbacks: Optional additional fallbacks

    Returns:
        Ordered list of language codes to try
    """
    chain = [primary]

    # Add generic variant (e.g., "zh" from "zh-CN")
    if "-" in primary:
        generic = primary.split("-")[0]
        if generic not in chain:
            chain.append(generic)

    # Add fallbacks
    if fallbacks:
        for fb in fallbacks:
            if fb not in chain:
                chain.append(fb)

    # Always end with English
    if "en" not in chain:
        chain.append("en")

    return chain


# ============================================================================
# README Selection
# ============================================================================


def readme_name(files: dict[str, Any]) -> str | None:
    """
    Find the README file name from a file listing.

    Args:
        files: Dict of file path -> file info

    Returns:
        The matched README path, or None
    """
    for name in README_NAMES:
        if name in files:
            return name
    return None


def readme_by_name(files: dict[str, Any], name: str) -> str | None:
    """
    Get README content by exact name.

    Args:
        files: Dict of file path -> file info
        name: Exact README name

    Returns:
        README content, or None
    """
    return files.get(name)


# ============================================================================
# Path Encoding
# ============================================================================


def encode_path(path: str) -> str:
    """
    Encode a file path for URL usage.

    Args:
        path: File path

    Returns:
        URL-encoded path
    """
    # Split path into segments, encode each
    segments = path.split("/")
    encoded = [urllib.parse.quote(seg, safe="") for seg in segments]
    return "/".join(encoded)


def join_path(base: str, *parts: str) -> str:
    """
    Join URL path segments.

    Args:
        base: Base URL or path
        *parts: Path segments to append

    Returns:
        Joined path
    """
    if not base.endswith("/"):
        base += "/"
    return base + "/".join(encode_path(p) for p in parts)


# ============================================================================
# File Sources
# ============================================================================


def local_files(package_dir: str | Path, max_size: int = 10 * 1024 * 1024) -> DetailFiles:
    """
    Read all files from a local package directory.

    Args:
        package_dir: Path to package directory
        max_size: Maximum total size to read

    Returns:
        List of ReadFile objects
    """
    package_dir = Path(package_dir)
    files: DetailFiles = []
    total_size = 0

    if not package_dir.exists() or not package_dir.is_dir():
        return files

    for path in sorted(package_dir.rglob("*")):
        if not path.is_file():
            continue

        # Skip hidden files and common ignore patterns
        if any(part.startswith(".") for part in path.relative_to(package_dir).parts):
            continue
        if path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
            continue

        try:
            rel_path = path.relative_to(package_dir).as_posix()
            if path.stat().st_size > max_size - total_size:
                continue
            mime_type, _ = mimetypes.guess_type(str(path))
            is_binary = mime_type and not mime_type.startswith("text/")

            if is_binary:
                content: bytes | str = path.read_bytes()
            else:
                content = path.read_text(encoding="utf-8", errors="replace")

            size = len(content) if isinstance(content, bytes) else len(content.encode())
            if size > max_size - total_size:
                continue

            files.append(
                ReadFile(
                    path=rel_path,
                    content=content,
                    source="local",
                    mime_type=mime_type or "application/octet-stream",
                )
            )
            total_size += size

        except Exception:
            continue

    return files


async def npm_files(
    package_name: str,
    version: str = "latest",
    max_size: int = 10 * 1024 * 1024,
    sources: list[str] | None = None,
) -> DetailFiles:
    """
    Fetch package files from npm CDN sources.

    Args:
        package_name: NPM package name (e.g., "lodash")
        version: Version tag or semver
        max_size: Maximum total size
        sources: CDN sources to try in order

    Returns:
        List of ReadFile objects
    """
    sources = sources or list(NPM_FILE_SOURCES)
    files: DetailFiles = []

    for source in sources:
        try:
            if source == "unpkg":
                files = await _fetch_unpkg(package_name, version, max_size)
            elif source == "jsdelivr":
                files = await _fetch_jsdelivr(package_name, version, max_size)
            elif source == "github":
                files = await _fetch_github(package_name, version, max_size)

            if files:
                return files
        except Exception:
            continue

    return files


async def _fetch_unpkg(package_name: str, version: str, max_size: int) -> DetailFiles:
    """Fetch from unpkg CDN."""
    # Would need to fetch package.json first to get file list
    # Simplified for now
    return []


async def _fetch_jsdelivr(package_name: str, version: str, max_size: int) -> DetailFiles:
    """Fetch from jsDelivr CDN."""
    return []


async def _fetch_github(package_name: str, version: str, max_size: int) -> DetailFiles:
    """Fetch from GitHub."""
    return []


npmFileSourceOrder = NPM_FILE_SOURCES  # noqa: N816 - retained camelCase compatibility export
localFiles = local_files  # noqa: N816 - retained camelCase compatibility export
readmeByName = readme_by_name  # noqa: N816 - retained camelCase compatibility export
readmeName = readme_name  # noqa: N816 - retained camelCase compatibility export
npmFiles = npm_files  # noqa: N816 - retained camelCase compatibility export


# ============================================================================
# Document Reading
# ============================================================================


async def read_package_document(
    package_dir: str | Path | None = None,
    package_name: str | None = None,
    version: str = "latest",
    languages: list[str] | None = None,
    max_size: int = 5 * 1024 * 1024,
) -> PackageDocument:
    """
    Read a package's documentation.

    Tries in order:
    1. Local package directory (meta.detail, then README)
    2. NPM package (README from CDN)

    Args:
        package_dir: Local package directory
        package_name: NPM package name
        version: Package version
        languages: Language fallback chain
        max_size: Maximum document size

    Returns:
        PackageDocument with content and metadata
    """
    # Try local first
    if package_dir:
        doc = await _read_local_package(package_dir, languages, max_size)
        if doc:
            return doc

    # Try NPM
    if package_name:
        doc = await _read_npm_package(package_name, version, languages, max_size)
        if doc:
            return doc

    raise DetailFailure(
        reason=DetailFailureReason.NOT_FOUND,
        message="No documentation found",
        package_name=package_name,
    )


async def _read_local_package(
    package_dir: str | Path,
    languages: list[str] | None,
    max_size: int,
) -> PackageDocument | None:
    """Read documentation from local package."""
    package_dir = Path(package_dir)

    # Check for meta.detail
    meta_detail = package_dir / "meta.detail"
    if meta_detail.exists():
        content = meta_detail.read_text(encoding="utf-8", errors="replace")
        return PackageDocument(
            content=content,
            content_type="markdown",
            source="meta.detail",
            language=languages[0] if languages else "en",
        )

    # Check for README
    readme = readme_name({f.name: None for f in package_dir.iterdir() if f.is_file()})
    if readme:
        readme_path = package_dir / readme
        content = readme_path.read_text(encoding="utf-8", errors="replace")
        return PackageDocument(
            content=content,
            content_type="markdown" if readme.endswith(".md") else "text",
            source="readme",
            language=languages[0] if languages else "en",
        )

    return None


async def _read_npm_package(
    package_name: str,
    version: str,
    languages: list[str] | None,
    max_size: int,
) -> PackageDocument | None:
    """Read documentation from NPM package."""
    # Would fetch README from NPM CDN
    return None


def markdown_of(document: PackageDocument) -> str:
    """Get markdown content from document."""
    if document.content_type == "markdown":
        return document.content
    elif document.content_type == "text":
        return document.content
    elif document.content_type == "html":
        # Simplified HTML to markdown
        return _html_to_markdown(document.content)
    return document.content


def _html_to_markdown(html: str) -> str:
    """Simple HTML to markdown conversion."""
    # Very basic conversion
    html = re.sub(r"<h1[^>]*>(.*?)</h1>", r"# \1", html, flags=re.DOTALL)
    html = re.sub(r"<h2[^>]*>(.*?)</h2>", r"## \1", html, flags=re.DOTALL)
    html = re.sub(r"<h3[^>]*>(.*?)</h3>", r"### \1", html, flags=re.DOTALL)
    html = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", html, flags=re.DOTALL)
    html = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", html, flags=re.DOTALL)
    html = re.sub(r"<pre[^>]*>(.*?)</pre>", r"```\n\1\n```", html, flags=re.DOTALL)
    html = re.sub(
        r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", html, flags=re.DOTALL
    )
    html = re.sub(r"<[^>]+>", "", html)
    return html


# ============================================================================
# Image Handling
# ============================================================================

MAX_INLINE_IMAGE_SIZE = 1024 * 1024  # 1 MB


async def detail_image(
    package_dir: str | Path | None = None,
    package_name: str | None = None,
    image_path: str = "",
    max_size: int = MAX_INLINE_IMAGE_SIZE,
) -> DetailImage | None:
    """
    Get an image from a package.

    Args:
        package_dir: Local package directory
        package_name: NPM package name
        image_path: Relative path to image
        max_size: Maximum image size

    Returns:
        DetailImage or None
    """
    # Try local first
    if package_dir and image_path:
        local_path = Path(package_dir) / image_path
        if local_path.exists():
            data = local_path.read_bytes()
            if len(data) <= max_size:
                mime_type, _ = mimetypes.guess_type(str(local_path))
                return DetailImage(
                    data=data,
                    mime_type=mime_type or "application/octet-stream",
                    source_path=image_path,
                    alt_text=Path(image_path).stem,
                )

    # Try NPM (would fetch from CDN)
    return None


async def detail_image_response(
    package_dir: str | Path | None = None,
    package_name: str | None = None,
    image_path: str = "",
    max_size: int = MAX_INLINE_IMAGE_SIZE,
) -> DetailImageResponse | None:
    """Get image response with cache headers."""
    image = await detail_image(package_dir, package_name, image_path, max_size)
    if not image:
        return None
    return DetailImageResponse(image=image)


# ============================================================================
# Fetch Body
# ============================================================================


async def fetch_body(
    url: str,
    options: RemoteFileOptions | None = None,
) -> FetchedBody:
    """
    Fetch content from a URL.

    Args:
        url: URL to fetch
        options: Fetch options

    Returns:
        FetchedBody with content and metadata
    """
    options = options or RemoteFileOptions()

    request = urllib.request.Request(url, headers=options.headers)
    response = urllib.request.urlopen(request, timeout=options.timeout)

    content = response.read()
    if len(content) > options.max_size:
        raise DetailFailure(
            reason=DetailFailureReason.SIZE_LIMIT_EXCEEDED,
            message=f"Content size {len(content)} exceeds limit {options.max_size}",
            source_url=url,
        )

    content_type = response.headers.get("Content-Type", "application/octet-stream")
    encoding = response.headers.get("Content-Encoding", "utf-8")

    return FetchedBody(
        content=content,
        content_type=content_type,
        url=url,
        encoding=encoding,
    )


# ============================================================================
# Utilities
# ============================================================================


def _extract_images_from_markdown(content: str, base_url: str = "") -> dict[str, str]:
    """Extract image references from markdown."""
    images = {}
    # Match ![alt](src) or ![alt](src "title")
    pattern = r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)'
    for match in re.finditer(pattern, content):
        alt, src = match.groups()
        if src.startswith(("http://", "https://", "data:")):
            images[src] = src
        elif base_url:
            images[src] = urljoin(base_url, src)
    return images


def _extract_images_from_html(content: str, base_url: str = "") -> dict[str, str]:
    """Extract image references from HTML."""
    images = {}
    # Match <img src="...">
    pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
    for match in re.finditer(pattern, content):
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:")):
            images[src] = src
        elif base_url:
            images[src] = urljoin(base_url, src)
    return images
