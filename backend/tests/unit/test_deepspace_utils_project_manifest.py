"""Tests for project_manifest utilities."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.deepspace.utils.project_manifest import (
    DEEPSECTION_KEY,
    MANIFEST_VERSION,
    DeepSpaceBundleManifest,
    DeepSpaceClientManifest,
    DeepSpaceEnginesManifest,
    DeepSpaceManifest,
    DeepSpacePackageManifest,
    DeepSpaceProfileManifest,
    ManifestValidationError,
    PluginLocalizedMeta,
    load_manifest,
    load_manifest_from_file,
    resolve_localized,
    validate_manifest,
)


class TestConstants:
    """Tests for constants."""

    def test_manifest_version(self) -> None:
        assert MANIFEST_VERSION == 1

    def test_deepsection_key(self) -> None:
        assert DEEPSECTION_KEY == "deepspace"


class TestLocalizedText:
    """Tests for localized text resolution."""

    def test_string(self) -> None:
        assert resolve_localized("hello") == "hello"

    def test_dict_with_language(self) -> None:
        text = {"en": "hello", "zh": "你好"}
        assert resolve_localized(text, "zh") == "你好"
        assert resolve_localized(text, "en") == "hello"

    def test_fallback_to_english(self) -> None:
        text = {"fr": "bonjour", "en": "hello"}
        assert resolve_localized(text, "de") == "hello"

    def test_empty_dict(self) -> None:
        text = {}
        assert resolve_localized(text, "en") == ""


class TestPluginLocalizedMeta:
    """Tests for PluginLocalizedMeta."""

    def test_creation(self) -> None:
        meta = PluginLocalizedMeta(
            title={"en": "Test", "zh": "测试"},
            description="A test plugin",
            icon="data:image/png;base64,...",
            error=None,
        )
        assert meta.title == {"en": "Test", "zh": "测试"}
        assert meta.description == "A test plugin"
        assert meta.icon == "data:image/png;base64,..."
        assert meta.error is None


class TestDeepSpaceEnginesManifest:
    """Tests for DeepSpaceEnginesManifest."""

    def test_creation(self) -> None:
        engines = DeepSpaceEnginesManifest(
            deepspace=">=1.0.0",
            python=">=3.11",
            extra={"node": ">=18"},
        )
        assert engines.deepspace == ">=1.0.0"
        assert engines.python == ">=3.11"
        assert engines.extra["node"] == ">=18"

    def test_defaults(self) -> None:
        engines = DeepSpaceEnginesManifest()
        assert engines.deepspace is None
        assert engines.python is None
        assert engines.extra == {}


class TestDeepSpaceBundleManifest:
    """Tests for DeepSpaceBundleManifest."""

    def test_creation(self) -> None:
        bundle = DeepSpaceBundleManifest(
            platforms=["linux", "macos"],
            patches=["patch1.patch", "patch2.patch"],
            listed_by_default=False,
            metadata={"category": "test"},
        )
        assert bundle.platforms == ["linux", "macos"]
        assert bundle.patches == ["patch1.patch", "patch2.patch"]
        assert bundle.listed_by_default is False

    def test_string_patches(self) -> None:
        bundle = DeepSpaceBundleManifest(patches="single.patch")
        assert bundle.patches == "single.patch"

    def test_defaults(self) -> None:
        bundle = DeepSpaceBundleManifest()
        assert bundle.platforms is None
        assert bundle.patches == []
        assert bundle.listed_by_default is True
        assert bundle.metadata == {}


class TestDeepSpaceProfileManifest:
    """Tests for DeepSpaceProfileManifest."""

    def test_creation(self) -> None:
        profile = DeepSpaceProfileManifest(bundles=["core", "agent", "tools"])
        assert profile.bundles == ["core", "agent", "tools"]

    def test_defaults(self) -> None:
        profile = DeepSpaceProfileManifest()
        assert profile.bundles == []


class TestDeepSpaceClientManifest:
    """Tests for DeepSpaceClientManifest."""

    def test_creation(self) -> None:
        client = DeepSpaceClientManifest(
            platform="web",
            inject=["module1", "module2"],
            immediately=True,
            external=["external1"],
        )
        assert client.platform == "web"
        assert client.inject == ["module1", "module2"]
        assert client.immediately is True
        assert client.external == ["external1"]

    def test_defaults(self) -> None:
        client = DeepSpaceClientManifest(platform="web")
        assert client.inject == []
        assert client.immediately is False
        assert client.external == []


class TestDeepSpaceManifest:
    """Tests for DeepSpaceManifest."""

    def test_creation(self) -> None:
        manifest = DeepSpaceManifest(
            manifest_version=1,
            settings={"theme": "dark"},
            extensions={"custom": "value"},
        )
        assert manifest.manifest_version == 1
        assert manifest.settings == {"theme": "dark"}
        assert manifest.extensions == {"custom": "value"}

    def test_defaults(self) -> None:
        manifest = DeepSpaceManifest()
        assert manifest.manifest_version is None
        assert manifest.bundle is None
        assert manifest.profile is None
        assert manifest.client is None
        assert manifest.engines is None
        assert manifest.settings == {}
        assert manifest.extensions == {}


class TestDeepSpacePackageManifest:
    """Tests for DeepSpacePackageManifest."""

    def test_minimal_creation(self) -> None:
        pkg = DeepSpacePackageManifest(
            name="test-package",
            version="1.0.0",
        )
        assert pkg.name == "test-package"
        assert pkg.version == "1.0.0"
        assert pkg.private is False
        assert pkg.dependencies == {}

    def test_full_creation(self) -> None:
        pkg = DeepSpacePackageManifest(
            name="test-package",
            version="1.0.0",
            description="Test package",
            private=True,
            dependencies={"requests": "^2.28"},
            deepspace=DeepSpaceManifest(
                manifest_version=1,
                settings={"theme": "dark"},
            ),
        )
        assert pkg.name == "test-package"
        assert pkg.private is True
        assert pkg.dependencies == {"requests": "^2.28"}
        assert pkg.deepspace is not None
        assert pkg.deepspace.manifest_version == 1


class TestValidateManifest:
    """Tests for validate_manifest."""

    def test_minimal_valid(self) -> None:
        data = {
            "name": "test-package",
            "version": "1.0.0",
        }
        manifest = validate_manifest(data)
        assert manifest.name == "test-package"
        assert manifest.version == "1.0.0"

    def test_missing_name(self) -> None:
        data = {"version": "1.0.0"}
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.name" in exc.value.path

    def test_missing_version(self) -> None:
        data = {"name": "test"}
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.version" in exc.value.path

    def test_empty_name(self) -> None:
        data = {"name": "", "version": "1.0.0"}
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.name" in exc.value.path

    def test_invalid_private(self) -> None:
        data = {"name": "test", "version": "1.0.0", "private": "yes"}
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.private" in exc.value.path

    def test_invalid_dependencies(self) -> None:
        data = {"name": "test", "version": "1.0.0", "dependencies": {"requests": 123}}
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.dependencies.requests" in exc.value.path

    def test_valid_deepspace_section(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {
                "manifestVersion": 1,
                "settings": {"theme": "dark"},
            },
        }
        manifest = validate_manifest(data)
        assert manifest.deepspace is not None
        assert manifest.deepspace.manifest_version == 1
        assert manifest.deepspace.settings == {"theme": "dark"}

    def test_invalid_manifest_version(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {"manifestVersion": "not-int"},
        }
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.deepspace.manifestVersion" in exc.value.path

    def test_invalid_bundle_patches(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {
                "bundle": {"patches": "not-array"},
            },
        }
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.deepspace.bundle.patches" in exc.value.path

    def test_invalid_profile_bundles(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {
                "profile": {"bundles": "not-array"},
            },
        }
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.deepspace.profile.bundles" in exc.value.path

    def test_invalid_client_platform(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {
                "client": {"platform": 123},
            },
        }
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.deepspace.client.platform" in exc.value.path

    def test_invalid_settings(self) -> None:
        data = {
            "name": "test",
            "version": "1.0.0",
            "deepspace": {"settings": "not-object"},
        }
        with pytest.raises(ManifestValidationError) as exc:
            validate_manifest(data)
        assert "$.deepspace.settings" in exc.value.path


class TestLoadManifest:
    """Tests for load_manifest and load_manifest_from_file."""

    def test_load_manifest(self) -> None:
        data = {"name": "test", "version": "1.0.0"}
        manifest = load_manifest(data)
        assert manifest.name == "test"

    def test_load_manifest_from_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("""
