"""Tests for code_language utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.deepspace.utils.code_language import (
    CODE_HIGHLIGHT_EXTENSIONS,
    COMMON_CONFIG_EXTENSIONS,
    COMMON_MARKUP_EXTENSIONS,
    COMMON_SOURCE_EXTENSIONS,
    EXTENSION_TO_LANGUAGE,
    LANGUAGE_EXTENSIONS,
    READ_LANG_BY_EXTENSION,
    SHORT_BY_LANGUAGE,
    build_lsp_extension_map,
    get_all_languages,
    get_language_extensions,
    is_recognized_extension,
    language_for_path,
    read_lang_hint_for_path,
)


class TestLanguageForPath:
    """Tests for language_for_path function."""

    def test_python_files(self) -> None:
        assert language_for_path("main.py") == "python"
        assert language_for_path("script.pyw") == "python"
        assert language_for_path("types.pyi") == "python"
        assert language_for_path("src/main.py") == "python"
        assert language_for_path("C:\\src\\main.py") == "python"

    def test_javascript_typescript(self) -> None:
        assert language_for_path("app.js") == "javascript"
        assert language_for_path("component.jsx") == "javascript"
        assert language_for_path("main.ts") == "typescript"
        assert language_for_path("component.tsx") == "typescript"
        assert language_for_path("config.mjs") == "javascript"
        assert language_for_path("config.cjs") == "javascript"

    def test_rust_go_cpp(self) -> None:
        assert language_for_path("main.rs") == "rust"
        assert language_for_path("main.go") == "go"
        assert language_for_path("main.cpp") == "cpp"
        assert language_for_path("main.cc") == "cpp"
        assert language_for_path("header.h") == "c"
        assert language_for_path("header.hpp") == "cpp"

    def test_config_files(self) -> None:
        assert language_for_path("config.json") == "json"
        assert language_for_path("config.yaml") == "yaml"
        assert language_for_path("config.yml") == "yaml"
        assert language_for_path("config.toml") == "toml"
        assert language_for_path("config.ini") == "ini"
        assert language_for_path(".env") == "dotenv"
        assert language_for_path("config.env") == "dotenv"

    def test_markup_files(self) -> None:
        assert language_for_path("README.md") == "markdown"
        assert language_for_path("README.markdown") == "markdown"
        assert language_for_path("page.html") == "html"
        assert language_for_path("style.css") == "css"
        assert language_for_path("style.scss") == "scss"

    def test_dotfiles(self) -> None:
        assert language_for_path(".env") == "dotenv"
        assert language_for_path(".gitignore") is None  # Not in table

    def test_extensionless(self) -> None:
        assert language_for_path("Makefile") is None  # Not in table (makefile is)
        assert language_for_path("Dockerfile") is None
        assert language_for_path("README") is None

    def test_path_object(self) -> None:
        assert language_for_path(Path("main.py")) == "python"

    def test_case_insensitive(self) -> None:
        assert language_for_path("MAIN.PY") == "python"
        assert language_for_path("Main.Py") == "python"
        assert language_for_path("CONFIG.JSON") == "json"


class TestReadLangHintForPath:
    """Tests for read_lang_hint_for_path function."""

    def test_standard_short_names(self) -> None:
        assert read_lang_hint_for_path("main.py") == "py"
        assert read_lang_hint_for_path("script.js") == "js"
        assert read_lang_hint_for_path("main.rs") == "rs"
        assert read_lang_hint_for_path("main.go") == "go"
        assert read_lang_hint_for_path("main.java") == "java"

    def test_override_extensions(self) -> None:
        # tsx/jsx keep their own suffix
        assert read_lang_hint_for_path("component.tsx") == "tsx"
        assert read_lang_hint_for_path("component.jsx") == "jsx"
        # tf/tfvars for HCL
        assert read_lang_hint_for_path("main.tf") == "tf"
        assert read_lang_hint_for_path("vars.tfvars") == "tfvars"
        # gradle for groovy
        assert read_lang_hint_for_path("build.gradle") == "gradle"

    def test_languages_where_short_eq_grammar(self) -> None:
        assert read_lang_hint_for_path("main.kt") == "kotlin"
        assert read_lang_hint_for_path("main.swift") == "swift"
        assert read_lang_hint_for_path("main.yaml") == "yaml"
        assert read_lang_hint_for_path("main.json") == "json"

    def test_unknown_returns_none(self) -> None:
        assert read_lang_hint_for_path("unknown.xyz") is None
        assert read_lang_hint_for_path("README") is None


class TestExtensionMaps:
    """Tests for extension/language maps."""

    def test_extension_to_language_complete(self) -> None:
        # Every extension in LANGUAGE_EXTENSIONS should be in EXTENSION_TO_LANGUAGE
        for lang, exts in LANGUAGE_EXTENSIONS.items():
            for ext in exts:
                assert EXTENSION_TO_LANGUAGE[ext] == lang

    def test_code_highlight_extensions_sorted(self) -> None:
        assert list(CODE_HIGHLIGHT_EXTENSIONS) == sorted(CODE_HIGHLIGHT_EXTENSIONS)
        # All should be lowercase
        assert all(ext == ext.lower() for ext in CODE_HIGHLIGHT_EXTENSIONS)

    def test_short_by_language_complete(self) -> None:
        for lang in LANGUAGE_EXTENSIONS:
            assert lang in SHORT_BY_LANGUAGE

    def test_read_lang_by_extension_overrides(self) -> None:
        for ext, hint in READ_LANG_BY_EXTENSION.items():
            assert ext in EXTENSION_TO_LANGUAGE
            canonical = EXTENSION_TO_LANGUAGE[ext]
            # Override should differ from standard short name
            assert hint != SHORT_BY_LANGUAGE[canonical] or hint == canonical


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_get_language_extensions(self) -> None:
        exts = get_language_extensions("python")
        assert exts == ("py", "pyw", "pyi")

        exts = get_language_extensions("unknown")
        assert exts == ()

    def test_is_recognized_extension(self) -> None:
        assert is_recognized_extension("py")
        assert is_recognized_extension("PY")
        assert not is_recognized_extension("unknown")

    def test_get_all_languages(self) -> None:
        langs = get_all_languages()
        assert "python" in langs
        assert "typescript" in langs
        assert len(langs) == len(LANGUAGE_EXTENSIONS)
        assert list(langs) == sorted(langs)

    def test_build_lsp_extension_map(self) -> None:
        lsp_map = build_lsp_extension_map()
        assert ".py" in lsp_map
        assert lsp_map[".py"] == "python"
        assert ".rs" in lsp_map
        assert lsp_map[".rs"] == "rust"


class TestCommonExtensionSets:
    """Tests for common extension frozensets."""

    def test_config_extensions(self) -> None:
        assert "json" in COMMON_CONFIG_EXTENSIONS
        assert "yaml" in COMMON_CONFIG_EXTENSIONS
        assert "toml" in COMMON_CONFIG_EXTENSIONS
        assert "tf" in COMMON_CONFIG_EXTENSIONS

    def test_source_extensions(self) -> None:
        assert "py" in COMMON_SOURCE_EXTENSIONS
        assert "rs" in COMMON_SOURCE_EXTENSIONS
        assert "js" in COMMON_SOURCE_EXTENSIONS
        assert "ts" in COMMON_SOURCE_EXTENSIONS
        assert "go" in COMMON_SOURCE_EXTENSIONS

    def test_markup_extensions(self) -> None:
        assert "md" in COMMON_MARKUP_EXTENSIONS
        assert "html" in COMMON_MARKUP_EXTENSIONS
        assert "css" in COMMON_MARKUP_EXTENSIONS
        assert "rst" in COMMON_MARKUP_EXTENSIONS


class TestCompleteness:
    """Tests that the table covers expected languages."""

    def test_core_languages_present(self) -> None:
        required = {
            "python",
            "javascript",
            "typescript",
            "rust",
            "go",
            "cpp",
            "c",
            "java",
            "csharp",
            "kotlin",
            "swift",
            "json",
            "yaml",
            "toml",
            "html",
            "css",
            "markdown",
            "sql",
            "shellscript",
        }
        assert required.issubset(get_all_languages())
        assert "dockerfile" not in get_all_languages()
        assert language_for_path("script.sh") == "shellscript"
        assert language_for_path("script.bash") == "shellscript"
        # Check most are present
        present = set(LANGUAGE_EXTENSIONS.keys())
        for lang in [
            "python",
            "javascript",
            "typescript",
            "rust",
            "go",
            "cpp",
            "c",
            "java",
            "csharp",
            "kotlin",
            "swift",
            "json",
            "yaml",
            "toml",
            "html",
            "css",
            "markdown",
            "sql",
        ]:
            assert lang in present, f"Missing core language: {lang}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
