"""
Project Manifest for DeepSpace.

Port of @deepseek-ai/dsh-package-manifest to Python.
Provides shared type declarations for pyproject.toml configuration fields
with a `deepspace` section for DeepSpace-specific settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    # Core types
    "DeepSpacePackageManifest",
    "DeepSpaceManifest",
    "DeepSpaceBundleManifest",
    "DeepSpaceProfileManifest",
    "DeepSpaceClientManifest",
    "DeepSpaceEnginesManifest",
    "PluginLocalizedMeta",
    "LocalizedText",
    # Validation
    "validate_manifest",
    "load_manifest",
    "load_manifest_from_file",
    "resolve_localized",
    # Constants
    "MANIFEST_VERSION",
    "DEEPSECTION_KEY",
]


# ============================================================================
# Constants
# ============================================================================

MANIFEST_VERSION = 1
DEEPSECTION_KEY = "deepspace"


# ============================================================================
# Localized Text
# ============================================================================

LocalizedText = str | dict[str, str]


def resolve_localized(text: LocalizedText, language: str = "en") -> str:
    """Resolve localized text to a string for the given language."""
    if isinstance(text, str):
        return text
    if language in text:
        return text[language]
    if "en" in text:
        return text["en"]
    return next(iter(text.values()), "")


# ============================================================================
# Plugin Localized Meta
# ============================================================================


@dataclass(frozen=True)
class PluginLocalizedMeta:
    """Validated plugin display fields and diagnostics."""

    title: LocalizedText | None = None
    description: LocalizedText | None = None
    icon: str | None = None  # Base64 data URL or path
    error: str | None = None


# ============================================================================
# Engines Manifest
# ============================================================================


@dataclass(frozen=True)
class DeepSpaceEnginesManifest:
    """Runtime version requirements under pyproject.toml.engines."""

    deepspace: str | None = None
    python: str | None = None
    # Additional engines
    extra: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Bundle Manifest
# ============================================================================


@dataclass(frozen=True)
class DeepSpaceBundleManifest:
    """The configuration layer exported by a bundle package."""

    platforms: list[str] | None = None
    patches: str | list[str] = field(default_factory=list)
    listed_by_default: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Profile Manifest
# ============================================================================


@dataclass(frozen=True)
class DeepSpaceProfileManifest:
    """The bundle composition declared by a profile."""

    bundles: list[str] = field(default_factory=list)


# ============================================================================
# Client Manifest
# ============================================================================


@dataclass(frozen=True)
class DeepSpaceClientManifest:
    """Client module declaration read by client-modules and the client build."""

    platform: str
    inject: list[str] = field(default_factory=list)
    immediately: bool = False
    external: list[str] = field(default_factory=list)


# ============================================================================
# Main Manifest
# ============================================================================


@dataclass(frozen=True)
class DeepSpaceManifest:
    """Public author fields under pyproject.toml.deepspace."""

    manifest_version: int | None = None
    bundle: DeepSpaceBundleManifest | None = None
    profile: DeepSpaceProfileManifest | None = None
    client: DeepSpaceClientManifest | None = None
    engines: DeepSpaceEnginesManifest | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # Custom extension fields
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeepSpacePackageManifest:
    """Package identity and metadata; local profile readers may accept a partial declaration."""

    name: str
    version: str
    description: str | None = None
    icon: str | None = None
    private: bool = False
    dependencies: dict[str, str] = field(default_factory=dict)
    optional_dependencies: dict[str, str] = field(default_factory=dict)
    peer_dependencies: dict[str, str] = field(default_factory=dict)
    engines: DeepSpaceEnginesManifest | None = None
    deepspace: DeepSpaceManifest | None = None
    # PEP 621 fields
    authors: list[dict[str, str]] = field(default_factory=list)
    maintainers: list[dict[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    classifiers: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    license: dict[str, str] | None = None
    readme: str | None = None
    requires_python: str | None = None


# ============================================================================
# Validation
# ============================================================================


class ManifestValidationError(Exception):
    """Raised when manifest validation fails."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def validate_manifest(data: dict[str, Any], path: str = "$") -> DeepSpacePackageManifest:
    """
    Validate a parsed pyproject.toml document.

    Args:
        data: Parsed TOML document
        path: JSON path for error reporting

    Returns:
        Validated DeepSpacePackageManifest

    Raises:
        ManifestValidationError: If validation fails
    """

    def require_object(value: Any, p: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ManifestValidationError(p, "must be an object")
        return value

    def require_string(value: Any, p: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ManifestValidationError(p, "must be a non-empty string")
        return value.strip()

    def optional_string(value: Any, p: str) -> str | None:
        if value is None:
            return None
        return require_string(value, p)

    def optional_dict(value: Any, p: str) -> dict[str, Any] | None:
        if value is None:
            return None
        return require_object(value, p)

    def optional_list(value: Any, p: str, item_validator) -> list | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ManifestValidationError(p, "must be an array")
        return [item_validator(item, f"{p}[{i}]") for i, item in enumerate(value)]

    obj = require_object(data, path)

    # Required fields
    name = require_string(obj.get("name"), f"{path}.name")
    version = require_string(obj.get("version"), f"{path}.version")

    # Optional fields
    description = optional_string(obj.get("description"), f"{path}.description")
    icon = optional_string(obj.get("icon"), f"{path}.icon")
    private = obj.get("private", False)
    if not isinstance(private, bool):
        raise ManifestValidationError(f"{path}.private", "must be boolean")

    # Dependencies
    def string_map(obj: dict, p: str) -> dict[str, str]:
        result = {}
        for k, v in obj.items():
            if not isinstance(v, str):
                raise ManifestValidationError(f"{p}.{k}", "must be a string")
            result[k] = v
        return result

    dependencies = string_map(
        optional_dict(obj.get("dependencies"), f"{path}.dependencies") or {}, f"{path}.dependencies"
    )
    optional_deps = string_map(
        optional_dict(obj.get("optional-dependencies"), f"{path}.optional-dependencies") or {},
        f"{path}.optional-dependencies",
    )
    peer_deps = string_map(
        optional_dict(obj.get("peer-dependencies"), f"{path}.peer-dependencies") or {},
        f"{path}.peer-dependencies",
    )

    # Engines
    engines_data = optional_dict(obj.get("engines"), f"{path}.engines")
    engines = None
    if engines_data:
        engines = DeepSpaceEnginesManifest(
            deepspace=optional_string(engines_data.get("deepspace"), f"{path}.engines.deepspace"),
            python=optional_string(engines_data.get("python"), f"{path}.engines.python"),
            extra={
                k: v
                for k, v in engines_data.items()
                if k not in ("deepspace", "python") and isinstance(v, str)
            },
        )

    # DeepSpace section
    ds_data = optional_dict(obj.get("deepspace"), f"{path}.deepspace")
    deepspace = None
    if ds_data:
        # Manifest version
        manifest_version = ds_data.get("manifestVersion")
        if manifest_version is not None:
            if not isinstance(manifest_version, int):
                raise ManifestValidationError(
                    f"{path}.deepspace.manifestVersion", "must be integer"
                )

        # Bundle
        bundle_data = optional_dict(ds_data.get("bundle"), f"{path}.deepspace.bundle")
        bundle = None
        if bundle_data:
            bundle = DeepSpaceBundleManifest(
                platforms=optional_list(
                    bundle_data.get("platforms"), f"{path}.deepspace.bundle.platforms", str
                )
                or [],
                patches=bundle_data.get("patches", []),
                listed_by_default=bundle_data.get("listedByDefault", True),
                metadata=bundle_data.get("metadata", {}),
            )
            if not isinstance(bundle.patches, list):
                raise ManifestValidationError(f"{path}.deepspace.bundle.patches", "must be array")

        # Profile
        profile_data = optional_dict(ds_data.get("profile"), f"{path}.deepspace.profile")
        profile = None
        if profile_data:
            bundles = profile_data.get("bundles", [])
            if not isinstance(bundles, list):
                raise ManifestValidationError(f"{path}.deepspace.profile.bundles", "must be array")
            profile = DeepSpaceProfileManifest(bundles=bundles)

        # Client
        client_data = optional_dict(ds_data.get("client"), f"{path}.deepspace.client")
        client = None
        if client_data:
            platform = client_data.get("platform")
            if not isinstance(platform, str):
                raise ManifestValidationError(f"{path}.deepspace.client.platform", "must be string")
            client = DeepSpaceClientManifest(
                platform=platform,
                inject=optional_list(
                    client_data.get("inject"), f"{path}.deepspace.client.inject", str
                )
                or [],
                immediately=client_data.get("immediately", False),
                external=optional_list(
                    client_data.get("external"), f"{path}.deepspace.client.external", str
                )
                or [],
            )

        # Engines (deepspace-specific)
        engines_ds = optional_dict(ds_data.get("engines"), f"{path}.deepspace.engines")
        engines_manifest = None
        if engines_ds:
            engines_manifest = DeepSpaceEnginesManifest(
                deepspace=optional_string(
                    engines_ds.get("deepspace"), f"{path}.deepspace.engines.deepspace"
                ),
                python=optional_string(
                    engines_ds.get("python"), f"{path}.deepspace.engines.python"
                ),
                extra={
                    k: v
                    for k, v in engines_ds.items()
                    if k not in ("deepspace", "python") and isinstance(v, str)
                },
            )

        # Settings
        settings = ds_data.get("settings", {})
        if not isinstance(settings, dict):
            raise ManifestValidationError(f"{path}.deepspace.settings", "must be object")

        # Extensions
        extensions = ds_data.get("extensions", {})
        if not isinstance(extensions, dict):
            raise ManifestValidationError(f"{path}.deepspace.extensions", "must be object")

        deepspace = DeepSpaceManifest(
            manifest_version=manifest_version,
            bundle=bundle,
            profile=profile,
            client=client,
            engines=engines_manifest,
            settings=settings,
            extensions=extensions,
        )

    return DeepSpacePackageManifest(
        name=name,
        version=version,
        description=description,
        icon=icon,
        private=private,
        dependencies=dependencies,
        optional_dependencies=optional_deps,
        peer_dependencies=peer_deps,
        engines=engines,
        deepspace=deepspace,
    )


def load_manifest(data: dict[str, Any]) -> DeepSpacePackageManifest:
    """
    Load and validate a manifest from a parsed TOML dict.

    Args:
        data: Parsed TOML dictionary

    Returns:
        Validated DeepSpacePackageManifest
    """
    return validate_manifest(data)


def load_manifest_from_file(path: str | Path) -> DeepSpacePackageManifest:
    """
    Load and validate a manifest from a pyproject.toml file.

    Args:
        path: Path to pyproject.toml

    Returns:
        Validated DeepSpacePackageManifest
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11 compatibility
        import tomli as tomllib
    path = Path(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return validate_manifest(data)
