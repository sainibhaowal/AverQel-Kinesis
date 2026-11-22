"""Tests for the consolidated DeepSpace unified tools.

These tests verify that the unified ``read``/``write``/``glob``/``grep`` tools
delegate to the existing filesystem handlers and that new capabilities work:

* directory listing returns immediate (non-recursive) entries
* glob defaults to non-recursive unless the pattern contains ``**``
* workspace write/edit/str_replace results carry reviewable ``diff`` metadata
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.unified import (
    UNIFIED_TOOLS,
    _handle_read,
    _handle_write,
)
from app.deepspace.services.workspace_root import workspace_root_for

pytestmark = pytest.mark.unit_no_db


def _context(tmp_path: Path) -> ToolContext:
    """Build a tool context whose request has no database session."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    settings = SimpleNamespace(deepspace_workspace_root=str(tmp_path / "workspaces"))
    return ToolContext(
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=uuid.uuid4(),
        settings=settings,
        db=None,
        task_store=None,
        tool_result_store=None,
        provider_registry=None,
        workspace_root=workspace_root_for(settings, tenant_id, user_id),
    )


def _root(context: ToolContext) -> Path:
    """Return the workspace root one context owns."""
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def test_unified_tools_register_the_expected_names() -> None:
    """The unified package exposes exactly the 14 core tool names."""
    names = {spec.name for spec in UNIFIED_TOOLS}
    expected = {
        "read",
        "write",
        "glob",
        "grep",
        "lock",
        "unlock",
        "watch",
        "unwatch",
        "delete",
        "stat",
        "web_search",
        "web_fetch",
        "image_read",
        "get_tool_result",
    }
    assert names == expected


async def test_read_on_directory_returns_immediate_entries(tmp_path: Path) -> None:
    """Reading a directory lists its immediate entries, not a recursive walk."""
    context = _context(tmp_path)
    root = _root(context)
    workdir = root / "workdir"
    workdir.mkdir()
    (workdir / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workdir / "b.txt").write_text("beta\n", encoding="utf-8")
    (workdir / "sub").mkdir()
    (workdir / "sub" / "c.txt").write_text("gamma\n", encoding="utf-8")

    result = await _handle_read({"path": "workdir"}, context)

    assert result["status"] == "ok"
    assert result["type"] == "directory"
    names = {entry["name"] for entry in result["entries"]}
    assert names == {"a.txt", "b.txt", "sub"}
    # Not recursive: sub/c.txt should not appear.
    assert not any(entry["name"] == "c.txt" for entry in result["entries"])


async def test_read_on_file_returns_numbered_text(tmp_path: Path) -> None:
    """Reading a file returns numbered text and a version."""
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("line one\nline two\n", encoding="utf-8")

    result = await _handle_read({"path": "app.py"}, context)

    assert result["status"] == "ok"
    assert "line one" in result["content"]
    assert "line two" in result["content"]
    assert result["version"]


async def test_write_returns_reviewable_diff(tmp_path: Path) -> None:
    """Creating a file returns a diff with additions and no deletions."""
    context = _context(tmp_path)

    result = await _handle_write(
        {
            "path": "new.txt",
            "content": "hello\nworld\n",
        },
        context,
    )

    assert result["status"] == "ok"
    assert "diff" in result
    assert result["diff"]["additions"] == 2
    assert result["diff"]["deletions"] == 0


async def test_write_overwrite_returns_diff(tmp_path: Path) -> None:
    """Overwriting a file returns a diff showing removed and added lines."""
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    # Must read first to establish observation for the strict write guard.
    await _handle_read({"path": "app.py"}, context)

    result = await _handle_write(
        {
            "path": "app.py",
            "operation": "replace",
            "content": "one\nTWO\nthree\n",
        },
        context,
    )

    assert result["status"] == "ok"
    assert "diff" in result
    assert result["diff"]["additions"] >= 1
    assert result["diff"]["deletions"] >= 1


async def test_str_replace_returns_diff(tmp_path: Path) -> None:
    """str_replace returns a diff showing the changed lines."""
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("foo\nbar\nbaz\n", encoding="utf-8")

    # Must read first to establish observation for the strict write guard.
    await _handle_read({"path": "app.py"}, context)

    result = await _handle_write(
        {
            "path": "app.py",
            "operation": "str_replace",
            "old_string": "bar",
            "new_string": "BAR",
        },
        context,
    )

    assert result["status"] == "ok"
    assert "diff" in result
    assert result["diff"]["additions"] == 1
    assert result["diff"]["deletions"] == 1


async def test_glob_without_double_star_is_non_recursive(tmp_path: Path) -> None:
    """Glob with '*.txt' lists only the immediate directory, not subdirectories."""
    context = _context(tmp_path)
    root = _root(context)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b\n", encoding="utf-8")

    from app.deepspace.services.tools.fs_search import _fs_glob_sync

    result = _fs_glob_sync({"pattern": "*.txt", "path": "."}, context)

    assert result["status"] == "ok"
    names = {Path(m).name for m in result["matches"]}
    assert names == {"a.txt"}
    assert "b.txt" not in names


async def test_glob_with_double_star_is_recursive(tmp_path: Path) -> None:
    """Glob with '**/*.txt' recurses into subdirectories."""
    context = _context(tmp_path)
    root = _root(context)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b\n", encoding="utf-8")

    from app.deepspace.services.tools.fs_search import _fs_glob_sync

    result = _fs_glob_sync({"pattern": "**/*.txt", "path": "."}, context)

    assert result["status"] == "ok"
    names = {Path(m).name for m in result["matches"]}
    assert names == {"a.txt", "b.txt"}


async def test_downloads_returns_top_level_entries(tmp_path: Path) -> None:
    """Reading a directory returns the immediate entries, not a recursive limit."""
    context = _context(tmp_path)
    root = _root(context)
    workdir = root / "workdir"
    workdir.mkdir()

    # Create 30 immediate entries.
    for i in range(30):
        (workdir / f"file_{i:02d}.txt").write_text(f"content {i}\n", encoding="utf-8")

    result = await _handle_read({"path": "workdir"}, context)

    assert result["status"] == "ok"
    assert result["entry_count"] == 30
    assert not result["truncated"]
    names = {entry["name"] for entry in result["entries"]}
    assert len(names) == 30
