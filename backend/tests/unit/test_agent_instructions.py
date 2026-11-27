"""Tests for agent instructions loader."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.deepspace.services.agent_instructions import (
    INSTRUCTION_FILES,
    MAX_INSTRUCTION_CHARS,
    AgentInstructionsLoader,
    build_instructions_section,
    get_instructions_loader,
    load_workspace_instructions,
)


class TestAgentInstructionsLoader:
    """Tests for the AgentInstructionsLoader class."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)
        self.loader = AgentInstructionsLoader()

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initialization(self) -> None:
        assert self.loader._cache == {}

    def test_load_instructions_no_files(self) -> None:
        result = self.loader.load_instructions(self.workspace)
        assert result is None

    def test_load_instructions_agents_md(self) -> None:
        (self.workspace / "AGENTS.md").write_text("# Agent Instructions\n\nBe helpful.")
        result = self.loader.load_instructions(self.workspace)
        assert result is not None
        assert "Agent Instructions" in result
        assert "Be helpful" in result

    def test_load_instructions_claude_md_priority(self) -> None:
        (self.workspace / "AGENTS.md").write_text("AGENTS content")
        (self.workspace / "CLAUDE.md").write_text("CLAUDE content")
        # AGENTS.md should take priority
        result = self.loader.load_instructions(self.workspace)
        assert result == "AGENTS content"

    def test_load_instructions_other_files(self) -> None:
        for filename in INSTRUCTION_FILES[2:]:
            (self.workspace / filename).write_text(f"Content from {filename}")
            result = self.loader.load_instructions(self.workspace)
            assert result is not None
            assert f"Content from {filename}" in result
            # Clean up for next iteration
            (self.workspace / filename).unlink()

    def test_load_instructions_truncation(self) -> None:
        long_content = "x" * (MAX_INSTRUCTION_CHARS + 1000)
        (self.workspace / "AGENTS.md").write_text(long_content)
        result = self.loader.load_instructions(self.workspace)
        assert result is not None
        assert len(result) <= MAX_INSTRUCTION_CHARS + 50  # Allow for truncation message
        assert "[... truncated ...]" in result

    def test_load_instructions_caching(self) -> None:
        (self.workspace / "AGENTS.md").write_text("Original content")
        result1 = self.loader.load_instructions(self.workspace)
        assert result1 == "Original content"

        # Same mtime (no modification) - should return cached version
        result2 = self.loader.load_instructions(self.workspace)
        assert result2 == "Original content"

        # Clear cache and reload (simulating external modification)
        self.loader.clear_cache()
        result3 = self.loader.load_instructions(self.workspace)
        assert result3 == "Original content"

    def test_load_instructions_errors(self) -> None:
        # Create a file that can't be read (e.g., directory with same name)
        (self.workspace / "AGENTS.md").mkdir()
        result = self.loader.load_instructions(self.workspace)
        # Should skip and return None
        assert result is None

    def test_clear_cache(self) -> None:
        (self.workspace / "AGENTS.md").write_text("Content")
        self.loader.load_instructions(self.workspace)
        assert len(self.loader._cache) == 1

        self.loader.clear_cache()
        assert len(self.loader._cache) == 0


class TestGlobalFunctions:
    """Tests for module-level functions."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_instructions_loader(self) -> None:
        loader1 = get_instructions_loader()
        loader2 = get_instructions_loader()
        assert loader1 is loader2  # Singleton

    def test_load_workspace_instructions(self) -> None:
        (self.workspace / "AGENTS.md").write_text("Test instructions")
        result = load_workspace_instructions(self.workspace)
        assert result == "Test instructions"

    def test_load_workspace_instructions_none(self) -> None:
        result = load_workspace_instructions(self.workspace)
        assert result is None

    def test_build_instructions_section(self) -> None:
        instructions = "Be helpful and concise."
        result = build_instructions_section(instructions)
        assert result is not None
        assert "Workspace instructions" in result
        assert "Be helpful and concise" in result
        assert "project-specific guidance" in result

    def test_build_instructions_section_none(self) -> None:
        result = build_instructions_section(None)
        assert result is None

    def test_build_instructions_section_empty(self) -> None:
        result = build_instructions_section("")
        assert result is None

    def test_build_instructions_section_whitespace(self) -> None:
        result = build_instructions_section("   ")
        # Whitespace string is truthy, so function returns header with empty content
        assert result is not None
        assert "Workspace instructions" in result


class TestConstants:
    """Tests for module constants."""

    def test_instruction_files_order(self) -> None:
        # AGENTS.md should be first (highest priority)
        assert INSTRUCTION_FILES[0] == "AGENTS.md"
        assert INSTRUCTION_FILES[1] == "CLAUDE.md"

    def test_max_instruction_chars(self) -> None:
        assert MAX_INSTRUCTION_CHARS == 50_000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
