"""Tests for plugin_market_index utilities."""

from __future__ import annotations

import pytest

from app.deepspace.utils.plugin_market_index import (
    BASE64_PATTERN,
    COMMIT_PATTERN,
    DATA_ICON_PATTERN,
    LANGUAGE_ID_PATTERN,
    MARKET_INDEX_FORMAT,
    MAX_INLINE_ICON_BYTES,
    PACKAGE_NAME_PATTERN,
    PATH_SEGMENT_PATTERN,
    SEMVER_PATTERN,
    MarketDeclaration,
    MarketEngines,
    MarketIndex,
    MarketIndexError,
    MarketIndexMetadata,
    MarketInstallTarget,
    MarketPlugin,
    MarketPluginMetadata,
    MarketRepository,
    MarketSortKeyDeclaration,
    MarketSource,
    MarketVersion,
    market_install_target,
    parse_market_index,
)


class TestConstants:
    """Tests for constants."""

    def test_format_version(self) -> None:
        assert MARKET_INDEX_FORMAT == 1

    def test_max_inline_icon_bytes(self) -> None:
        assert MAX_INLINE_ICON_BYTES == 32 * 1024

    def test_regex_patterns(self) -> None:
        assert LANGUAGE_ID_PATTERN.match("en")
        assert LANGUAGE_ID_PATTERN.match("zh-CN")
        assert LANGUAGE_ID_PATTERN.match("en-US")
        assert not LANGUAGE_ID_PATTERN.match("EN")  # Must be lowercase
        assert not LANGUAGE_ID_PATTERN.match("en_US")  # Underscore not allowed

        assert PACKAGE_NAME_PATTERN.match("pkg")
        assert PACKAGE_NAME_PATTERN.match("@scope/pkg")
        assert PACKAGE_NAME_PATTERN.match("pkg-name")
        assert not PACKAGE_NAME_PATTERN.match("PKG")  # Must be lowercase

        assert SEMVER_PATTERN.match("1.0.0")
        assert SEMVER_PATTERN.match("1.2.3-alpha.1")
        assert SEMVER_PATTERN.match("2.0.0+build.123")
        assert not SEMVER_PATTERN.match("1.0")  # Must have 3 parts

        assert COMMIT_PATTERN.match("a" * 40)
        assert not COMMIT_PATTERN.match("g" * 40)

        assert PATH_SEGMENT_PATTERN.match("simple")
        assert PATH_SEGMENT_PATTERN.match("with.dots")
        assert PATH_SEGMENT_PATTERN.match("with_underscore")
        assert not PATH_SEGMENT_PATTERN.match("with spaces")
        assert not PATH_SEGMENT_PATTERN.match("..")

        assert DATA_ICON_PATTERN.match("data:image/png;base64,SGVsbG8=")
        assert not DATA_ICON_PATTERN.match("data:text/plain;base64,SGVsbG8=")

        assert BASE64_PATTERN.match("SGVsbG8=")
        assert BASE64_PATTERN.match("SGVsbG8")
        assert not BASE64_PATTERN.match("not-base64!")


class TestMarketIndexError:
    """Tests for MarketIndexError."""

    def test_creation(self) -> None:
        err = MarketIndexError("$.plugins[0].name", "duplicates pkg")
        assert err.path == "$.plugins[0].name"
        assert err.reason == "duplicates pkg"
        assert "$.plugins[0].name" in str(err)
        assert "duplicates pkg" in str(err)


class TestMarketIndexMetadata:
    """Tests for MarketIndexMetadata."""

    def test_empty(self) -> None:
        meta = MarketIndexMetadata()
        assert meta.categories == {}
        assert meta.labels == {}
        assert meta.sort_keys == {}

    def test_with_data(self) -> None:
        meta = MarketIndexMetadata(
            categories={"cat1": {"title": {"en": "Category 1"}}},
            labels={"lbl1": {"title": {"en": "Label 1"}}},
            sort_keys={"sk1": {"title": {"en": "Sort 1"}, "order": "asc"}},
        )
        assert "cat1" in meta.categories
        assert "lbl1" in meta.labels
        assert "sk1" in meta.sort_keys


class TestMarketDeclaration:
    """Tests for MarketDeclaration."""

    def test_creation(self) -> None:
        decl = MarketDeclaration(title={"en": "Title", "zh": "标题"})
        assert decl.title == {"en": "Title", "zh": "标题"}


class TestMarketSortKeyDeclaration:
    """Tests for MarketSortKeyDeclaration."""

    def test_valid_orders(self) -> None:
        decl = MarketSortKeyDeclaration(
            title={"en": "Sort"},
            order="asc",
        )
        assert decl.order == "asc"

        decl2 = MarketSortKeyDeclaration(
            title={"en": "Sort"},
            order="desc",
        )
        assert decl2.order == "desc"

    def test_invalid_order(self) -> None:
        with pytest.raises(ValueError):
            MarketSortKeyDeclaration(title={"en": "Sort"}, order="invalid")


