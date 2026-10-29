"""Tests for the DeepSpace native tool package and the tools it owns."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY, summarize_tool_arguments
from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL_NAMES
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.tool_registry import NativeToolValidationError
from app.deepspace.services.tools import (
    NATIVE_TOOL_HANDLERS,
    NATIVE_TOOL_MODES,
    NATIVE_TOOL_SPECS,
    dispatch_native_tool,
    unavailable_tool_names,
)
from app.deepspace.services.tools.fs import FsReadArguments, FsWriteArguments
from app.deepspace.services.tools.shell import read_only_violation
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    normalize_relative_path,
    resolve_within_root,
)

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build a settings double for the workspace tools."""
    base = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_shell_timeout_seconds": 10,
        "deepspace_shell_max_output_bytes": 65_536,
        "deepspace_subagent_max_depth": 1,
        "deepspace_subagent_max_children": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(tmp_path: Path, **overrides: Any) -> Any:
    """Build a tool context double for the workspace tools."""
    from app.deepspace.services.tools._base import ToolContext

    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=_settings(tmp_path, **overrides),
        db=SimpleNamespace(),
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )


def test_every_registered_tool_has_a_policy_mode_and_a_handler() -> None:
    registered = set(NATIVE_TOOL_REGISTRY.names())
    policy = set(DeepSpaceToolPolicy._MODES)

    assert registered | MCP_BROKER_TOOL_NAMES == policy
    for spec in NATIVE_TOOL_SPECS:
        assert NATIVE_TOOL_MODES[spec.name] == spec.mode
        assert DeepSpaceToolPolicy.mode(spec.name) == spec.mode
    for name in registered - set(NATIVE_TOOL_HANDLERS):
        # The tools the package does not own keep the chat service's own
        # dispatcher, so they have no package handler.
        assert name in {
            "analyze",
            "artifact_create",
            "ask_user",
            "delete",
            "discover_tools",
            "edit",
            "find",
            "get_tool_result",
            "image_read",
            "read",
            "sandbox_execute",
            "todo_write",
            "url_read",
            "web_search",
            "write",
        }


def test_tool_argument_summary_keeps_nested_values_json_serializable() -> None:
    summary = summarize_tool_arguments(
        {"arguments": {"message": "MCP_E2E_OK", "flags": {"read_only"}}}
    )

    assert summary == {"arguments": {"message": "MCP_E2E_OK", "flags": "{'read_only'}"}}


def test_the_package_and_the_registry_agree_on_the_tool_names() -> None:
    package_names = {spec.name for spec in NATIVE_TOOL_SPECS}

    assert package_names == set(NATIVE_TOOL_HANDLERS)
    assert package_names <= set(NATIVE_TOOL_REGISTRY.names())


def test_mcp_namespaced_tools_pass_the_shared_pipeline_policy() -> None:
    policy = DeepSpaceToolPolicy()
    policy.set_mcp_tool_names({"mcp_server_echo"})

    decision = policy.before_tool("mcp_server_echo", {"message": "hello"})

    assert decision.allowed is True
    assert decision.mode == "read"


def test_conversation_mcp_binding_is_not_rejected_by_preset_allowlist() -> None:
    policy = DeepSpaceToolPolicy()
    policy.set_preset_rules({"read"})
    policy.set_mcp_tool_names({"mcp_server_echo"})

    decision = policy.before_tool("mcp_server_echo", {"message": "hello"})

    assert decision.allowed is True


def test_every_package_tool_validates_its_arguments() -> None:
    for spec in NATIVE_TOOL_SPECS:
        assert spec.arguments_model is not None
        assert spec.mode in {"read", "write", "interactive"}
        assert spec.definition["function"]["name"] == spec.name


def test_the_dispatch_leaves_a_foreign_tool_name_to_the_caller() -> None:
    context = _context(Path("/tmp"))

    assert asyncio.run(dispatch_native_tool("write", {}, context)) is None


