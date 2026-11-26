"""Tests for Codex-style patch parsing and application."""

from __future__ import annotations

import pytest

from app.deepspace.utils.codex_patch import (
    ApplyResult,
    ParseResult,
    PatchAction,
    PatchFile,
    PatchHunk,
    _find_match,
    _normalize_line_endings,
    apply_hunks,
    apply_patch_to_file,
    parse_patch,
)


class TestPatchAction:
    """Tests for the PatchAction enum."""

    def test_enum_values(self) -> None:
        assert PatchAction.ADD.value == "add"
        assert PatchAction.DELETE.value == "delete"
        assert PatchAction.UPDATE.value == "update"


class TestPatchHunk:
    """Tests for the PatchHunk dataclass."""

    def test_default_creation(self) -> None:
        hunk = PatchHunk()
        assert hunk.old_lines == []
        assert hunk.new_lines == []
        assert hunk.context == ""

    def test_creation_with_values(self) -> None:
        hunk = PatchHunk(
            old_lines=["line1", "line2"],
            new_lines=["line1", "line2 modified"],
            context="@@ -1,2 +1,2 @@",
        )
        assert hunk.old_lines == ["line1", "line2"]
        assert hunk.new_lines == ["line1", "line2 modified"]
        assert hunk.context == "@@ -1,2 +1,2 @@"


class TestPatchFile:
    """Tests for the PatchFile dataclass."""

    def test_add_file(self) -> None:
        file = PatchFile(
            action=PatchAction.ADD,
            path="new_file.py",
            content="print('hello')",
        )
        assert file.action == PatchAction.ADD
        assert file.path == "new_file.py"
        assert file.content == "print('hello')"

    def test_delete_file(self) -> None:
        file = PatchFile(
            action=PatchAction.DELETE,
            path="old_file.py",
        )
        assert file.action == PatchAction.DELETE
        assert file.path == "old_file.py"

    def test_update_file(self) -> None:
        file = PatchFile(
            action=PatchAction.UPDATE,
            path="main.py",
            hunks=[PatchHunk(old_lines=["a"], new_lines=["b"])],
        )
        assert file.action == PatchAction.UPDATE
        assert file.path == "main.py"
        assert len(file.hunks) == 1

    def test_move_file(self) -> None:
        file = PatchFile(
            action=PatchAction.UPDATE,
            path="old_path.py",
            move_to="new_path.py",
        )
        assert file.move_to == "new_path.py"


class TestParseResult:
    """Tests for the ParseResult dataclass."""

    def test_success(self) -> None:
        result = ParseResult(ok=True, files=[PatchFile(action=PatchAction.ADD, path="test.py")])
        assert result.ok is True
        assert len(result.files) == 1
        assert result.error is None

    def test_failure(self) -> None:
        result = ParseResult(ok=False, error="Parse error")
        assert result.ok is False
        assert result.error == "Parse error"
        assert result.files == []


class TestApplyResult:
    """Tests for the ApplyResult dataclass."""

    def test_success(self) -> None:
        result = ApplyResult(ok=True, content="new content")
        assert result.ok is True
        assert result.content == "new content"
        assert result.error is None

    def test_failure(self) -> None:
        result = ApplyResult(ok=False, error="Apply failed")
        assert result.ok is False
        assert result.error == "Apply failed"
        assert result.content == ""


class TestNormalizeLineEndings:
    """Tests for the _normalize_line_endings function."""

    def test_crlf_to_lf(self) -> None:
        text = "line1\r\nline2\r\n"
        result = _normalize_line_endings(text)
        assert result == "line1\nline2\n"

    def test_cr_to_lf(self) -> None:
        text = "line1\rline2\r"
        result = _normalize_line_endings(text)
        assert result == "line1\nline2\n"

    def test_mixed(self) -> None:
        text = "line1\r\nline2\rline3\n"
        result = _normalize_line_endings(text)
        assert result == "line1\nline2\nline3\n"

    def test_no_change(self) -> None:
        text = "line1\nline2\n"
        result = _normalize_line_endings(text)
        assert result == "line1\nline2\n"