class TestMarketPluginMetadata:
    """Tests for MarketPluginMetadata."""

    def test_defaults(self) -> None:
        meta = MarketPluginMetadata()
        assert meta.category is None
        assert meta.keywords == []
        assert meta.labels == []
        assert meta.sort_keys == {}

    def test_with_data(self) -> None:
        meta = MarketPluginMetadata(
            category="cat1",
            keywords=["kw1", "kw2"],
            labels=["lbl1"],
            sort_keys={"sk1": 1.0},
        )
        assert meta.category == "cat1"
        assert meta.keywords == ["kw1", "kw2"]
        assert meta.labels == ["lbl1"]
        assert meta.sort_keys == {"sk1": 1.0}


class TestMarketSource:
    """Tests for MarketSource."""

    def test_npm(self) -> None:
        src = MarketSource(type="npm", registry="https://registry.npmjs.org")
        assert src.type == "npm"
        assert src.registry == "https://registry.npmjs.org"
        assert src.url is None
        assert src.commit is None

    def test_git(self) -> None:
        src = MarketSource(
            type="git",
            url="https://github.com/user/repo",
            commit="a" * 40,
            path="packages/pkg",
        )
        assert src.type == "git"
        assert src.url == "https://github.com/user/repo"
        assert src.commit == "a" * 40
        assert src.path == "packages/pkg"


class TestMarketRepository:
    """Tests for MarketRepository."""

    def test_creation(self) -> None:
        repo = MarketRepository(
            url="https://github.com/user/repo",
            directory="packages/pkg",
        )
        assert repo.url == "https://github.com/user/repo"
        assert repo.directory == "packages/pkg"


class TestMarketEngines:
    """Tests for MarketEngines."""

    def test_creation(self) -> None:
        eng = MarketEngines(deepspace=">=1.0.0")
        assert eng.deepspace == ">=1.0.0"

    def test_defaults(self) -> None:
        eng = MarketEngines()
        assert eng.deepspace is None


class TestMarketVersion:
    """Tests for MarketVersion."""

    def test_creation(self) -> None:
        ver = MarketVersion(
            version="1.2.3",
            publish_timestamp=1234567890,
            source={"type": "npm"},
        )
        assert ver.version == "1.2.3"
        assert ver.publish_timestamp == 1234567890


class TestMarketPlugin:
    """Tests for MarketPlugin."""

    def test_minimal(self) -> None:
        plugin = MarketPlugin(
            name="test-pkg",
            versions=[{"version": "1.0.0", "publishTimestamp": 123, "source": {"type": "npm"}}],
        )
        assert plugin.name == "test-pkg"
        assert len(plugin.versions) == 1


class TestMarketIndex:
    """Tests for MarketIndex."""

    def test_creation(self) -> None:
        idx = MarketIndex(
            format=1,
            title={"en": "Test Index"},
            plugins=[],
        )
        assert idx.format == 1
        assert idx.title == {"en": "Test Index"}
        assert idx.plugins == []


class TestMarketInstallTargetParsed:
    """Tests for MarketInstallTarget."""

    def test_npm(self) -> None:
        target = MarketInstallTarget(
            spec="pkg@1.0.0",
            registry="https://registry.npmjs.org",
        )
        assert target.spec == "pkg@1.0.0"
        assert target.registry == "https://registry.npmjs.org"

    def test_git(self) -> None:
        target = MarketInstallTarget(
            spec="git+https://github.com/user/repo.git#abc123",
        )
        assert target.spec.startswith("git+")
        assert target.registry is None