name = "test-package"
version = "1.0.0"
description = "Test package"

[deepspace]
manifestVersion = 1
settings = { theme = "dark" }
""")
            f.flush()
            path = Path(f.name)

        try:
            manifest = load_manifest_from_file(path)
            assert manifest.name == "test-package"
            assert manifest.version == "1.0.0"
            assert manifest.deepspace is not None
            assert manifest.deepspace.manifest_version == 1
            assert manifest.deepspace.settings == {"theme": "dark"}
        finally:
            Path(f.name).unlink(missing_ok=True)


class TestResolveLocalized:
    """Tests for resolve_localized."""

    def test_string(self) -> None:
        assert resolve_localized("hello") == "hello"

    def test_dict(self) -> None:
        text = {"en": "hello", "zh": "你好"}
        assert resolve_localized(text, "zh") == "你好"
        assert resolve_localized(text, "en") == "hello"

    def test_fallback(self) -> None:
        text = {"fr": "bonjour", "en": "hello"}
        assert resolve_localized(text, "de") == "hello"

    def test_empty_dict(self) -> None:
        assert resolve_localized({}, "en") == ""


class TestManifestValidationError:
    """Tests for ManifestValidationError."""

    def test_creation(self) -> None:
        exc = ManifestValidationError("$.name", "must be string")
        assert exc.path == "$.name"
        assert exc.reason == "must be string"
        assert "$.name" in str(exc)
        assert "must be string" in str(exc)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
