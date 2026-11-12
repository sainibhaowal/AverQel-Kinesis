"""
Plugin Market Index for DeepSpace.

Port of @deepseek-ai/dsh-plugin-market-index to Python.
Provides plugin market index format v1: JSON validation, relative icon resolution,
and pinned install arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import semver

__all__ = [
    # Types
    "MarketIndexError",
    "MarketIndexMetadata",
    "MarketDeclaration",
    "MarketSortKeyDeclaration",
    "MarketPluginMetadata",
    "MarketSource",
    "MarketRepository",
    "MarketEngines",
    "MarketVersion",
    "MarketPlugin",
    "MarketIndex",
    "MarketInstallTarget",
    # Validation
    "parse_market_index",
    "market_install_target",
    # Constants
    "MARKET_INDEX_FORMAT",
    "MAX_INLINE_ICON_BYTES",
    "LANGUAGE_ID_PATTERN",
    "PACKAGE_NAME_PATTERN",
    "SEMVER_PATTERN",
    "COMMIT_PATTERN",
    "PATH_SEGMENT_PATTERN",
    "DATA_ICON_PATTERN",
    "BASE64_PATTERN",
]


# ============================================================================
# Constants
# ============================================================================

MARKET_INDEX_FORMAT = 1
MAX_INLINE_ICON_BYTES = 32 * 1024  # 32 KiB
MAX_PUBLISH_TIMESTAMP = 10**11

LANGUAGE_ID_PATTERN = re.compile(r"^[a-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
PACKAGE_NAME_PATTERN = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*\/)?[a-z0-9][a-z0-9._~-]*$")
PACKAGE_NAME_MAX_LENGTH = 214

NUMERIC = r"(?:0|[1-9]\d*)"
PRERELEASE_PART = rf"(?:{NUMERIC}|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
SEMVER_PATTERN = re.compile(
    rf"^{NUMERIC}\.{NUMERIC}\.{NUMERIC}(?:-{PRERELEASE_PART}(?:\.{PRERELEASE_PART})*)?(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PATH_SEGMENT_PATTERN = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._@+-]+$")
DATA_ICON_PATTERN = re.compile(
    r"^data:(?:image\/svg\+xml|image\/png|image\/jpeg|image\/webp);base64,([A-Za-z0-9+/]*={0,2})$"
)
BASE64_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}(?:==)?|[A-Za-z0-9+/]{3}=?)?$"
)


# ============================================================================
# Types
# ============================================================================


class MarketIndexError(Exception):
    """An index that violates format 1."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path} {reason}")


@dataclass(frozen=True)
class MarketDeclaration:
    title: dict[str, str]  # LocalizedText


@dataclass(frozen=True)
class MarketSortKeyDeclaration:
    title: dict[str, str]
    order: Literal["asc", "desc"]

    def __post_init__(self) -> None:
        if self.order not in ("asc", "desc"):
            raise ValueError('order must be "asc" or "desc"')


@dataclass(frozen=True)
class MarketPluginMetadata:
    category: str | None = None
    keywords: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    sort_keys: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSource:
    type: Literal["npm", "git"]
    registry: str | None = None
    url: str | None = None
    commit: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class MarketRepository:
    url: str
    directory: str | None = None


@dataclass(frozen=True)
class MarketEngines:
    deepspace: str | None = None


@dataclass(frozen=True)
class MarketVersion:
    version: str
    publish_timestamp: int
    source: MarketSource
    engines: MarketEngines = field(default_factory=MarketEngines)


@dataclass(frozen=True)
class MarketPlugin:
    name: str
    title: dict[str, str] | None = None
    description: dict[str, str] | None = None
    icon: str | None = None
    author: str | None = None
    homepage: str | None = None
    repository: MarketRepository | None = None
    metadata: MarketPluginMetadata = field(default_factory=MarketPluginMetadata)
    versions: list[MarketVersion] = field(default_factory=list)