class TestParsePatch:
    """Tests for the parse_patch function."""

    def test_empty_patch(self) -> None:
        result = parse_patch("")
        assert result.ok is False
        assert result.error == "Empty patch"

    def test_only_whitespace(self) -> None:
        result = parse_patch("   \n\t\n  ")
        assert result.ok is False
        assert result.error == "Empty patch"

    def test_add_file(self) -> None:
        patch = """*** Begin Patch
*** Add File: new_file.py
+print('hello')
+print('world')
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert len(result.files) == 1
        file = result.files[0]
        assert file.action == PatchAction.ADD
        assert file.path == "new_file.py"
        assert file.content == "print('hello')\nprint('world')\n"

    def test_add_file_empty_content(self) -> None:
        patch = """*** Begin Patch
*** Add File: empty.py
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert result.files[0].content == ""

    def test_delete_file(self) -> None:
        patch = """*** Begin Patch
*** Delete File: old_file.py
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert len(result.files) == 1
        file = result.files[0]
        assert file.action == PatchAction.DELETE
        assert file.path == "old_file.py"

    def test_update_file(self) -> None:
        patch = """*** Begin Patch
*** Update File: main.py
@@ -1,2 +1,2 @@
-old_line
+new_line
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert len(result.files) == 1
        file = result.files[0]
        assert file.action == PatchAction.UPDATE
        assert file.path == "main.py"
        assert len(file.hunks) == 1
        assert file.hunks[0].old_lines == ["old_line"]
        assert file.hunks[0].new_lines == ["new_line"]
        assert file.hunks[0].context == "-1,2 +1,2 @@"

    def test_move_file(self) -> None:
        patch = """*** Begin Patch
*** Update File: old_path.py
*** Move to: new_path.py
@@
-old
+new
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert len(result.files) == 1
        file = result.files[0]
        assert file.move_to == "new_path.py"
        assert file.path == "old_path.py"

    def test_multiple_files(self) -> None:
        patch = """*** Begin Patch
*** Add File: file1.py
+content1
*** Update File: file2.py
@@
-old
+new
*** Delete File: file3.py
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        assert len(result.files) == 3
        assert result.files[0].action == PatchAction.ADD
        assert result.files[1].action == PatchAction.UPDATE
        assert result.files[2].action == PatchAction.DELETE

    def test_update_file_multiple_hunks(self) -> None:
        patch = """*** Begin Patch
*** Update File: main.py
@@ -1,2 +1,2 @@
-old1
+new1
@@ -5,2 +5,2 @@
-old2
+new2
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        file = result.files[0]
        assert len(file.hunks) == 2
        assert file.hunks[0].old_lines == ["old1"]
        assert file.hunks[1].old_lines == ["old2"]

    def test_context_lines(self) -> None:
        patch = """*** Begin Patch
*** Update File: main.py
@@ -10,3 +10,3 @@
 context line
-old line
+new line
 context line
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        hunk = result.files[0].hunks[0]
        assert hunk.old_lines == ["context line", "old line", "context line"]
        assert hunk.new_lines == ["context line", "new line", "context line"]

    def test_no_wrapper(self) -> None:
        patch = """*** Add File: test.py
+content"""
        result = parse_patch(patch)
        assert result.ok is True
        assert result.files[0].path == "test.py"

    def test_no_end_wrapper(self) -> None:
        patch = """*** Begin Patch
*** Add File: test.py
+content"""
        result = parse_patch(patch)
        assert result.ok is True

    def test_no_files_found(self) -> None:
        patch = """*** Begin Patch
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is False
        assert result.error == "No file operations found in patch"

    def test_invalid_line_in_hunk(self) -> None:
        # Lines not starting with +, -, or space are ignored
        patch = """*** Begin Patch
*** Update File: main.py
@@
-old
+new
invalid_line
*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        # invalid_line should be ignored
        assert result.files[0].hunks[0].old_lines == ["old"]
        assert result.files[0].hunks[0].new_lines == ["new"]

    def test_blank_line_in_hunk(self) -> None:
        patch = """*** Begin Patch
*** Update File: main.py
@@
-old

+new

*** End Patch"""
        result = parse_patch(patch)
        assert result.ok is True
        hunk = result.files[0].hunks[0]
        assert hunk.old_lines == ["old", ""]
        assert hunk.new_lines == ["", "new"]

    def test_crlf_line_endings(self) -> None:
        patch = "*** Begin Patch\r\n*** Add File: test.py\r\n+content\r\n*** End Patch\r\n"
        result = parse_patch(patch)
        assert result.ok is True
        assert result.files[0].content == "content\n"


