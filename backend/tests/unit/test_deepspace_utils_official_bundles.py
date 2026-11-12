"""Tests for official_bundles utilities."""

from __future__ import annotations

import pytest

from app.deepspace.utils.official_bundles import (
    # Bundle lists
    DEFAULT_PROFILE_BUNDLES,
    # Plugin manager
    EXPERIMENTAL_BUNDLES,
    EXTENSION_BUNDLES,
    HIDDEN_BUNDLES,
    INSTALLATION_OWNED_PROFILE_TUPLES,
    MORE_BUNDLES,
    # Catalog
    OFFICIAL_ON_DEMAND_CATALOG,
    ON_DEMAND_BUNDLES,
    OPTIONAL_BUNDLES,
    PROFILE_TEMPLATES,
    PROTECTED_PLUGINS,
    RETIRED_BUNDLES,
    # Types
    BundleManifest,
    OfficialBundleCatalogEntry,
    ProfileTemplate,
    # Functions
    get_bundle_manifest,
    get_catalog_entry,
    get_experimental_bundles,
    get_extension_bundles,
    get_more_bundles,
    get_on_demand_bundles,
    get_optional_bundles,
    get_profile_templates,
    get_protected_plugins,
    is_bundle_experimental,
    is_bundle_extension,
    is_bundle_hidden,
    is_bundle_on_demand,
    is_bundle_optional,
    is_bundle_protected,
    is_bundle_retired,
    list_catalog,
    register_bundle,
    resolve_profile_bundles,
)


class TestBundleManifest:
    """Tests for BundleManifest."""

    def test_creation(self) -> None:
        manifest = BundleManifest(
            name="test-bundle",
            version="1.0.0",
            description="Test bundle",
            platforms=["linux", "macos"],
            listed_by_default=True,
        )
        assert manifest.name == "test-bundle"
        assert manifest.version == "1.0.0"
        assert manifest.platforms == ["linux", "macos"]

    def test_defaults(self) -> None:
        manifest = BundleManifest(
            name="test",
            version="1.0",
            description="Test",
        )
        assert manifest.platforms == []
        assert manifest.listed_by_default is True
        assert manifest.dependencies == []


class TestProfileTemplate:
    """Tests for ProfileTemplate."""

    def test_creation(self) -> None:
        template = ProfileTemplate(
            name="test",
            description="Test profile",
            bundles=["core", "agent"],
            category="general",
            order=1,
        )
        assert template.name == "test"
        assert template.bundles == ["core", "agent"]
        assert template.category == "general"
        assert template.order == 1
        assert template.hidden is False


class TestOfficialBundleCatalogEntry:
    """Tests for OfficialBundleCatalogEntry."""

    def test_creation(self) -> None:
        entry = OfficialBundleCatalogEntry(
            package_name="@test/bundle",
            platforms=["linux", "macos"],
            metadata={"category": "test"},
        )
        assert entry.package_name == "@test/bundle"
        assert entry.platforms == ["linux", "macos"]
        assert entry.metadata["category"] == "test"
        assert entry.listed_by_default is True


class TestBundleLists:
    """Tests for bundle list constants."""

    def test_default_profile_bundles(self) -> None:
        assert "core" in DEFAULT_PROFILE_BUNDLES
        assert "agent" in DEFAULT_PROFILE_BUNDLES
        assert len(DEFAULT_PROFILE_BUNDLES) >= 5

    def test_installation_owned_profiles(self) -> None:
        assert "standard" in INSTALLATION_OWNED_PROFILE_TUPLES
        assert "minimal" in INSTALLATION_OWNED_PROFILE_TUPLES
        assert "creator" in INSTALLATION_OWNED_PROFILE_TUPLES
        assert "ptc" in INSTALLATION_OWNED_PROFILE_TUPLES
        assert "headless" in INSTALLATION_OWNED_PROFILE_TUPLES

    def test_on_demand_bundles(self) -> None:
        assert "browser" in ON_DEMAND_BUNDLES
        assert "ssh" in ON_DEMAND_BUNDLES
        assert "mcp" in ON_DEMAND_BUNDLES

    def test_optional_bundles(self) -> None:
        assert "telemetry" in OPTIONAL_BUNDLES
        assert "debug" in OPTIONAL_BUNDLES

    def test_retired_bundles(self) -> None:
        assert "legacy-agent" in RETIRED_BUNDLES

    def test_profile_templates(self) -> None:
        templates = list(PROFILE_TEMPLATES)
        assert len(templates) >= 5
        names = {t.name for t in templates}
        assert "standard" in names
        assert "minimal" in names
        assert "creator" in names


class TestPluginManagerLists:
    """Tests for plugin manager lists."""

    def test_protected_plugins(self) -> None:
        assert "core" in PROTECTED_PLUGINS
        assert "agent" in PROTECTED_PLUGINS
        assert "session" in PROTECTED_PLUGINS

    def test_experimental_bundles(self) -> None:
        assert "experimental" in EXPERIMENTAL_BUNDLES
        assert "computer-vision" in EXPERIMENTAL_BUNDLES

    def test_extension_bundles(self) -> None:
        assert "skills" in EXTENSION_BUNDLES
        assert "extensions" in EXTENSION_BUNDLES

    def test_hidden_bundles(self) -> None:
        assert "internal" in HIDDEN_BUNDLES

    def test_more_bundles(self) -> None:
        assert "academic" in MORE_BUNDLES
        assert "devops" in MORE_BUNDLES