@dataclass(frozen=True)
class MarketIndexMetadata:
    categories: dict[str, MarketDeclaration] = field(default_factory=dict)
    labels: dict[str, MarketDeclaration] = field(default_factory=dict)
    sort_keys: dict[str, MarketSortKeyDeclaration] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketIndex:
    format: int
    title: dict[str, str]
    description: dict[str, str] | None = None
    metadata: MarketIndexMetadata = field(default_factory=MarketIndexMetadata)
    plugins: list[MarketPlugin] = field(default_factory=list)


@dataclass(frozen=True)
class MarketInstallTarget:
    spec: str
    registry: str | None = None


# ============================================================================
# Validation
# ============================================================================


def parse_market_index(value: Any, index_url: str) -> MarketIndex:
    """
    Validate a parsed format-1 index.

    Args:
        value: The parsed JSON document.
        index_url: Absolute URL the index was read from; relative icon references resolve against it.

    Returns:
        The validated index with absent metadata groups filled with empty values,
        and each plugin's versions in descending SemVer precedence.

    Raises:
        MarketIndexError: For the first violated rule.
    """

    def fail(path: str, reason: str) -> None:
        raise MarketIndexError(path, reason)

    def object_at(value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict) or value is None:
            fail(path, "must be an object")
        return value

    def string_at(value: Any, path: str) -> str:
        if not isinstance(value, str) or not value.strip():
            fail(path, "must be a string with a non-whitespace character")
        return value.strip()

    def optional(obj: dict[str, Any], key: str, path: str, reader) -> Any:
        if key in obj:
            return reader(obj[key], f"{path}.{key}")
        return None

    def localized_text(value: Any, path: str) -> dict[str, str]:
        if isinstance(value, str):
            return {"en": string_at(value, path)}
        obj = object_at(value, path)
        result = {}
        for lang, member in obj.items():
            if not LANGUAGE_ID_PATTERN.match(lang):
                fail(f"{path}.{lang}", "must be keyed by a lowercase language id")
            result[lang] = string_at(member, f"{path}.{lang}")
        if "en" not in result:
            fail(path, "must contain en")
        return result

    def own(record: dict[str, Any], key: str, value: Any) -> None:
        record[key] = value

    def declarations(value: Any, path: str, reader) -> dict[str, Any]:
        result = {}
        obj = object_at(value, path)
        for id_, member in obj.items():
            if not id_:
                fail(path, "must not declare an empty id")
            own(result, id_, reader(object_at(member, f"{path}.{id_}"), f"{path}.{id_}"))
        return result

    def sort_key_declaration(obj: dict[str, Any], path: str) -> MarketSortKeyDeclaration:
        order = obj.get("order")
        if order not in ("asc", "desc"):
            fail(f"{path}.order", 'must be "asc" or "desc"')
        return MarketSortKeyDeclaration(
            title=localized_text(obj["title"], f"{path}.title"),
            order=order,
        )

    def index_metadata(value: Any, path: str) -> MarketIndexMetadata:
        obj = value if value is not None else {}
        if not isinstance(obj, dict):
            fail(path, "must be an object")
        return MarketIndexMetadata(
            categories=optional(
                obj,
                "categories",
                path,
                lambda m, p: declarations(
                    m, p, lambda o, p: MarketDeclaration(title=localized_text(o["title"], p))
                ),
            )
            or {},
            labels=optional(
                obj,
                "labels",
                path,
                lambda m, p: declarations(
                    m, p, lambda o, p: MarketDeclaration(title=localized_text(o["title"], p))
                ),
            )
            or {},
            sort_keys=optional(
                obj, "sortKeys", path, lambda m, p: declarations(m, p, sort_key_declaration)
            )
            or {},
        )

    def plugin_metadata(value: Any, path: str, index: MarketIndexMetadata) -> MarketPluginMetadata:
        obj = value if value is not None else {}
        if not isinstance(obj, dict):
            fail(path, "must be an object")

        category = optional(obj, "category", path, lambda m, p: string_at(m, p))
        if category is not None and category not in index.categories:
            fail(f"{path}.category", f"references undeclared category {category!r}")

        keywords = (
            optional(
                obj,
                "keywords",
                path,
                lambda m, p: (
                    [string_at(kw, f"{p}[{i}]") for i, kw in enumerate(m)]
                    if isinstance(m, list)
                    else fail(p, "must be an array")
                ),
            )
            or []
        )

        labels = (
            optional(
                obj,
                "labels",
                path,
                lambda m, p: (
                    [string_at(lbl, f"{p}[{i}]") for i, lbl in enumerate(m)]
                    if isinstance(m, list)
                    else fail(p, "must be an array")
                ),
            )
            or []
        )
        if len(set(labels)) != len(labels):
            fail(path, "must not repeat a label")
        for lbl in labels:
            if lbl not in index.labels:
                fail(path, f"references undeclared label {lbl!r}")

        sort_keys = (
            optional(
                obj,
                "sortKeys",
                path,
                lambda m, p: (
                    {k: float(v) for k, v in object_at(m, p).items()}
                    if isinstance(m, dict)
                    else fail(p, "must be an object")
                ),
            )
            or {}
        )
        for sk, val in sort_keys.items():
            if sk not in index.sort_keys:
                fail(path, f"references undeclared sort key {sk!r}")
            if not isinstance(val, int | float) or not float(val).is_integer() and val != int(val):
                pass  # Allow any finite number

        return MarketPluginMetadata(
            category=category,
            keywords=keywords,
            labels=labels,
            sort_keys=sort_keys,
        )

    def http_url(value: Any, path: str, protocols: tuple) -> str:
        text = string_at(value, path)
        try:
            url = urlparse(text)
        except Exception:
            fail(path, "must be an absolute URL")
        if url.scheme not in protocols:
            fail(path, f"must use {' or '.join(protocols)}")
        if url.username or url.password:
            fail(path, "must not contain credentials")
        if url.query or url.fragment:
            fail(path, "must not contain a query or fragment")
        return text

    def homepage_url(value: Any, path: str) -> str:
        text = string_at(value, path)
        if not text.startswith(("http://", "https://")):
            fail(path, "must be an absolute URL")
        return text

    def git_url(value: Any, path: str) -> str:
        url = http_url(value, path, ("https:",))
        parsed = urlparse(url)
        if not re.match(r"^(?:/[^/]+){2,}$", parsed.path):
            fail(path, "must name a repository with at least two path segments")
        if re.search(r"\.(?:tgz|tar\.gz)$", parsed.path, re.IGNORECASE):
            fail(path, "must name a repository, not a tarball")
        return url

    def package_path(value: Any, path: str) -> str:
        text = string_at(value, path)
        segments = text.split("/")
        for seg in segments:
            if seg in (".", "..") or not PATH_SEGMENT_PATTERN.match(seg):
                fail(
                    path,
                    "must be a relative directory of letters, digits, and ._@+- segments without . or ..",
                )
        return text

    def repository(value: Any, path: str) -> MarketRepository:
        obj = object_at(value, path)
        url = http_url(obj["url"], f"{path}.url", ("https:", "http:"))
        directory = optional(obj, "directory", path, package_path)
        return MarketRepository(url=url, directory=directory)

    def source(value: Any, path: str) -> MarketSource:
        obj = object_at(value, path)
        if obj.get("type") == "npm":
            registry = optional(
                obj, "registry", path, lambda m, p: http_url(m, p, ("https:", "http:"))
            )
            return MarketSource(type="npm", registry=registry)
        elif obj.get("type") == "git":
            commit = string_at(obj.get("commit"), f"{path}.commit")
            if not COMMIT_PATTERN.match(commit):
                fail(f"{path}.commit", "must be a lowercase 40-hex-digit commit SHA")
            package_dir = optional(obj, "path", path, package_path)
            return MarketSource(
                type="git",
                url=git_url(obj["url"], f"{path}.url"),
                commit=commit,
                path=package_dir,
            )
        else:
            fail(f"{path}.type", 'must be "npm" or "git"')

    def engines(value: Any, path: str) -> MarketEngines:
        obj = object_at(value, path)
        deepspace = optional(obj, "deepspace", path, lambda m, p: string_at(m, p))
        if deepspace is not None:
            if deepspace.startswith("workspace:"):
                fail(path, "must not be a workspace: range")
            if semver.valid_range(deepspace) is None:
                fail(path, "must be a SemVer range")
        return MarketEngines(deepspace=deepspace)

    def icon(value: Any, path: str, index_url: str) -> str:
        text = string_at(value, path)
        if text.startswith("data:"):
            match = DATA_ICON_PATTERN.match(text)
            if not match:
                fail(path, "must be a base64 SVG, PNG, JPEG, or WebP data URL")
            payload = match.group(1)
            if not BASE64_PATTERN.match(payload):
                fail(path, "must be a base64 SVG, PNG, JPEG, or WebP data URL")
            # Approximate decoded size
            bytes_size = len(payload) * 3 // 4 - (
                2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
            )
            if bytes_size > MAX_INLINE_ICON_BYTES:
                fail(path, f"must not exceed {MAX_INLINE_ICON_BYTES // 1024} KiB of decoded bytes")
            return text

        # Relative URL
        try:
            resolved = urlparse(text)
            if resolved.scheme in ("http", "https"):
                return text
            # Relative to index URL
            base = urlparse(index_url)
            resolved = urlparse(text)
            if not resolved.scheme:
                # Relative path
                full_url = f"{base.scheme}://{base.netloc}{text}"
                return full_url
        except Exception:
            pass

        fail(path, "must be a URL reference")

    def market_version(value: Any, path: str) -> MarketVersion:
        obj = object_at(value, path)
        version = string_at(obj["version"], f"{path}.version")
        if not SEMVER_PATTERN.match(version):
            fail(f"{path}.version", "must be an exact SemVer version")

        publish_ts = obj.get("publishTimestamp")
        if not isinstance(publish_ts, int) or publish_ts < 0 or publish_ts >= 10**11:
            fail(
                f"{path}.publishTimestamp",
                "must be an integer number of Unix seconds in [0, 10^11)",
            )

        return MarketVersion(
            version=version,
            publish_timestamp=publish_ts,
            source=source(obj["source"], f"{path}.source"),
            engines=optional(obj, "engines", path, engines) or MarketEngines(),
        )

    def versions(value: Any, path: str) -> list[MarketVersion]:
        if not isinstance(value, list) or not value:
            fail(path, "must be a non-empty array")
        seen = set()
        parsed = []
        for i, member in enumerate(value):
            entry = market_version(member, f"{path}[{i}]")
            if entry.version in seen:
                fail(f"{path}[{i}].version", f"duplicates {entry.version}")
            seen.add(entry.version)
            parsed.append(entry)
        # Sort by descending SemVer precedence
        parsed.sort(key=lambda v: semver.VersionInfo.parse(v.version), reverse=True)
        return parsed

    def plugin(value: Any, path: str, index: MarketIndexMetadata, index_url: str) -> MarketPlugin:
        obj = object_at(value, path)
        name = string_at(obj["name"], f"{path}.name")
        if len(name) > PACKAGE_NAME_MAX_LENGTH or not PACKAGE_NAME_PATTERN.match(name):
            fail(f"{path}.name", "must be a valid npm package name")

        return MarketPlugin(
            name=name,
            title=optional(obj, "title", path, localized_text),
            description=optional(obj, "description", path, localized_text),
            icon=optional(obj, "icon", path, lambda v, p: icon(v, p, index_url)),
            author=optional(obj, "author", path, string_at),
            homepage=optional(obj, "homepage", path, homepage_url),
            repository=optional(obj, "repository", path, repository),
            metadata=plugin_metadata(obj.get("metadata", {}), f"{path}.metadata", index),
            versions=versions(obj["versions"], f"{path}.versions"),
        )

    # Main validation
    if not isinstance(value, dict):
        fail("$", "must be an object")

    if value.get("format") != MARKET_INDEX_FORMAT:
        fail("$.format", "must be 1")

    base_url = urlparse(index_url)
    if not base_url.scheme or not base_url.netloc:
        fail("$", f"index URL {index_url!r} must be an absolute URL")

    metadata = index_metadata(value.get("metadata"), "$.metadata")

    plugins_list = value.get("plugins")
    if not isinstance(plugins_list, list):
        fail("$.plugins", "must be an array")

    seen_names = set()
    plugins = []
    for i, entry in enumerate(plugins_list):
        parsed = plugin(entry, f"$.plugins[{i}]", metadata, index_url)
        if parsed.name in seen_names:
            fail(f"$.plugins[{i}].name", f"duplicates {parsed.name}")
        seen_names.add(parsed.name)
        plugins.append(parsed)

    title = localized_text(value["title"], "$.title")
    description = optional(value, "description", "$", localized_text)

    return MarketIndex(
        format=MARKET_INDEX_FORMAT,
        title=title,
        description=description,
        metadata=metadata,
        plugins=plugins,
    )


