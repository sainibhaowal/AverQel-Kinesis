"""Tests for the DeepSpace workspace snapshot service and the diff tool.

The checks cover the collection of a workspace snapshot, the diff between
two snapshot file sets, the store that keeps the newest snapshot per
workspace, and the tool that reports a workspace's changes.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auth.dependencies import AuthContext
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.diff import DIFF_TOOLS, WorkspaceDiffArguments
from app.deepspace.services.workspace_root import workspace_root_for
from app.deepspace.services.workspace_snapshot import (
    TRUNCATION_NOTICE,
    SnapshotFile,
    SnapshotStore,
    collect_snapshot_files,
    diff_snapshots,
    snapshot_store,
)

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path) -> SimpleNamespace:
    """Build the deployment settings one test runs with."""
    return SimpleNamespace(deepspace_workspace_root=str(tmp_path / "workspaces"))


def _context(tmp_path: Path) -> ToolContext:
    """Build a tool context whose request has no database session."""
    return ToolContext(
        auth=AuthContext(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            roles=frozenset(),
            token_id="test-token",
        ),
        conversation_id=uuid.uuid4(),
        settings=_settings(tmp_path),
        db=None,
        task_store=None,
        tool_result_store=None,
        provider_registry=None,
    )


def _root(context: ToolContext) -> Path:
    """Return the workspace root one context owns."""
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _file(relative_path: str, content_hash: str, size_bytes: int) -> SnapshotFile:
    """Build one snapshot entry for a test."""
    return SnapshotFile(
        relative_path=relative_path,
        content_hash=content_hash,
        size_bytes=size_bytes,
    )


# --------------------------------------------------------------------------
# Collecting a snapshot
# --------------------------------------------------------------------------


def test_the_collection_finds_text_and_skips_binary_and_git(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01\x02binary")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("module.exports = 1;\n", encoding="utf-8")

    collected = collect_snapshot_files(root)

    assert [item.relative_path for item in collected] == ["src/app.py"]
    assert collected[0].size_bytes == len("print('hi')\n")
    assert len(collected[0].content_hash) == 64


def test_the_collection_reports_the_files_in_path_order(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")

    collected = collect_snapshot_files(root)

    assert [item.relative_path for item in collected] == ["a.txt", "b.txt"]


# --------------------------------------------------------------------------
# Diffing two snapshot file sets
# --------------------------------------------------------------------------


def test_the_diff_reports_an_added_a_removed_and_a_modified_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "added.py").write_text("new line\n", encoding="utf-8")
    before = [
        _file("kept.py", "a" * 64, 8),
        _file("gone.py", "b" * 64, 3),
    ]
    after = [
        _file("kept.py", "c" * 64, 9),
        _file("added.py", "d" * 64, 9),
    ]

    text = diff_snapshots(before, after, root=root)

    assert "kept.py" in text
    assert "gone.py" in text
    assert "added.py" in text
    assert "new line" in text


def test_the_diff_is_truncated_at_the_cap_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "added.py").write_text("new line\n" * 100, encoding="utf-8")
    before = [_file("gone.py", "b" * 64, 3)]
    after = [_file("added.py", "d" * 64, 900)]

    text = diff_snapshots(before, after, root=root, max_diff_bytes=200)

    assert TRUNCATION_NOTICE in text
    assert len(text) <= 200


def test_the_diff_of_two_equal_file_sets_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    files = [_file("same.py", "e" * 64, 4)]

    assert diff_snapshots(files, files, root=root) == ""


# --------------------------------------------------------------------------
# The snapshot store
# --------------------------------------------------------------------------


def test_the_store_keeps_the_latest_snapshot_per_workspace() -> None:
    store = SnapshotStore()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    root = "/tmp/workspace"

    assert store.latest(tenant_id=tenant_id, user_id=user_id, root=root) is None

    first = store.save(
        tenant_id=tenant_id,
        user_id=user_id,
        root=root,
        files=[_file("a.py", "a" * 64, 1)],
    )
    second = store.save(
        tenant_id=tenant_id,
        user_id=user_id,
        root=root,
        files=[_file("a.py", "b" * 64, 2)],
    )

    latest = store.latest(tenant_id=tenant_id, user_id=user_id, root=root)
    assert latest is not None
    assert latest.id == second.id
    assert latest.id != first.id
    assert latest.files[0].content_hash == "b" * 64


def test_the_store_keeps_the_workspaces_apart() -> None:
    store = SnapshotStore()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    store.save(
        tenant_id=tenant_id,
        user_id=user_id,
        root="/tmp/one",
        files=[_file("a.py", "a" * 64, 1)],
    )

    assert store.latest(tenant_id=tenant_id, user_id=user_id, root="/tmp/two") is None
    assert store.latest(tenant_id=uuid.uuid4(), user_id=user_id, root="/tmp/one") is None
    assert store.latest(tenant_id=tenant_id, user_id=uuid.uuid4(), root="/tmp/one") is None


def test_clearing_the_store_empties_it() -> None:
    store = SnapshotStore()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    store.save(
        tenant_id=tenant_id,
        user_id=user_id,
        root="/tmp/one",
        files=[_file("a.py", "a" * 64, 1)],
    )

    store.clear()

    assert store.latest(tenant_id=tenant_id, user_id=user_id, root="/tmp/one") is None


def test_the_module_serves_one_store() -> None:
    assert snapshot_store() is snapshot_store()


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


async def test_the_tool_reports_the_workspace_without_a_baseline(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({"path": None, "max_bytes": 100_000}, context)

    assert result["status"] == "ok"
    assert result["source"] in {"git", "snapshot", "none"}
    assert "app.py" in result["changed_files"]
    assert "app.py" in result["diff"]


async def test_the_tool_reports_the_change_against_a_baseline(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    snapshot_store().save(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        root=str(root),
        files=collect_snapshot_files(root),
    )
    (root / "app.py").write_text("print('bye')\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({"path": None, "max_bytes": 100_000}, context)

    assert result["status"] == "ok"
    assert result["source"] == "snapshot"
    assert result["changed_files"] == ["app.py"]
    assert "app.py" in result["diff"]
    assert result["truncated"] is False


async def test_the_tool_refuses_a_path_outside_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _root(context)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({"path": "../outside.txt"}, context)

    assert result["status"] == "error"
    assert result["message"]
    assert "diff" not in result
    assert "secret" not in str(result)


async def test_the_tool_refuses_a_symlink_that_leaves_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    result = await DIFF_TOOLS[0].handler({"path": "link.txt"}, context)

    assert result["status"] == "error"
    assert "secret" not in str(result)
    assert "link.txt" not in collect_snapshot_files(root)


async def test_the_tool_runs_without_a_database_session(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert context.db is None
    root = _root(context)
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({}, context)

    assert result["status"] == "ok"


async def test_the_tool_limits_the_diff_to_one_directory(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "keep").mkdir()
    (root / "keep" / "a.py").write_text("a\n", encoding="utf-8")
    (root / "other.py").write_text("b\n", encoding="utf-8")
    snapshot_store().save(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        root=str(root),
        files=collect_snapshot_files(root),
    )
    (root / "keep" / "a.py").write_text("changed\n", encoding="utf-8")
    (root / "other.py").write_text("changed too\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({"path": "keep"}, context)

    assert result["status"] == "ok"
    assert result["changed_files"] == ["keep/a.py"]


def test_the_tool_is_declared_as_a_read_tool() -> None:
    spec = DIFF_TOOLS[0]

    assert spec.name == "workspace_diff"
    assert spec.mode == "read"
    assert spec.arguments_model is WorkspaceDiffArguments
    properties = spec.definition["function"]["parameters"]["properties"]
    assert set(properties) == {"path", "max_bytes"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_the_tool_uses_git_inside_a_repository(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run_git(root, ["init", "-q", "."])
    _run_git(root, ["config", "user.email", "test@example.com"])
    _run_git(root, ["config", "user.name", "Test"])
    _run_git(root, ["add", "app.py"])
    _run_git(root, ["commit", "-q", "-m", "first"])
    (root / "app.py").write_text("print('bye')\n", encoding="utf-8")

    result = await DIFF_TOOLS[0].handler({}, context)

    assert result["status"] == "ok"
    assert result["source"] == "git"
    assert result["changed_files"] == ["app.py"]
    assert "-print('hi')" in result["diff"]
    assert "+print('bye')" in result["diff"]

    scoped = await DIFF_TOOLS[0].handler({"path": "app.py"}, context)

    assert scoped["status"] == "ok"
    assert scoped["source"] == "git"
    assert scoped["changed_files"] == ["app.py"]


def _run_git(root: Path, arguments: list[str]) -> None:
    """Run one git command in a test repository."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