class TestParseMarketIndex:
    """Tests for parse_market_index."""

    def test_valid_index(self) -> None:
        index_data = {
            "format": 1,
            "title": {"en": "Test Index"},
            "metadata": {
                "categories": {"cat1": {"title": {"en": "Category 1"}}},
                "labels": {},
                "sortKeys": {},
            },
            "plugins": [
                {
                    "name": "test-pkg",
                    "versions": [
                        {
                            "version": "1.0.0",
                            "publishTimestamp": 1234567890,
                            "source": {"type": "npm"},
                        }
                    ],
                }
            ],
        }
        index = parse_market_index(index_data, "https://example.com/index.json")
        assert index.format == 1
        assert index.title == {"en": "Test Index"}
        assert len(index.plugins) == 1
        assert index.plugins[0].name == "test-pkg"

    def test_invalid_format(self) -> None:
        with pytest.raises(MarketIndexError) as exc:
            parse_market_index({"format": 2}, "https://example.com/index.json")
        assert "$.format" in exc.value.path

    def test_missing_title(self) -> None:
        with pytest.raises(MarketIndexError):
            parse_market_index({"format": 1}, "https://example.com/index.json")

    def test_invalid_plugin_name(self) -> None:
        data = {
            "format": 1,
            "title": {"en": "Test"},
            "metadata": {},
            "plugins": [
                {
                    "name": "INVALID",
                    "versions": [
                        {"version": "1.0.0", "publishTimestamp": 123, "source": {"type": "npm"}}
                    ],
                }
            ],
        }
        with pytest.raises(MarketIndexError) as exc:
            parse_market_index(data, "https://example.com/index.json")
        assert "$.plugins[0].name" in exc.value.path

    def test_invalid_version_format(self) -> None:
        data = {
            "format": 1,
            "title": {"en": "Test"},
            "metadata": {},
            "plugins": [
                {
                    "name": "test",
                    "versions": [
                        {"version": "1.0", "publishTimestamp": 123, "source": {"type": "npm"}},
                    ],
                }
            ],
        }
        with pytest.raises(MarketIndexError) as exc:
            parse_market_index(data, "https://example.com/index.json")
        assert "$.plugins[0].versions[0].version" in exc.value.path

    def test_invalid_publish_timestamp(self) -> None:
        data = {
            "format": 1,
            "title": {"en": "Test"},
            "metadata": {},
            "plugins": [
                {
                    "name": "test",
                    "versions": [
                        {"version": "1.0.0", "publishTimestamp": -1, "source": {"type": "npm"}},
                    ],
                }
            ],
        }
        with pytest.raises(MarketIndexError) as exc:
            parse_market_index(data, "https://example.com/index.json")
        assert "$.plugins[0].versions[0].publishTimestamp" in exc.value.path

    def test_duplicate_plugin_names(self) -> None:
        data = {
            "format": 1,
            "title": {"en": "Test"},
            "metadata": {},
            "plugins": [
                {
                    "name": "dup",
                    "versions": [
                        {"version": "1.0.0", "publishTimestamp": 123, "source": {"type": "npm"}}
                    ],
                },
                {
                    "name": "dup",
                    "versions": [
                        {"version": "2.0.0", "publishTimestamp": 456, "source": {"type": "npm"}}
                    ],
                },
            ],
        }
        with pytest.raises(MarketIndexError) as exc:
            parse_market_index(data, "https://example.com/index.json")
        assert "$.plugins[1].name" in exc.value.path
        assert "duplicates" in exc.value.reason

    def test_version_sorting(self) -> None:
        data = {
            "format": 1,
            "title": {"en": "Test"},
            "metadata": {},
            "plugins": [
                {
                    "name": "test",
                    "versions": [
                        {"version": "1.0.0", "publishTimestamp": 100, "source": {"type": "npm"}},
                        {"version": "2.0.0", "publishTimestamp": 200, "source": {"type": "npm"}},
                        {"version": "1.5.0", "publishTimestamp": 150, "source": {"type": "npm"}},
                    ],
                }
            ],
        }
        index = parse_market_index(data, "https://example.com/index.json")
        versions = index.plugins[0].versions
        # Should be sorted by SemVer descending
        assert versions[0].version == "2.0.0"
        assert versions[1].version == "1.5.0"
        assert versions[2].version == "1.0.0"


class TestMarketInstallTarget:
    """Tests for market_install_target."""

    def test_npm(self) -> None:
        version = {"version": "1.0.0", "publishTimestamp": 123, "source": {"type": "npm"}}
        target = market_install_target("test-pkg", version)
        assert target.spec == "test-pkg@1.0.0"
        assert target.registry is None

    def test_npm_with_registry(self) -> None:
        version = {
            "version": "1.0.0",
            "publishTimestamp": 123,
            "source": {"type": "npm", "registry": "https://registry.example.com"},
        }
        target = market_install_target("test-pkg", version)
        assert target.spec == "test-pkg@1.0.0"
        assert target.registry == "https://registry.example.com"

    def test_git(self) -> None:
        version = {
            "version": "1.0.0",
            "publishTimestamp": 123,
            "source": {
                "type": "git",
                "url": "https://github.com/user/repo",
                "commit": "a" * 40,
            },
        }
        target = market_install_target("test-pkg", version)
        assert target.spec.startswith("git+https://github.com/user/repo.git#")
        assert "a" * 40 in target.spec

    def test_git_with_path(self) -> None:
        version = {
            "version": "1.0.0",
            "publishTimestamp": 123,
            "source": {
                "type": "git",
                "url": "https://github.com/user/repo",
                "commit": "a" * 40,
                "path": "packages/pkg",
            },
        }
        target = market_install_target("test-pkg", version)
        assert "&path:/" in target.spec


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