class TestCatalog:
    """Tests for official bundle catalog."""

    def test_catalog_entries(self) -> None:
        assert "browser" in OFFICIAL_ON_DEMAND_CATALOG
        assert "ssh" in OFFICIAL_ON_DEMAND_CATALOG
        assert "mcp" in OFFICIAL_ON_DEMAND_CATALOG

    def test_catalog_entry_structure(self) -> None:
        entry = OFFICIAL_ON_DEMAND_CATALOG["browser"]
        assert entry.package_name == "@deepseek-ai/dsh-bundle-browser"
        assert "linux" in entry.platforms
        assert entry.metadata["category"] == "automation"


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_get_bundle_manifest(self) -> None:
        # Default bundles are pre-registered
        manifest = get_bundle_manifest("core")
        assert manifest is not None
        assert manifest.name == "core"

    def test_register_bundle(self) -> None:
        manifest = BundleManifest(
            name="test-new-bundle",
            version="1.0.0",
            description="Test",
        )
        register_bundle(manifest)
        retrieved = get_bundle_manifest("test-new-bundle")
        assert retrieved is not None
        assert retrieved.name == "test-new-bundle"

    def test_resolve_profile_bundles(self) -> None:
        bundles = resolve_profile_bundles("standard")
        assert "core" in bundles
        assert "agent" in bundles
        assert "browser" in bundles

        # Unknown profile returns empty
        assert resolve_profile_bundles("unknown") == []

    def test_is_bundle_protected(self) -> None:
        assert is_bundle_protected("core") is True
        assert is_bundle_protected("agent") is True
        assert is_bundle_protected("browser") is False

    def test_is_bundle_experimental(self) -> None:
        assert is_bundle_experimental("experimental") is True
        assert is_bundle_experimental("core") is False

    def test_is_bundle_on_demand(self) -> None:
        assert is_bundle_on_demand("browser") is True
        assert is_bundle_on_demand("core") is False

    def test_is_bundle_optional(self) -> None:
        assert is_bundle_optional("telemetry") is True
        assert is_bundle_optional("core") is False

    def test_is_bundle_retired_catalog(self) -> None:
        assert is_bundle_retired("legacy-agent") is True
        assert is_bundle_retired("core") is False

    def test_is_bundle_hidden_catalog(self) -> None:
        assert is_bundle_hidden("internal") is True
        assert is_bundle_hidden("core") is False

    def test_is_bundle_extension_catalog(self) -> None:
        assert is_bundle_extension("skills") is True
        assert is_bundle_extension("core") is False

    def test_get_on_demand_bundles(self) -> None:
        bundles = get_on_demand_bundles()
        assert "browser" in bundles
        assert isinstance(bundles, list)

    def test_get_optional_bundles(self) -> None:
        bundles = get_optional_bundles()
        assert "telemetry" in bundles
        assert isinstance(bundles, list)

    def test_get_profile_templates(self) -> None:
        templates = get_profile_templates()
        assert len(templates) >= 5
        assert all(isinstance(t, ProfileTemplate) for t in templates)

    def test_get_protected_plugins(self) -> None:
        plugins = get_protected_plugins()
        assert "core" in plugins
        assert "agent" in plugins
        assert isinstance(plugins, list)

    def test_get_experimental_bundles(self) -> None:
        bundles = get_experimental_bundles()
        assert "experimental" in bundles
        assert isinstance(bundles, list)

    def test_get_extension_bundles(self) -> None:
        bundles = get_extension_bundles()
        assert "skills" in bundles
        assert isinstance(bundles, list)

    def test_get_more_bundles(self) -> None:
        more = get_more_bundles()
        assert "academic" in more
        assert isinstance(more, dict)

    def test_get_catalog_entry(self) -> None:
        entry = get_catalog_entry("browser")
        assert entry is not None
        assert entry.package_name == "@deepseek-ai/dsh-bundle-browser"

    def test_get_catalog_entry_missing(self) -> None:
        entry = get_catalog_entry("nonexistent")
        assert entry is None

    def test_list_catalog(self) -> None:
        catalog = list_catalog()
        assert len(catalog) >= 7
        assert "browser" in catalog

    def test_is_bundle_retired(self) -> None:
        assert is_bundle_retired("legacy-agent") is True
        assert is_bundle_retired("core") is False

    def test_is_bundle_hidden(self) -> None:
        assert is_bundle_hidden("internal") is True
        assert is_bundle_hidden("core") is False

    def test_is_bundle_extension(self) -> None:
        assert is_bundle_extension("skills") is True
        assert is_bundle_extension("core") is False


class TestProfileTemplates:
    """Tests for profile templates."""

    def test_all_templates_have_required_fields(self) -> None:
        for template in PROFILE_TEMPLATES:
            assert template.name
            assert template.description
            assert isinstance(template.bundles, list)
            assert len(template.bundles) > 0
            assert template.category
            assert isinstance(template.order, int)

    def test_standard_template(self) -> None:
        std = next(t for t in PROFILE_TEMPLATES if t.name == "standard")
        assert "core" in std.bundles
        assert "browser" in std.bundles
        assert std.category == "general"

    def test_minimal_template(self) -> None:
        min_t = next(t for t in PROFILE_TEMPLATES if t.name == "minimal")
        assert "core" in min_t.bundles
        assert "browser" not in min_t.bundles
        assert len(min_t.bundles) < 5

    def test_creator_template(self) -> None:
        creator = next(t for t in PROFILE_TEMPLATES if t.name == "creator")
        assert "skills" in creator.bundles
        assert "extensions" in creator.bundles
        assert len(creator.bundles) >= 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
