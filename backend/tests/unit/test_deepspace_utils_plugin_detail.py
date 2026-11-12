"""Tests for plugin_detail utilities."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.deepspace.utils.plugin_detail import (
    NPM_FILE_SOURCES,
    README_NAMES,
    DetailFailure,
    DetailFailureReason,
    DetailImage,
    DetailImageResponse,
    PackageDocument,
    RemoteFileOptions,
    encode_path,
    join_path,
    language_chain,
    local_files,
    markdown_of,
    readme_by_name,
    readme_name,
)


class TestConstants:
    """Tests for constants."""

    def test_readme_names(self) -> None:
        assert "README.md" in README_NAMES
        assert "readme.md" in README_NAMES
        assert len(README_NAMES) > 5

    def test_npm_file_sources(self) -> None:
        assert "unpkg" in NPM_FILE_SOURCES
        assert "jsdelivr" in NPM_FILE_SOURCES
        assert "github" in NPM_FILE_SOURCES


class TestLanguageChainFallbackVariants:
    """Tests for language_chain."""

    def test_primary_only(self) -> None:
        chain = language_chain("en")
        assert chain == ["en"]

    def test_with_generic(self) -> None:
        chain = language_chain("zh-CN")
        assert chain == ["zh-CN", "zh", "en"]

    def test_with_fallbacks(self) -> None:
        chain = language_chain("zh-CN", ["ja", "ko"])
        assert chain == ["zh-CN", "zh", "ja", "ko", "en"]

    def test_en_not_duplicated(self) -> None:
        chain = language_chain("en", ["en", "fr"])
        assert chain.count("en") == 1


class TestREADMESelection:
    """Tests for README selection."""

    def test_readme_name_found(self) -> None:
        files = {"README.md": None, "src/main.py": None}
        assert readme_name(files) == "README.md"

    def test_readme_name_case_insensitive(self) -> None:
        files = {"Readme.md": None}
        assert readme_name(files) == "Readme.md"

    def test_readme_name_not_found(self) -> None:
        files = {"src/main.py": None}
        assert readme_name(files) is None

    def test_readme_by_name(self) -> None:
        files = {"README.md": "# Hello", "src/main.py": "print()"}
        assert readme_by_name(files, "README.md") == "# Hello"
        assert readme_by_name(files, "missing.md") is None


class TestPathEncodingSimpleVariants:
    """Tests for path encoding."""

    def test_encode_path(self) -> None:
        assert encode_path("simple/path") == "simple/path"
        assert encode_path("path with spaces") == "path%20with%20spaces"
        assert encode_path("path/with/unicode/文件") == "path/with/unicode/%E6%96%87%E4%BB%B6"

    def test_join_path(self) -> None:
        assert (
            join_path("https://example.com/", "path", "to", "file")
            == "https://example.com/path/to/file"
        )
        assert join_path("https://example.com", "path") == "https://example.com/path"


class TestLocalFilesBasicContent:
    """Tests for local_files."""

    def test_local_files_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "file.txt").write_text("hello")
            Path(tmp, "subdir").mkdir()
            Path(tmp, "subdir", "nested.txt").write_text("nested")

            files = local_files(tmp)
            assert len(files) == 2
            paths = {f.path for f in files}
            assert "file.txt" in paths
            assert "subdir/nested.txt" in paths

    def test_local_files_skips_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".hidden").write_text("secret")
            Path(tmp, "visible.txt").write_text("public")

            files = local_files(tmp)
            paths = {f.path for f in files}
            assert "visible.txt" in paths
            assert ".hidden" not in paths

    def test_local_files_skips_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package-lock.json").write_text("{}")
            Path(tmp, "yarn.lock").write_text("lock")
            Path(tmp, "pnpm-lock.yaml").write_text("yaml")
            Path(tmp, "real.txt").write_text("content")

            files = local_files(tmp)
            paths = {f.path for f in files}
            assert "real.txt" in paths
            assert "package-lock.json" not in paths
            assert "yarn.lock" not in paths
            assert "pnpm-lock.yaml" not in paths

    def test_local_files_max_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "small.txt").write_text("x" * 100)
            Path(tmp, "large.txt").write_text("y" * 10000)

            files = local_files(tmp, max_size=500)
            paths = {f.path for f in files}
            assert "small.txt" in paths
            assert "large.txt" not in paths

    def test_local_files_nonexistent(self) -> None:
        files = local_files("/nonexistent/path")
        assert files == []


class TestLanguageChain:
    """Tests for language_chain."""

    def test_basic(self) -> None:
        assert language_chain("en") == ["en"]
        assert language_chain("zh-CN") == ["zh-CN", "zh", "en"]
        assert language_chain("fr-FR") == ["fr-FR", "fr", "en"]

    def test_with_fallbacks(self) -> None:
        assert language_chain("de", ["fr", "es"]) == ["de", "fr", "es", "en"]

    def test_no_duplicates(self) -> None:
        chain = language_chain("en", ["en", "fr"])
        assert chain.count("en") == 1


class TestPathEncoding:
    """Tests for path encoding."""

    def test_encode_path(self) -> None:
        assert encode_path("simple") == "simple"
        assert encode_path("path with spaces") == "path%20with%20spaces"
        assert encode_path("a/b/c") == "a/b/c"

    def test_join_path(self) -> None:
        assert join_path("https://example.com", "a", "b") == "https://example.com/a/b"
        assert join_path("https://example.com/", "a") == "https://example.com/a"


class TestLocalFilesNestedPaths:
    """Tests for local_files."""

    def test_reads_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test.txt").write_text("content")
            files = local_files(tmp)
            assert len(files) == 1
            assert files[0].path == "test.txt"
            assert files[0].content == "content"
            assert files[0].source == "local"

    def test_skips_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".gitignore").write_text("ignored")
            Path(tmp, "file.txt").write_text("visible")
            files = local_files(tmp)
            paths = {f.path for f in files}
            assert "file.txt" in paths

    def test_respects_max_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "small.txt").write_text("x" * 100)
            Path(tmp, "large.txt").write_text("y" * 10000)
            files = local_files(tmp, max_size=500)
            paths = {f.path for f in files}
            assert "small.txt" in paths
            assert "large.txt" not in paths


class TestPackageDocumentMetadata:
    """Tests for PackageDocument."""

    def test_creation(self) -> None:
        doc = PackageDocument(
            content="# Hello",
            content_type="markdown",
            source="readme",
            language="en",
        )
        assert doc.content == "# Hello"
        assert doc.content_type == "markdown"
        assert doc.source == "readme"
        assert doc.language == "en"

    def test_with_images(self) -> None:
        doc = PackageDocument(
            content="# Hello",
            content_type="markdown",
            source="readme",
            language="en",
            images={"logo.png": "data:image/png;base64,..."},
        )
        assert "logo.png" in doc.images


class TestMarkdownOfFormats:
    """Tests for markdown_of."""

    def test_markdown_document(self) -> None:
        doc = PackageDocument(
            content="# Hello",
            content_type="markdown",
            source="readme",
            language="en",
        )
        assert markdown_of(doc) == "# Hello"

    def test_text_document(self) -> None:
        doc = PackageDocument(
            content="plain text",
            content_type="text",
            source="readme",
            language="en",
        )
        assert markdown_of(doc) == "plain text"

    def test_html_document(self) -> None:
        doc = PackageDocument(
            content="<h1>Hello</h1><p>World</p>",
            content_type="html",
            source="readme",
            language="en",
        )
        md = markdown_of(doc)
        assert "# Hello" in md
        assert "World" in md


class TestDetailFailureFormatting:
    """Tests for DetailFailure."""

    def test_creation(self) -> None:
        failure = DetailFailure(
            reason=DetailFailureReason.NOT_FOUND,
            message="Package not found",
            package_name="test-pkg",
        )
        assert failure.reason == DetailFailureReason.NOT_FOUND
        assert failure.message == "Package not found"
        assert failure.package_name == "test-pkg"

    def test_str_representation(self) -> None:
        failure = DetailFailure(
            reason=DetailFailureReason.NETWORK_ERROR,
            message="Connection timeout",
        )
        assert "DetailFailure(network_error)" in str(failure)
        assert "Connection timeout" in str(failure)


class TestDetailImage:
    """Tests for DetailImage."""

    def test_creation(self) -> None:
        img = DetailImage(
            data=b"fake image data",
            mime_type="image/png",
            source_path="logo.png",
            alt_text="Logo",
        )
        assert img.data == b"fake image data"
        assert img.mime_type == "image/png"
        assert img.source_path == "logo.png"
        assert img.alt_text == "Logo"


class TestDetailImageResponse:
    """Tests for DetailImageResponse."""

    def test_creation(self) -> None:
        img = DetailImage(
            data=b"data",
            mime_type="image/png",
            source_path="logo.png",
        )
        resp = DetailImageResponse(image=img)
        assert resp.image == img
        assert resp.cache_control == "public, max-age=3600"


class TestRemoteFileOptions:
    """Tests for RemoteFileOptions."""

    def test_defaults(self) -> None:
        opts = RemoteFileOptions()
        assert opts.max_size == 10 * 1024 * 1024
        assert opts.timeout == 30.0
        assert opts.headers == {}
        assert opts.follow_redirects is True

    def test_custom(self) -> None:
        opts = RemoteFileOptions(max_size=1000, timeout=5.0)
        assert opts.max_size == 1000
        assert opts.timeout == 5.0


class TestEncodePath:
    """Tests for encode_path."""

    def test_simple(self) -> None:
        assert encode_path("simple") == "simple"
        assert encode_path("path/with/slashes") == "path/with/slashes"

    def test_spaces(self) -> None:
        assert encode_path("with spaces") == "with%20spaces"

    def test_unicode(self) -> None:
        assert encode_path("文件") == "%E6%96%87%E4%BB%B6"


class TestJoinPath:
    """Tests for join_path."""

    def test_absolute(self) -> None:
        assert join_path("https://example.com", "a", "b") == "https://example.com/a/b"

    def test_with_trailing_slash(self) -> None:
        assert join_path("https://example.com/", "a") == "https://example.com/a"


class TestLocalFiles:
    """Tests for local_files function."""

    def test_reads_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test.txt").write_text("content")
            files = local_files(tmp)
            assert len(files) == 1
            assert files[0].path == "test.txt"
            assert files[0].content == "content"
            assert files[0].source == "local"

    def test_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "subdir").mkdir()
            Path(tmp, "subdir", "nested.txt").write_text("nested")
            files = local_files(tmp)
            paths = {f.path for f in files}
            assert "subdir/nested.txt" in paths


class TestReadmeFunctions:
    """Tests for README-related functions."""

    def test_readme_names_constant(self) -> None:
        assert "README.md" in README_NAMES
        assert len(README_NAMES) > 5

    def test_readme_name_priority(self) -> None:
        files = {"readme.md": None, "README.md": None, "README.txt": None}
        assert readme_name(files) == "README.md"  # First in list

    def test_readme_by_name(self) -> None:
        files = {"README.md": "# Title", "other.txt": "content"}
        assert readme_by_name(files, "README.md") == "# Title"
        assert readme_by_name(files, "missing.md") is None


class TestPackageDocument:
    """Tests for PackageDocument."""

    def test_creation(self) -> None:
        doc = PackageDocument(
            content="# Test",
            content_type="markdown",
            source="readme",
            language="en",
        )
        assert doc.content == "# Test"
        assert doc.source == "readme"

    def test_with_metadata(self) -> None:
        doc = PackageDocument(
            content="# Test",
            content_type="markdown",
            source="readme",
            language="en",
            metadata={"version": "1.0.0"},
        )
        assert doc.metadata["version"] == "1.0.0"


class TestMarkdownOf:
    """Tests for markdown_of."""

    def test_markdown(self) -> None:
        doc = PackageDocument(
            content="# Hello",
            content_type="markdown",
            source="readme",
            language="en",
        )
        assert markdown_of(doc) == "# Hello"

    def test_text(self) -> None:
        doc = PackageDocument(
            content="plain text",
            content_type="text",
            source="readme",
            language="en",
        )
        assert markdown_of(doc) == "plain text"

    def test_html_conversion(self) -> None:
        doc = PackageDocument(
            content="<h1>Title</h1><p>Content</p>",
            content_type="html",
            source="readme",
            language="en",
        )
        md = markdown_of(doc)
        assert "# Title" in md
        assert "Content" in md


class TestDetailFailure:
    """Tests for DetailFailure."""

    def test_creation(self) -> None:
        failure = DetailFailure(
            reason=DetailFailureReason.NOT_FOUND,
            message="Not found",
            package_name="test",
        )
        assert failure.reason == DetailFailureReason.NOT_FOUND
        assert failure.message == "Not found"
        assert failure.package_name == "test"

    def test_str(self) -> None:
        failure = DetailFailure(
            reason=DetailFailureReason.NETWORK_ERROR,
            message="Timeout",
        )
        assert "network_error" in str(failure).lower()
        assert "Timeout" in str(failure)


class TestImageTypes:
    """Tests for image-related types."""

    def test_detail_image(self) -> None:
        img = DetailImage(
            data=b"fake",
            mime_type="image/png",
            source_path="logo.png",
            alt_text="Logo",
        )
        assert img.mime_type == "image/png"
        assert img.alt_text == "Logo"

    def test_detail_image_response(self) -> None:
        img = DetailImage(data=b"x", mime_type="image/png", source_path="x.png")
        resp = DetailImageResponse(image=img)
        assert resp.cache_control == "public, max-age=3600"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