def test_the_workspace_root_rejects_a_parent_directory_path() -> None:
    with pytest.raises(WorkspacePathError):
        normalize_relative_path("../outside.txt")


def test_the_workspace_root_rejects_an_absolute_path() -> None:
    with pytest.raises(WorkspacePathError):
        normalize_relative_path("/etc/passwd")


def test_a_resolved_path_never_leaves_the_workspace_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspacePathError):
        resolve_within_root(root, "../secret.txt")


def test_fs_write_then_read_round_trips_through_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path)

    written = asyncio.run(
        dispatch_native_tool(
            "fs_write",
            {"path": "notes/todo.md", "content": "first line\nsecond line\n"},
            context,
        )
    )
    read = asyncio.run(dispatch_native_tool("fs_read", {"path": "notes/todo.md"}, context))

    assert written is not None and written["status"] == "ok"
    assert read is not None and read["status"] == "ok"
    assert "first line" in str(read["content"])
    assert read["line_count"] == 2


def test_fs_read_reports_a_missing_file_without_failing(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = asyncio.run(dispatch_native_tool("fs_read", {"path": "absent.md"}, context))

    assert result is not None
    assert result["status"] == "not_found"


def test_fs_str_replace_refuses_an_ambiguous_match(tmp_path: Path) -> None:
    context = _context(tmp_path)
    asyncio.run(dispatch_native_tool("fs_write", {"path": "dup.txt", "content": "x\nx\n"}, context))

    result = asyncio.run(
        dispatch_native_tool("fs_str_replace", {"path": "dup.txt", "old_string": "x"}, context)
    )

    assert result is not None
    assert result["status"] == "ambiguous"
    assert result["occurrences"] == 2


def test_fs_grep_finds_matching_lines(tmp_path: Path) -> None:
    context = _context(tmp_path)
    asyncio.run(
        dispatch_native_tool(
            "fs_write", {"path": "code.py", "content": "needle here\nhay\n"}, context
        )
    )

    result = asyncio.run(dispatch_native_tool("fs_grep", {"pattern": "needle"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert result["match_count"] == 1
    assert result["matches"][0]["path"] == "code.py"


def test_fs_glob_lists_matching_paths(tmp_path: Path) -> None:
    context = _context(tmp_path)
    asyncio.run(dispatch_native_tool("fs_write", {"path": "src/app.ts", "content": "x"}, context))

    result = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "**/*.ts"}, context))

    assert result is not None
    assert "src/app.ts" in result["matches"]


def test_fs_glob_accepts_the_workspace_root_alias(tmp_path: Path) -> None:
    context = _context(tmp_path)
    asyncio.run(dispatch_native_tool("fs_write", {"path": "src/app.ts", "content": "x"}, context))

    result = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "*", "path": "."}, context))

    assert result is not None
    assert "src" in result["matches"]


def test_the_file_tool_arguments_refuse_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        FsReadArguments(path="a.md", unexpected="value")
    with pytest.raises(ValidationError):
        FsWriteArguments(path="a.md", content="x", mode="destroy")


def test_the_read_only_permission_mode_refuses_a_mutating_command() -> None:
    assert read_only_violation("rm -rf build") == "rm"
    assert read_only_violation("git commit -m x") == "git"
    assert read_only_violation("sed -i s/a/b/ file.txt") == "sed"
    assert read_only_violation("ls -la") is None
    assert read_only_violation("cat package.json") is None


def test_the_shell_tools_are_absent_when_the_deployment_switches_them_off() -> None:
    settings = _settings(Path("/tmp"), deepspace_shell_enabled=False)

    assert unavailable_tool_names(settings) == {"bash", "pwsh"}


def test_the_shell_tools_are_present_by_default() -> None:
    settings = _settings(Path("/tmp"))

    assert unavailable_tool_names(settings) == set()


def test_a_tool_call_with_a_wrong_argument_type_is_refused() -> None:
    with pytest.raises(NativeToolValidationError):
        NATIVE_TOOL_REGISTRY.validate("fs_read", {"path": 12})
