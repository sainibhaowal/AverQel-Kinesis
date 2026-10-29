"""Tests for the version guards on the DeepSpace workspace edit tools.

The checks cover the refusal of an edit whose expected version no longer
matches the file on disk, the refusal of an edit whose target is gone, the
application of an edit that carries the version the file holds, and the
unchanged behaviour of an edit that carries no version at all.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.fs import FS_TOOLS, FsEditArguments, FsStrReplaceArguments
from app.deepspace.services.workspace_root import workspace_root_for

pytestmark = pytest.mark.unit_no_db


def _context(tmp_path: Path) -> ToolContext:
    """Build a tool context whose request has no database session."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=SimpleNamespace(deepspace_workspace_root=str(tmp_path / "workspaces")),
        db=None,
        task_store=None,
        tool_result_store=None,
        provider_registry=None,
    )


def _tool(name: str):  # noqa: ANN202 - the specs carry their own types
    """Return the spec of one file system tool."""
    for spec in FS_TOOLS:
        if spec.name == name:
            return spec
    raise AssertionError(f"the package does not own a tool named {name}")


def _root(context: ToolContext) -> Path:
    """Return the workspace root one context owns."""
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _version(text: str) -> str:
    """Return the version one file content holds."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# A stale version
# --------------------------------------------------------------------------


async def test_a_stale_version_refuses_the_edit_and_leaves_the_file_alone(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")
    stale = _version("one\n")
    (root / "app.py").write_text("two\n", encoding="utf-8")

    result = await _tool("fs_edit").handler(
        {
            "path": "app.py",
            "operation": "replace",
            "content": "three\n",
            "expected_version": stale,
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "FS_STALE_VERSION"
    assert result["current_version"] == _version("two\n")
    assert (root / "app.py").read_text(encoding="utf-8") == "two\n"


async def test_a_stale_version_refuses_the_replacement_and_leaves_the_file_alone(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")
    stale = _version("one\n")
    (root / "app.py").write_text("two\n", encoding="utf-8")

    result = await _tool("fs_str_replace").handler(
        {
            "path": "app.py",
            "old_string": "two",
            "new_string": "three",
            "expected_version": stale,
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "FS_STALE_VERSION"
    assert result["current_version"] == _version("two\n")
    assert (root / "app.py").read_text(encoding="utf-8") == "two\n"


async def test_a_missing_file_with_an_expected_version_is_an_ambiguous_edit(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _root(context)

    result = await _tool("fs_edit").handler(
        {
            "path": "absent.py",
            "operation": "replace",
            "content": "three\n",
            "expected_version": _version("one\n"),
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "FS_AMBIGUOUS_EDIT"
    assert "current_version" not in result


async def test_a_missing_file_with_an_expected_version_is_an_ambiguous_replacement(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _root(context)

    result = await _tool("fs_str_replace").handler(
        {
            "path": "absent.py",
            "old_string": "one",
            "expected_version": _version("one\n"),
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "FS_AMBIGUOUS_EDIT"


# --------------------------------------------------------------------------
# A matching version
# --------------------------------------------------------------------------


async def test_a_matching_version_applies_the_edit_and_reports_the_new_version(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")
    version = _version("one\n")

    result = await _tool("fs_edit").handler(
        {
            "path": "app.py",
            "operation": "replace",
            "content": "three\n",
            "expected_version": version,
        },
        context,
    )

    assert result["status"] == "ok"
    assert result["version"] == _version("three\n")
    assert (root / "app.py").read_text(encoding="utf-8") == "three\n"


async def test_a_matching_version_applies_the_replacement_and_reports_the_new_version(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")
    version = _version("one\n")

    result = await _tool("fs_str_replace").handler(
        {
            "path": "app.py",
            "old_string": "one",
            "new_string": "two",
            "expected_version": version,
        },
        context,
    )

    assert result["status"] == "ok"
    assert result["version"] == _version("two\n")
    assert (root / "app.py").read_text(encoding="utf-8") == "two\n"


async def test_a_read_reports_the_version_an_edit_can_guard_with(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")

    result = await _tool("fs_read").handler({"path": "app.py"}, context)

    assert result["status"] == "ok"
    assert result["version"] == _version("one\n")


# --------------------------------------------------------------------------
# No version at all
# --------------------------------------------------------------------------


async def test_an_edit_without_a_version_behaves_as_it_did_before(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")

    result = await _tool("fs_edit").handler(
        {"path": "app.py", "operation": "replace", "content": "three\n"},
        context,
    )

    assert result["status"] == "ok"
    assert result["path"] == "app.py"
    assert (root / "app.py").read_text(encoding="utf-8") == "three\n"


async def test_a_replacement_without_a_version_behaves_as_it_did_before(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "app.py").write_text("one\n", encoding="utf-8")

    result = await _tool("fs_str_replace").handler(
        {"path": "app.py", "old_string": "one", "new_string": "two"},
        context,
    )

    assert result["status"] == "ok"
    assert result["replacements"] == 1
    assert (root / "app.py").read_text(encoding="utf-8") == "two\n"


async def test_an_edit_of_a_missing_file_without_a_version_stays_not_found(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _root(context)

    result = await _tool("fs_edit").handler(
        {"path": "absent.py", "operation": "replace", "content": "three\n"},
        context,
    )

    assert result["status"] == "not_found"


# --------------------------------------------------------------------------
# The declared arguments
# --------------------------------------------------------------------------


def test_the_edit_tools_declare_the_expected_version() -> None:
    edit_properties = _tool("fs_edit").definition["function"]["parameters"]["properties"]
    replace_properties = _tool("fs_str_replace").definition["function"]["parameters"]["properties"]

    assert "expected_version" in edit_properties
    assert "expected_version" in replace_properties


def test_the_argument_models_accept_the_expected_version() -> None:
    arguments = FsEditArguments(path="a.md", content="x", expected_version="a" * 64)
    replacement = FsStrReplaceArguments(path="a.md", old_string="x", expected_version="a" * 64)

    assert arguments.expected_version == "a" * 64
    assert replacement.expected_version == "a" * 64
