"""
Official Bundles / Preset Catalog for DeepSpace.

Port of @deepseek-ai/dsh-official-bundles to Python.
Provides installation-owned bundle lists, profile templates, and plugin manager
protected/presented package lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    # Bundle types
    "ProfileTemplate",
    "BundleManifest",
    # Bundle lists
    "DEFAULT_PROFILE_BUNDLES",
    "INSTALLATION_OWNED_PROFILE_TUPLES",
    "ON_DEMAND_BUNDLES",
    "OPTIONAL_BUNDLES",
    "PROFILE_TEMPLATES",
    "RETIRED_BUNDLES",
    # Plugin manager
    "EXPERIMENTAL_BUNDLES",
    "EXTENSION_BUNDLES",
    "HIDDEN_BUNDLES",
    "MORE_BUNDLES",
    "PROTECTED_PLUGINS",
    # Catalog
    "OfficialBundleCatalogEntry",
    "OFFICIAL_ON_DEMAND_CATALOG",
    # Functions
    "get_bundle_manifest",
    "resolve_profile_bundles",
    "is_bundle_protected",
    "is_bundle_experimental",
    "get_on_demand_bundles",
    "get_optional_bundles",
    "get_profile_templates",
]


# ============================================================================
# Bundle Types
# ============================================================================


@dataclass(frozen=True)
class BundleManifest:
    """Metadata for a bundle."""

    name: str
    version: str
    description: str
    platforms: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    listed_by_default: bool = True
    dependencies: list[str] = field(default_factory=list)
    optional_dependencies: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileTemplate:
    """A profile template composition."""

    name: str
    description: str
    bundles: list[str]
    category: str = "general"
    icon: str | None = None
    order: int = 0
    hidden: bool = False


# ============================================================================
# Bundle Lists (Installation-Owned)
# ============================================================================

# Default profile bundles - always included
DEFAULT_PROFILE_BUNDLES: tuple[str, ...] = (
    "core",
    "agent",
    "tools",
    "workspace",
    "session",
)

# Installation-owned profile tuples (profile_name -> bundle list)
INSTALLATION_OWNED_PROFILE_TUPLES: dict[str, tuple[str, ...]] = {
    "standard": (
        "core",
        "agent",
        "tools",
        "workspace",
        "session",
        "mcp",
        "browser",
    ),
    "minimal": (
        "core",
        "agent",
        "tools",
    ),
    "creator": (
        "core",
        "agent",
        "tools",
        "workspace",
        "session",
        "mcp",
        "browser",
        "skills",
        "extensions",
    ),
    "ptc": (
        "core",
        "agent",
        "tools",
        "ptc",
    ),
    "headless": (
        "core",
        "agent",
        "tools",
        "workspace",
    ),
}

# On-demand bundles - installed when needed
ON_DEMAND_BUNDLES: tuple[str, ...] = (
    "browser",
    "computer-use",
    "ssh",
    "terminal",
    "mcp",
    "skills",
    "extensions",
    "computer-vision",
    "audio",
)

# Optional bundles - user can enable
OPTIONAL_BUNDLES: tuple[str, ...] = (
    "telemetry",
    "debug",
    "profiling",
    "experimental",
)

# Profile templates for UI
PROFILE_TEMPLATES: tuple[ProfileTemplate, ...] = (
    ProfileTemplate(
        name="standard",
        description="Standard DeepSpace experience with all core features",
        bundles=["core", "agent", "tools", "workspace", "session", "mcp", "browser"],
        category="general",
        order=1,
    ),
    ProfileTemplate(
        name="minimal",
        description="Minimal footprint for resource-constrained environments",
        bundles=["core", "agent", "tools"],
        category="general",
        order=2,
    ),
    ProfileTemplate(
        name="creator",
        description="Full-featured with extensions and skills",
        bundles=[
            "core",
            "agent",
            "tools",
            "workspace",
            "session",
            "mcp",
            "browser",
            "skills",
            "extensions",
        ],
        category="general",
        order=3,
    ),
    ProfileTemplate(
        name="ptc",
        description="PTC (Persistent Terminal Control) mode",
        bundles=["core", "agent", "tools", "ptc"],
        category="specialized",
        order=10,
    ),
    ProfileTemplate(
        name="headless",
        description="Headless server mode",
        bundles=["core", "agent", "tools", "workspace"],
        category="specialized",
        order=11,
    ),
)

# Retired bundles - no longer maintained
RETIRED_BUNDLES: tuple[str, ...] = (
    "legacy-agent",
    "old-browser",
    "deprecated-tools",
)


# ============================================================================
# Plugin Manager Lists
# ============================================================================

# Protected plugins - cannot be disabled by user
PROTECTED_PLUGINS: set[str] = {
    "core",
    "agent",
    "session",
    "workspace",
    "tools",
    "auth",
    "security",
}

# Experimental bundles - opt-in only
EXPERIMENTAL_BUNDLES: set[str] = {
    "experimental",
    "computer-vision",
    "audio",
    "profiling",
}

# Extension bundles - add-on functionality
EXTENSION_BUNDLES: set[str] = {
    "skills",
    "extensions",
    "computer-use",
    "ssh",
    "terminal",
}

# Hidden bundles - internal use only
HIDDEN_BUNDLES: set[str] = {
    "internal",
    "migration",
    "compat",
}

# More bundles - additional curated bundles
MORE_BUNDLES: dict[str, str] = {
    "academic": "Academic research tools",
    "devops": "DevOps and infrastructure tools",
    "data-science": "Data science and ML tools",
    "writing": "Writing and documentation tools",
}


# ============================================================================
# Official Bundle Catalog
# ============================================================================


@dataclass(frozen=True)
class OfficialBundleCatalogEntry:
    """Offline discovery metadata for a bundle."""

    package_name: str
    platforms: list[str] | None = None
    listed_by_default: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


# Generated catalog - in production this would be generated from NPM
OFFICIAL_ON_DEMAND_CATALOG: dict[str, OfficialBundleCatalogEntry] = {
    "browser": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-browser",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "In-app browser automation", "category": "automation"},
    ),
    "computer-use": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-computer-use",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "Desktop automation via VNC", "category": "automation"},
    ),
    "ssh": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-ssh",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "SSH remote execution", "category": "remote"},
    ),
    "terminal": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-terminal",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "Persistent terminal sessions", "category": "system"},
    ),
    "mcp": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-mcp",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "MCP server connector", "category": "integration"},
    ),
    "skills": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-skills",
        platforms=["linux", "macos", "windows"],
        metadata={
            "description": "Built-in skills (Office, Python, Security)",
            "category": "skills",
        },
    ),
    "extensions": OfficialBundleCatalogEntry(
        package_name="@deepseek-ai/dsh-bundle-extensions",
        platforms=["linux", "macos", "windows"],
        metadata={"description": "Declarative extensions system", "category": "extensibility"},
    ),
}


# ============================================================================
# Helper Functions
# ============================================================================

# In-memory bundle registry
_bundle_registry: dict[str, BundleManifest] = {}


def register_bundle(manifest: BundleManifest) -> None:
    """Register a bundle manifest."""
    _bundle_registry[manifest.name] = manifest


def get_bundle_manifest(name: str) -> BundleManifest | None:
    """Get bundle manifest by name."""
    return _bundle_registry.get(name)


def resolve_profile_bundles(profile_name: str) -> list[str]:
    """Resolve a profile name to its bundle list."""
    if profile_name in INSTALLATION_OWNED_PROFILE_TUPLES:
        return list(INSTALLATION_OWNED_PROFILE_TUPLES[profile_name])
    return []


def is_bundle_protected(name: str) -> bool:
    """Check if a bundle/plugin is protected."""
    return name in PROTECTED_PLUGINS


def is_bundle_experimental(name: str) -> bool:
    """Check if a bundle is experimental."""
    return name in EXPERIMENTAL_BUNDLES


def is_bundle_on_demand(name: str) -> bool:
    """Check if a bundle is on-demand."""
    return name in ON_DEMAND_BUNDLES


def is_bundle_optional(name: str) -> bool:
    """Check if a bundle is optional."""
    return name in OPTIONAL_BUNDLES


def is_bundle_retired(name: str) -> bool:
    """Check if a bundle is retired."""
    return name in RETIRED_BUNDLES


def is_bundle_hidden(name: str) -> bool:
    """Check if a bundle is hidden."""
    return name in HIDDEN_BUNDLES


def is_bundle_extension(name: str) -> bool:
    """Check if a bundle is an extension."""
    return name in EXTENSION_BUNDLES


def get_on_demand_bundles() -> list[str]:
    """Get all on-demand bundles."""
    return list(ON_DEMAND_BUNDLES)


def get_optional_bundles() -> list[str]:
    """Get all optional bundles."""
    return list(OPTIONAL_BUNDLES)


def get_profile_templates() -> list[ProfileTemplate]:
    """Get all profile templates."""
    return list(PROFILE_TEMPLATES)


def get_protected_plugins() -> list[str]:
    """Get all protected plugins."""
    return list(PROTECTED_PLUGINS)


def get_experimental_bundles() -> list[str]:
    """Get all experimental bundles."""
    return list(EXPERIMENTAL_BUNDLES)


def get_extension_bundles() -> list[str]:
    """Get all extension bundles."""
    return list(EXTENSION_BUNDLES)


def get_more_bundles() -> dict[str, str]:
    """Get more bundles catalog."""
    return dict(MORE_BUNDLES)


def get_catalog_entry(bundle_name: str) -> OfficialBundleCatalogEntry | None:
    """Get catalog entry for a bundle."""
    return OFFICIAL_ON_DEMAND_CATALOG.get(bundle_name)


def list_catalog() -> dict[str, OfficialBundleCatalogEntry]:
    """List all catalog entries."""
    return dict(OFFICIAL_ON_DEMAND_CATALOG)


# Pre-register default bundles
for name in DEFAULT_PROFILE_BUNDLES:
    register_bundle(
        BundleManifest(
            name=name,
            version="1.0.0",
            description=f"Default bundle: {name}",
        )
    )

for name in ON_DEMAND_BUNDLES:
    register_bundle(
        BundleManifest(
            name=name,
            version="1.0.0",
            description=f"On-demand bundle: {name}",
        )
    )