def market_install_target(
    name: str, version: MarketVersion | dict[str, Any]
) -> MarketInstallTarget:
    """
    Build the arguments that install one exact version.

    Args:
        name: The plugin's package name.
        version: A validated version of that plugin.

    Returns:
        The install spec and the registry to ask first, when the version names one.
    """
    if not isinstance(name, str) or not PACKAGE_NAME_PATTERN.fullmatch(name):
        raise MarketIndexError("$.name", "must be a valid npm package name")

    version_text = version.get("version") if isinstance(version, dict) else version.version
    if not isinstance(version_text, str) or not SEMVER_PATTERN.fullmatch(version_text):
        raise MarketIndexError("$.version", "must be an exact SemVer version")

    raw_source = version.get("source") if isinstance(version, dict) else version.source
    if isinstance(raw_source, MarketSource):
        source = raw_source
    elif isinstance(raw_source, dict):
        source_type = raw_source.get("type")
        if source_type == "npm":
            registry = raw_source.get("registry")
            if registry is not None:
                parsed_registry = urlparse(registry) if isinstance(registry, str) else None
                if (
                    parsed_registry is None
                    or parsed_registry.scheme not in ("http", "https")
                    or not parsed_registry.netloc
                    or parsed_registry.username
                    or parsed_registry.password
                ):
                    raise MarketIndexError("$.source.registry", "must be an absolute HTTP(S) URL")
            source = MarketSource(type="npm", registry=registry)
        elif source_type == "git":
            repository_url = raw_source.get("url")
            commit = raw_source.get("commit")
            parsed_repository = (
                urlparse(repository_url) if isinstance(repository_url, str) else None
            )
            if (
                parsed_repository is None
                or parsed_repository.scheme != "https"
                or not parsed_repository.netloc
                or parsed_repository.username
                or parsed_repository.password
                or parsed_repository.query
                or parsed_repository.fragment
            ):
                raise MarketIndexError("$.source.url", "must be an absolute HTTPS repository URL")
            if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
                raise MarketIndexError(
                    "$.source.commit", "must be a lowercase 40-hex-digit commit SHA"
                )
            package_path = raw_source.get("path")
            if package_path is not None and (
                not isinstance(package_path, str)
                or any(
                    segment in ("", ".", "..") or not PATH_SEGMENT_PATTERN.fullmatch(segment)
                    for segment in package_path.split("/")
                )
            ):
                raise MarketIndexError("$.source.path", "must be a safe relative package directory")
            source = MarketSource(type="git", url=repository_url, commit=commit, path=package_path)
        else:
            raise MarketIndexError("$.source.type", 'must be "npm" or "git"')
    else:
        raise MarketIndexError("$.source", "must be a validated source object")

    if source.type == "npm":
        return MarketInstallTarget(
            spec=f"{name}@{version_text}",
            registry=source.registry,
        )
    else:
        repository = source.url
        if not repository.endswith(".git"):
            repository = f"{repository}.git"
        path_part = f"&path:/{source.path}" if source.path else ""
        return MarketInstallTarget(
            spec=f"git+{repository}#{source.commit}{path_part}",
        )


# Backwards compatibility aliases
MARKET_INDEX_FORMAT = 1
PACKAGE_NAME_MAX_LENGTH = 214
