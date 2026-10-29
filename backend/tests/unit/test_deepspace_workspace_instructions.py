"""Tests for workspace instruction loader."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.deepspace.services.workspace_instructions import (
    apply_instruction_version_updates,
    discover_baseline_files,
    discover_nested_files,
    load_baseline_instructions,
    reconcile_instruction_context,
    render_agent_instructions,
)


def test_discover_baseline_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "AGENTS.md").write_text("# Baseline\nContent")
        (root / "CLAUDE.md").write_text("# Claude\nContent")

        files = discover_baseline_files(root)
        assert len(files) == 2
        assert files[0].relative_path == "AGENTS.md"
        assert files[0].is_baseline is True
        assert "Baseline" in files[0].content


def test_discover_baseline_files_priority():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "AGENTS.md").write_text("# Agents")
        (root / "CLAUDE.md").write_text("# Claude")

        files = discover_baseline_files(root)
        # AGENTS.md should come first (priority order)
        assert files[0].relative_path == "AGENTS.md"


def test_discover_nested_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("# Nested Agents")
        (project / "sub").mkdir(parents=True)
        (project / "sub" / "CLAUDE.md").write_text("# Nested Claude")

        files = discover_nested_files(root, project)
        assert len(files) == 2
        assert all(not f.is_baseline for f in files)
        rel_paths = {f.relative_path for f in files}
        assert "project/AGENTS.md" in rel_paths
        assert "project/sub/CLAUDE.md" in rel_paths


def test_load_baseline_instructions():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("# Baseline")
        (project / "CLAUDE.md").write_text("# Nested")

        loaded = load_baseline_instructions(root, project)
        assert loaded.baseline is not None
        assert loaded.baseline.relative_path == "AGENTS.md"
        assert len(loaded.nested) == 1
        assert loaded.nested[0].relative_path == "project/CLAUDE.md"


def test_reconcile_instruction_context():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("# Baseline")
        (project / "CLAUDE.md").write_text("# Nested")

        loaded = load_baseline_instructions(root, project)
        context = reconcile_instruction_context(None, loaded)

        assert context.baseline_content == "# Baseline"
        assert len(context.nested_instructions) == 1
        assert context.nested_instructions[0][0] == "project/CLAUDE.md"
        assert context.nested_instructions[0][1] == "# Nested"
        assert context.project_root == str(project)


def test_reconcile_preserves_versions():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("# Baseline")
        (project / "CLAUDE.md").write_text("# Nested")

        loaded = load_baseline_instructions(root, project)
        context1 = reconcile_instruction_context(None, loaded)

        # Get the computed versions (hash-based)
        baseline_v1 = context1.version_cache.get_version("AGENTS.md")
        nested_v1 = context1.version_cache.get_version("project/CLAUDE.md")
        assert baseline_v1 != 0
        assert nested_v1 != 0

        # Reload and reconcile - versions should be preserved since content hasn't changed
        loaded2 = load_baseline_instructions(root, project)
        context2 = reconcile_instruction_context(context1, loaded2)

        assert context2.version_cache.get_version("AGENTS.md") == baseline_v1
        assert context2.version_cache.get_version("project/CLAUDE.md") == nested_v1


def test_apply_instruction_version_updates():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("# Baseline")
        (project / "CLAUDE.md").write_text("# Nested")

        loaded = load_baseline_instructions(root, project)
        context = reconcile_instruction_context(None, loaded)

        # Get initial versions (hash-based)
        baseline_v1 = context.version_cache.get_version("AGENTS.md")
        nested_v1 = context.version_cache.get_version("project/CLAUDE.md")
        assert baseline_v1 != 0
        assert nested_v1 != 0

        # Update versions after tool modifies files
        updated = apply_instruction_version_updates(context, ["AGENTS.md", "project/CLAUDE.md"])

        # Versions should increment by 1
        assert updated.version_cache.get_version("AGENTS.md") == baseline_v1 + 1
        assert updated.version_cache.get_version("project/CLAUDE.md") == nested_v1 + 1


def test_render_agent_instructions():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("# Baseline\nContent")
        (project / "CLAUDE.md").write_text("# Nested\nContent")

        loaded = load_baseline_instructions(root, project)
        context = reconcile_instruction_context(None, loaded)
        rendered = render_agent_instructions(context, max_bytes=10000)

        assert "Workspace Baseline Instructions" in rendered
        assert "Baseline" in rendered
        assert "Nested Workspace Instructions" in rendered
        assert "project/CLAUDE.md" in rendered


def test_render_agent_instructions_truncation():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()
        (root / "AGENTS.md").write_text("x" * 5000)
        (project / "CLAUDE.md").write_text("y" * 5000)

        loaded = load_baseline_instructions(root, project)
        context = reconcile_instruction_context(None, loaded)
        rendered = render_agent_instructions(context, max_bytes=1000)

        assert len(rendered.encode("utf-8")) <= 1000
        assert "[... instructions truncated" in rendered


def test_empty_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "project"
        project.mkdir()

        loaded = load_baseline_instructions(root, project)
        assert loaded.baseline is None
        assert len(loaded.nested) == 0

        context = reconcile_instruction_context(None, loaded)
        rendered = render_agent_instructions(context)
        assert rendered == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