class TestFindMatch:
    """Tests for the _find_match function."""

    def test_exact_match(self) -> None:
        content = ["line1", "line2", "line3", "line4"]
        old = ["line2", "line3"]
        assert _find_match(content, old) == 1

    def test_exact_match_at_start(self) -> None:
        content = ["line1", "line2", "line3"]
        old = ["line1", "line2"]
        assert _find_match(content, old) == 0

    def test_exact_match_at_end(self) -> None:
        content = ["line1", "line2", "line3"]
        old = ["line2", "line3"]
        assert _find_match(content, old) == 1

    def test_no_match(self) -> None:
        content = ["line1", "line2", "line3"]
        old = ["nonexistent", "line"]
        assert _find_match(content, old) == -1

    def test_empty_old_lines(self) -> None:
        content = ["line1", "line2"]
        old = []
        assert _find_match(content, old) == 0

    def test_trailing_whitespace_trim(self) -> None:
        content = ["line1  ", "line2\t", "line3"]
        old = ["line1", "line2"]
        assert _find_match(content, old) == 0

    def test_both_ends_trim(self) -> None:
        content = ["  line1  ", "  line2  "]
        old = ["line1", "line2"]
        assert _find_match(content, old) == 0

    def test_unicode_normalization(self) -> None:
        content = ["\u2018hello\u2019", "\u201ctest\u201d"]
        old = ["'hello'", '"test"']
        assert _find_match(content, old) == 0

    def test_unicode_dashes(self) -> None:
        content = ["\u2014 dash", "\u2013 ndash"]
        old = ["- dash", "- ndash"]
        assert _find_match(content, old) == 0

    def test_start_offset(self) -> None:
        content = ["a", "b", "a", "b", "c"]
        old = ["a", "b"]
        # Should find first match at index 0
        assert _find_match(content, old, start=0) == 0
        # But with offset 1, should find at index 2
        assert _find_match(content, old, start=1) == 2

    def test_offset_past_match(self) -> None:
        content = ["a", "b", "c"]
        old = ["a", "b"]
        # Start after the match
        assert _find_match(content, old, start=1) == -1


