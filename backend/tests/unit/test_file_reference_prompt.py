"""Tests for file reference prompt functions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.deepspace.services.file_reference_prompt import (
    file_reference_system_prompt,
    file_reference_system_prompt_section,
)


class TestFileReferenceSystemPrompt:
    """Tests for the file_reference_system_prompt function."""

    def test_enabled_returns_prompt(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=True)
        result = file_reference_system_prompt(settings)

        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0
        assert "File reference guidance" in result
        assert "Inline reference" in result
        assert "Deliverable declaration" in result
        assert "@file references" in result
        assert "present" in result

    def test_disabled_returns_none(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=False)
        result = file_reference_system_prompt(settings)

        assert result is None

    def test_missing_attribute_defaults_enabled(self) -> None:
        settings = SimpleNamespace()
        result = file_reference_system_prompt(settings)

        # Should default to enabled (bool of missing attr is False, but getattr with default True)
        # Actually getattr(settings, "attr", True) returns True if attr missing
        assert result is not None

    def test_content_contains_key_sections(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=True)
        result = file_reference_system_prompt(settings)

        assert "1. **Inline reference**" in result
        assert "2. **Deliverable declaration**" in result
        assert "3. **@file references**" in result
        assert "File reference guidance" in result
        assert "`present` tool" in result
        assert "@file references" in result
        assert "workspace-relative" in result
        assert "fuzzy-match" in result

    def test_examples_included(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=True)
        result = file_reference_system_prompt(settings)

        assert "`src/main.py`" in result
        assert "`present(paths=" in result
        assert "@src/main.py" in result
        assert "@main.py" in result
        assert "@tests/test_main.py" in result


class TestFileReferenceSystemPromptSection:
    """Tests for the file_reference_system_prompt_section alias function."""

    def test_alias_returns_same_result(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=True)

        result1 = file_reference_system_prompt(settings)
        result2 = file_reference_system_prompt_section(settings)

        assert result1 == result2

    def test_alias_returns_none_when_disabled(self) -> None:
        settings = SimpleNamespace(deepspace_file_references_enabled=False)

        result = file_reference_system_prompt_section(settings)

        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
