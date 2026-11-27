"""Tests for local file reference provider."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from app.deepspace.services.file_reference_local import (
    FILE_REF_PATTERN,
    INDEX_REFRESH_INTERVAL,
    MAX_INDEXED_FILES,
    SKIP_DIRS,
    SKIP_EXTS,
    FileReferenceEntry,
    FileReferenceIndex,
    FileReferenceLocalService,
    get_file_reference_service,
    get_workspace_files,
    init_file_reference_service,
    parse_file_refs,
    resolve_file_refs_in_text,
)


class TestFileReferenceEntry:
    """Tests for the FileReferenceEntry dataclass."""

    def test_creation(self) -> None:
        entry = FileReferenceEntry(
            path="src/main.py",
            size=1024,
            mtime=1234567890.0,
            extension=".py",
        )
        assert entry.path == "src/main.py"
        assert entry.size == 1024
        assert entry.mtime == 1234567890.0
        assert entry.extension == ".py"


class TestFileReferenceIndex:
    """Tests for the FileReferenceIndex class."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)

        # Create test files
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "main.py").write_text("print('hello')")
        (self.workspace / "src" / "utils.py").write_text("def helper(): pass")
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_main.py").write_text("def test(): pass")
        (self.workspace / "README.md").write_text("# Project")
        (self.workspace / "config.json").write_text('{"key": "value"}')
        (self.workspace / "package.json").write_text('{"name": "test"}')

        # Create a .git directory (should be skipped)
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config").write_text("[core]")

        # Create a __pycache__ directory (should be skipped)
        (self.workspace / "src" / "__pycache__").mkdir()
        (self.workspace / "src" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"compiled")

        self.index = FileReferenceIndex(self.workspace)

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initialization(self) -> None:
        assert self.index.workspace_root == self.workspace.resolve()
        assert len(self.index._entries) == 0
        assert self.index._last_refresh == 0.0

    def test_should_skip_dir_git(self) -> None:
        assert self.index._should_skip_dir(".git") is True
        assert self.index._should_skip_dir(".github") is True  # starts with .

    def test_should_skip_dir_pycache(self) -> None:
        assert self.index._should_skip_dir("__pycache__") is True

    def test_should_skip_dir_node_modules(self) -> None:
        assert self.index._should_skip_dir("node_modules") is True

    def test_should_skip_dir_not_skipped(self) -> None:
        assert self.index._should_skip_dir("src") is False
        assert self.index._should_skip_dir("tests") is False
        assert self.index._should_skip_dir("my_project") is False

    def test_should_skip_file_hidden(self) -> None:
        assert self.index._should_skip_file(".gitignore") is True
        assert self.index._should_skip_file(".env") is True

    def test_should_skip_file_extensions(self) -> None:
        assert self.index._should_skip_file("test.pyc") is True
        assert self.index._should_skip_file("test.jpg") is True
        assert self.index._should_skip_file("test.png") is True
        assert self.index._should_skip_file("test.zip") is True
        assert self.index._should_skip_file("test.pdf") is True
        assert self.index._should_skip_file("test.log") is True

    def test_should_skip_file_not_skipped(self) -> None:
        assert self.index._should_skip_file("main.py") is False
        assert self.index._should_skip_file("test.js") is False
        assert self.index._should_skip_file("README.md") is False
        assert self.index._should_skip_file("config.json") is False

    def test_scan_workspace(self) -> None:
        entries = self.index._scan_workspace()

        # Should find the test files we created
        paths = {e.path for e in entries}
        assert "src/main.py" in paths
        assert "src/utils.py" in paths
        assert "tests/test_main.py" in paths
        assert "README.md" in paths
        assert "config.json" in paths
        assert "package.json" in paths

        # Should NOT find skipped directories/files
        assert "src/__pycache__/main.cpython-312.pyc" not in paths
        assert ".git/config" not in paths

        # All entries should have correct types
        for entry in entries:
            assert isinstance(entry, FileReferenceEntry)
            assert isinstance(entry.path, str)
            assert isinstance(entry.size, int)
            assert isinstance(entry.mtime, float)
            assert isinstance(entry.extension, str)

    def test_refresh(self) -> None:
        self.index.refresh()
        assert len(self.index._entries) > 0
        assert self.index._last_refresh > 0
        assert len(self.index._path_to_entry) == len(self.index._entries)

    def test_refresh_not_forced_within_interval(self) -> None:
        self.index.refresh()
        first_refresh = self.index._last_refresh

        # Refresh again immediately - should not change timestamp
        time.sleep(0.01)
        self.index.refresh()
        assert self.index._last_refresh == first_refresh

    def test_refresh_forced(self) -> None:
        self.index.refresh()
        first_refresh = self.index._last_refresh

        time.sleep(0.01)
        self.index.refresh(force=True)
        assert self.index._last_refresh > first_refresh

    def test_search_exact_match(self) -> None:
        self.index.refresh()
        results = self.index.search("README.md")
        assert len(results) == 1
        assert results[0].path == "README.md"

    def test_search_partial_match(self) -> None:
        self.index.refresh()
        results = self.index.search("main")
        paths = [e.path for e in results]
        assert "src/main.py" in paths

    def test_search_fuzzy_match(self) -> None:
        self.index.refresh()
        results = self.index.search("test main")
        paths = [e.path for e in results]
        # Should match test_main.py and main.py
        assert "tests/test_main.py" in paths or "src/main.py" in paths

    def test_search_empty_query(self) -> None:
        self.index.refresh()
        results = self.index.search("", limit=10)
        # Should return all entries up to limit
        assert len(results) <= 10
        assert len(results) > 0

    def test_search_limit(self) -> None:
        self.index.refresh()
        results = self.index.search("", limit=2)
        assert len(results) == 2

    def test_search_no_matches(self) -> None:
        self.index.refresh()
        results = self.index.search("nonexistentfile12345")
        assert len(results) == 0

    def test_get_by_path_exact(self) -> None:
        self.index.refresh()
        entry = self.index.get_by_path("README.md")
        assert entry is not None
        assert entry.path == "README.md"

    def test_get_by_path_not_found(self) -> None:
        self.index.refresh()
        entry = self.index.get_by_path("nonexistent.txt")
        assert entry is None

    def test_get_all(self) -> None:
        self.index.refresh()
        entries = self.index.get_all()
        assert len(entries) > 0
        assert len(entries) == len(self.index._entries)

    def test_case_insensitive_search(self) -> None:
        self.index.refresh()
        results = self.index.search("readme.md")
        assert len(results) == 1
        assert results[0].path == "README.md"

    def test_multi_term_search(self) -> None:
        self.index.refresh()
        results = self.index.search("test main")
        # Should match files with both "test" and "main"
        paths = [e.path for e in results]
        assert "tests/test_main.py" in paths or "src/main.py" in paths

    def test_extension_boost(self) -> None:
        self.index.refresh()
        # Create a .py file and a .txt file with similar names
        (self.workspace / "test_file.py").write_text("# python")
        (self.workspace / "test_file.txt").write_text("text")
        self.index.refresh(force=True)

        results = self.index.search("test_file")
        # .py should rank higher due to extension boost
        py_index = next((i for i, e in enumerate(results) if e.path == "test_file.py"), None)
        txt_index = next((i for i, e in enumerate(results) if e.path == "test_file.txt"), None)
        assert py_index is not None
        if txt_index is not None:
            assert py_index < txt_index  # .py should come first