class TestApplyHunks:
    """Tests for the apply_hunks function."""

    def test_empty_hunks(self) -> None:
        result = apply_hunks("content", [])
        assert result.ok is True
        assert result.content == "content"

    def test_no_change(self) -> None:
        content = "line1\nline2\nline3"
        hunks = [PatchHunk(old_lines=["line2"], new_lines=["line2"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == content

    def test_simple_replacement(self) -> None:
        content = "line1\nline2\nline3"
        hunks = [PatchHunk(old_lines=["line2"], new_lines=["modified"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nmodified\nline3"

    def test_multiple_replacements(self) -> None:
        content = "line1\nline2\nline3\nline4"
        hunks = [
            PatchHunk(old_lines=["line2"], new_lines=["new2"]),
            PatchHunk(old_lines=["line3"], new_lines=["new3"]),
        ]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nnew2\nnew3\nline4"

    def test_context_lines(self) -> None:
        content = "line1\nline2\nline3\nline4\nline5"
        hunks = [
            PatchHunk(
                old_lines=["line2", "line3"],
                new_lines=["line2", "modified"],
            )
        ]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nline2\nmodified\nline4\nline5"

    def test_add_lines(self) -> None:
        content = "line1\nline2"
        hunks = [PatchHunk(old_lines=["line2"], new_lines=["line2", "new_line"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nline2\nnew_line"

    def test_delete_lines(self) -> None:
        content = "line1\nline2\nline3"
        hunks = [PatchHunk(old_lines=["line2", "line3"], new_lines=[])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1"

    def test_not_found(self) -> None:
        content = "line1\nline2"
        hunks = [PatchHunk(old_lines=["nonexistent"], new_lines=["new"])]
        result = apply_hunks(content, hunks)
        assert result.ok is False
        assert "Could not find context" in result.error

    def test_fallback_to_start(self) -> None:
        content = "line1\nline2\nline1\nline2"
        # First match at offset 1 would be at index 2, but we start at offset 0
        hunks = [PatchHunk(old_lines=["line1", "line2"], new_lines=["new"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        # Should match at index 0
        assert result.content == "new\nline1\nline2"

    def test_trailing_newline_preserved(self) -> None:
        content = "line1\nline2\n"
        hunks = [PatchHunk(old_lines=["line2"], new_lines=["modified"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nmodified\n"

    def test_no_trailing_newline(self) -> None:
        content = "line1\nline2"
        hunks = [PatchHunk(old_lines=["line2"], new_lines=["modified"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "line1\nmodified"

    def test_empty_old_lines(self) -> None:
        # Empty old_lines means insert at start
        content = "line1\nline2"
        hunks = [PatchHunk(old_lines=[], new_lines=["new_first"])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "new_first\nline1\nline2"

    def test_multiple_hunks_with_offset(self) -> None:
        content = "a\nb\nc\nd\ne"
        hunks = [
            PatchHunk(old_lines=["b"], new_lines=["B"]),
            PatchHunk(old_lines=["d"], new_lines=["D"]),
        ]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == "a\nB\nc\nD\ne"

    def test_empty_hunk(self) -> None:
        content = "line1\nline2"
        hunks = [PatchHunk(old_lines=[], new_lines=[])]
        result = apply_hunks(content, hunks)
        assert result.ok is True
        assert result.content == content


class TestApplyPatchToFile:
    """Tests for the apply_patch_to_file function."""

    def test_add_file(self) -> None:
        file = PatchFile(action=PatchAction.ADD, path="new.py", content="print('hello')")
        result = apply_patch_to_file("", file)
        assert result.ok is True
        assert result.content == "print('hello')"

    def test_delete_file(self) -> None:
        file = PatchFile(action=PatchAction.DELETE, path="old.py")
        result = apply_patch_to_file("original content", file)
        assert result.ok is True
        assert result.content == ""

    def test_update_file(self) -> None:
        file = PatchFile(
            action=PatchAction.UPDATE,
            path="main.py",
            hunks=[PatchHunk(old_lines=["old"], new_lines=["new"])],
        )
        result = apply_patch_to_file("old\ncontent", file)
        assert result.ok is True
        assert result.content == "new\ncontent"

    def test_unknown_action(self) -> None:
        # This tests the defensive fallback
        file = PatchFile(action="invalid", path="test.py")
        result = apply_patch_to_file("content", file)
        assert result.ok is False
        assert "Unknown action" in result.error


class TestIntegration:
    """Integration tests parsing and applying patches."""

    def test_parse_and_apply_add(self) -> None:
        patch_text = """*** Begin Patch
*** Add File: new.py
+print('hello world')
*** End Patch"""
        result = parse_patch(patch_text)
        assert result.ok is True

        apply_result = apply_patch_to_file("", result.files[0])
        assert apply_result.ok is True
        assert apply_result.content == "print('hello world')\n"

    def test_parse_and_apply_update(self) -> None:
        patch_text = """*** Begin Patch
*** Update File: main.py
@@
-print('old')
+print('new')
*** End Patch"""
        result = parse_patch(patch_text)
        assert result.ok is True

        apply_result = apply_patch_to_file("print('old')\n", result.files[0])
        assert apply_result.ok is True
        assert apply_result.content == "print('new')\n"

    def test_parse_and_apply_delete(self) -> None:
        patch_text = """*** Begin Patch
*** Delete File: old.py
*** End Patch"""
        result = parse_patch(patch_text)
        assert result.ok is True

        apply_result = apply_patch_to_file("content", result.files[0])
        assert apply_result.ok is True
        assert apply_result.content == ""

    def test_parse_and_apply_multi_file(self) -> None:
        patch_text = """*** Begin Patch
*** Add File: new.py
+new file
*** Update File: existing.py
@@
-old
+new
*** Delete File: remove.py
*** End Patch"""
        result = parse_patch(patch_text)
        assert result.ok is True
        assert len(result.files) == 3

        # Apply each
        assert apply_patch_to_file("", result.files[0]).ok is True
        assert apply_patch_to_file("old\n", result.files[1]).content == "new\n"
        assert apply_patch_to_file("content", result.files[2]).content == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