class TestFileReferenceLocalService:
    """Tests for the FileReferenceLocalService class."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "main.py").write_text("print('hello')")
        (self.workspace / "README.md").write_text("# Project")
        self.service = FileReferenceLocalService(self.workspace)

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initialization_with_workspace(self) -> None:
        service = FileReferenceLocalService(self.workspace)
        assert service.is_available() is True

    def test_initialization_without_workspace(self) -> None:
        service = FileReferenceLocalService()
        assert service.is_available() is False

    def test_set_workspace_root(self) -> None:
        service = FileReferenceLocalService()
        assert service.is_available() is False
        service.set_workspace_root(self.workspace)
        assert service.is_available() is True

    def test_search_files(self) -> None:
        results = self.service.search_files("main")
        assert len(results) == 1
        assert results[0]["path"] == "src/main.py"

    def test_search_files_no_index(self) -> None:
        service = FileReferenceLocalService()
        results = service.search_files("main")
        assert results == []

    def test_search_files_limit(self) -> None:
        results = self.service.search_files("", limit=1)
        assert len(results) == 1

    def test_resolve_file_ref_exact(self) -> None:
        result = self.service.resolve_file_ref("@README.md")
        assert result is not None
        assert result["path"] == "README.md"
        assert result["resolved"] is True

    def test_resolve_file_ref_without_at_prefix(self) -> None:
        result = self.service.resolve_file_ref("README.md")
        assert result is not None
        assert result["path"] == "README.md"

    def test_resolve_file_ref_fuzzy(self) -> None:
        result = self.service.resolve_file_ref("@main")
        assert result is not None
        assert result["resolved"] is True
        assert result.get("fuzzy") is True
        assert result["path"] == "src/main.py"

    def test_resolve_file_ref_not_found(self) -> None:
        result = self.service.resolve_file_ref("@nonexistent.txt")
        assert result is None

    def test_resolve_file_ref_no_index(self) -> None:
        service = FileReferenceLocalService()
        result = service.resolve_file_ref("@README.md")
        assert result is None

    def test_get_all_files(self) -> None:
        results = self.service.get_all_files(limit=10)
        assert len(results) >= 2  # src/main.py and README.md

    def test_get_all_files_limit(self) -> None:
        results = self.service.get_all_files(limit=1)
        assert len(results) == 1

    def test_get_all_files_no_index(self) -> None:
        service = FileReferenceLocalService()
        results = service.get_all_files()
        assert results == []

    def test_refresh_index(self) -> None:
        # Should not raise
        self.service.refresh_index()


class TestGlobalFunctions:
    """Tests for global functions."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "main.py").write_text("print('hello')")
        (self.workspace / "README.md").write_text("# Project")

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_file_reference_service(self) -> None:
        service = init_file_reference_service(self.workspace)
        assert service.is_available() is True
        assert service == get_file_reference_service()

    def test_parse_file_refs(self) -> None:
        text = "Please look at @README.md and @src/main.py"
        refs = parse_file_refs(text)
        assert refs == ["README.md", "src/main.py"]

    def test_parse_file_refs_multiple(self) -> None:
        text = "@file1 @file2 @file3"
        refs = parse_file_refs(text)
        assert refs == ["file1", "file2", "file3"]

    def test_parse_file_refs_no_matches(self) -> None:
        text = "No file references here"
        refs = parse_file_refs(text)
        assert refs == []

    def test_parse_file_refs_edge_cases(self) -> None:
        # The pattern matches @ followed by non-whitespace, non-punctuation
        # So email@example.com will match "example.com"
        text = "email@example.com and @README.md"
        refs = parse_file_refs(text)
        assert refs == ["example.com", "README.md"]

    def test_resolve_file_refs_in_text(self) -> None:
        text = "See @README.md and @src/main.py"
        results = resolve_file_refs_in_text(text, self.workspace)

        assert len(results) == 2
        refs = {r["ref"] for r in results}
        assert "@README.md" in refs
        assert "@src/main.py" in refs

        # Check resolved ones
        for r in results:
            if r["ref"] == "@README.md":
                assert r["resolved"] is True
                assert r["path"] == "README.md"
            if r["ref"] == "@src/main.py":
                assert r["resolved"] is True
                assert r["path"] == "src/main.py"

    def test_resolve_file_refs_in_text_unresolved(self) -> None:
        text = "See @nonexistent.txt"
        results = resolve_file_refs_in_text(text, self.workspace)

        assert len(results) == 1
        assert results[0]["ref"] == "@nonexistent.txt"
        assert results[0]["resolved"] is False

    def test_get_workspace_files(self) -> None:
        results = get_workspace_files(self.workspace, "main", limit=10)
        assert len(results) >= 1
        assert results[0]["path"] == "src/main.py"

    def test_get_workspace_files_empty_query(self) -> None:
        results = get_workspace_files(self.workspace, "", limit=10)
        assert len(results) >= 2


class TestConstants:
    """Tests for module constants."""

    def test_max_indexed_files(self) -> None:
        assert MAX_INDEXED_FILES == 10_000

    def test_index_refresh_interval(self) -> None:
        assert INDEX_REFRESH_INTERVAL == 300

    def test_skip_dirs_contains_common(self) -> None:
        assert ".git" in SKIP_DIRS
        assert "__pycache__" in SKIP_DIRS
        assert "node_modules" in SKIP_DIRS
        assert "venv" in SKIP_DIRS
        assert "build" in SKIP_DIRS
        assert "dist" in SKIP_DIRS

    def test_skip_exts_contains_common(self) -> None:
        assert ".pyc" in SKIP_EXTS
        assert ".jpg" in SKIP_EXTS
        assert ".png" in SKIP_EXTS
        assert ".pdf" in SKIP_EXTS
        assert ".zip" in SKIP_EXTS
        assert ".log" in SKIP_EXTS

    def test_file_ref_pattern(self) -> None:
        text = "See @README.md and @src/main.py"
        matches = FILE_REF_PATTERN.findall(text)
        assert matches == ["README.md", "src/main.py"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
