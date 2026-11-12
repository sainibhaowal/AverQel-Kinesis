"""Tests for the DeepSpace native tool package and the tools it owns."""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY, summarize_tool_arguments
from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL_NAMES
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.tool_registry import NativeToolValidationError
from app.deepspace.services.tools import (
    NATIVE_TOOL_HANDLERS,
    NATIVE_TOOL_MODES,
    NATIVE_TOOL_SPECS,
    dispatch_native_tool,
    fs_search,
    unavailable_tool_names,
)
from app.deepspace.services.tools import fs as fs_tools
from app.deepspace.services.tools.fs import FsReadArguments, FsWriteArguments
from app.deepspace.services.tools.shell import read_only_violation
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    normalize_relative_path,
    resolve_within_root,
)
from app.ingestion.services.extractors.base import ExtractionResult
from app.ingestion.services.extractors.router import SupportedFormat

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build a settings double for the workspace tools."""
    base = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_sandbox_enabled": False,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_shell_timeout_seconds": 10,
        "deepspace_shell_max_output_bytes": 65_536,
        "deepspace_subagent_max_depth": 1,
        "deepspace_subagent_max_children": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(tmp_path: Path, *, workspace_root: Path | None = None, **overrides: Any) -> Any:
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
        workspace_root=workspace_root,
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


def test_fs_read_extracts_an_authorized_binary_document(monkeypatch, tmp_path: Path) -> None:
    class _Reader:
        def __init__(self, _settings):
            pass

        def describe_supported_formats(self):
            return [
                SupportedFormat(
                    extension=".pdf",
                    category="pdf",
                    extraction_method="pdf_text",
                )
            ]

        def extract(self, **kwargs):
            assert kwargs["filename"] == "report.pdf"
            assert kwargs["tenant_id"] is not None
            return ExtractionResult(
                text="Revenue 42\nEarnings 7",
                page_count=1,
                extraction_method="pdf_text",
                coverage_score=1.0,
            )

    monkeypatch.setattr(fs_tools, "ExtractorRouter", _Reader)
    context = _context(tmp_path, workspace_root=tmp_path)
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.7\n\x00fixture")

    result = asyncio.run(dispatch_native_tool("fs_read", {"path": "report.pdf"}, context))

    assert result is not None
    assert result["status"] == "ok"
    assert "Revenue 42" in result["content"]
    assert result["extraction_method"] == "pdf_text"
    assert result["capability"]["available"] is True
    assert result["page_count"] == 1


def test_fs_read_reports_an_unsupported_binary_format_as_terminal(
    monkeypatch, tmp_path: Path
) -> None:
    class _Reader:
        def __init__(self, _settings):
            pass

        def describe_supported_formats(self):
            return [
                SupportedFormat(
                    extension=".pdf",
                    category="pdf",
                    extraction_method="pdf_text",
                )
            ]

        def extract(self, **_kwargs):
            raise ApiError(
                code="UNSUPPORTED_DOCUMENT_TYPE",
                message="Unsupported document type for parsing.",
                status_code=400,
                details={"supported_extensions": [".pdf"]},
            )

    monkeypatch.setattr(fs_tools, "ExtractorRouter", _Reader)
    context = _context(tmp_path, workspace_root=tmp_path)
    (tmp_path / "unknown.bin").write_bytes(b"\x00not-readable")

    result = asyncio.run(dispatch_native_tool("fs_read", {"path": "unknown.bin"}, context))

    assert result is not None
    assert result["status"] == "unreadable"
    assert result["capability_failure"] is True
    assert result["stop_turn"] is True
    assert result["no_retry"] is True
    assert result["retryable"] is False
    assert result["capability"]["supported_extensions"] == [".pdf"]


def test_fs_glob_stops_after_a_complete_exact_name_miss(tmp_path: Path) -> None:
    context = _context(tmp_path, workspace_root=tmp_path)

    result = asyncio.run(
        dispatch_native_tool("fs_glob", {"pattern": "Q4-FY23-Revenue-and-Earnings.pdf"}, context)
    )

    assert result is not None
    assert result["matches"] == []
    assert result["capability_failure"] is True
    assert result["stop_turn"] is True
    assert result["search_complete"] is True


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


def test_fs_glob_stops_at_the_entry_budget_and_marks_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    context = _context(tmp_path, workspace_root=workspace)
    for index in range(20):
        (workspace / f"entry-{index:02}.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(fs_search, "MAX_SEARCH_ENTRIES", 5)

    result = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "missing.pdf"}, context))

    assert result is not None
    assert result["matches"] == []
    assert result["scanned_entries"] == 5
    assert result["search_truncated"] is True
    assert result["truncated"] is True


def test_fs_grep_stops_at_the_entry_budget_and_marks_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    for index in range(20):
        (workspace / f"entry-{index:02}.txt").write_text("no match", encoding="utf-8")
    monkeypatch.setattr(fs_search, "MAX_SEARCH_ENTRIES", 5)
    context = _context(tmp_path, workspace_root=workspace)

    result = asyncio.run(dispatch_native_tool("fs_grep", {"pattern": "needle"}, context))

    assert result is not None
    assert result["match_count"] == 0
    assert result["scanned_entries"] == 5
    assert result["search_truncated"] is True


def test_full_access_exact_filename_uses_one_prioritized_home_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (home / ".cache").mkdir()
    target = downloads / "Q4-FY23-Revenue-and-Earnings.pdf"
    target.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(fs_search, "MAX_SEARCH_ENTRIES", 5)
    context = _context(
        workspace, workspace_root=workspace, deepspace_permission_mode="danger-full-access"
    )

    result = asyncio.run(
        dispatch_native_tool("fs_glob", {"pattern": "Q4-FY23-Revenue-and-Earnings.pdf"}, context)
    )

    assert result is not None
    assert result["matches"] == [str(target)]
    assert result["search_roots"] == [str(workspace.resolve()), str(home.resolve())]
    assert result["search_truncated"] is False
    assert result["scanned_entries"] == 3


def test_full_access_wildcard_without_path_remains_workspace_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "visible.pdf").write_text("workspace", encoding="utf-8")
    context = _context(
        workspace, workspace_root=workspace, deepspace_permission_mode="danger-full-access"
    )

    result = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "*.pdf"}, context))

    assert result is not None
    assert result["matches"] == ["visible.pdf"]
    assert result["search_roots"] == [str(workspace.resolve())]


def test_fs_glob_runs_its_filesystem_walk_off_the_async_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(
        fs_search,
        "_fs_glob_sync",
        lambda _arguments, _context: {"worker_thread": threading.get_ident()},
    )

    result = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "*.pdf"}, context))

    assert result is not None
    assert result["worker_thread"] != event_loop_thread


def test_fs_glob_skips_protected_system_trees_and_reports_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "proc"
    protected.mkdir()
    (protected / "hidden.pdf").write_text("not searched", encoding="utf-8")
    (tmp_path / "visible.pdf").write_text("searched", encoding="utf-8")
    monkeypatch.setattr(fs_search, "PROTECTED_HOST_SEARCH_ROOTS", (protected,))
    context = _context(tmp_path, deepspace_permission_mode="danger-full-access")

    result = asyncio.run(
        dispatch_native_tool(
            "fs_glob",
            {"pattern": "**/*.pdf", "path": str(tmp_path)},
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["matches"] == ["visible.pdf"]
    assert result["skipped_paths"] == [
        {"path": str(protected), "reason": f"protected system tree: {protected}"}
    ]


def test_host_search_continues_after_a_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked-storage"
    blocked.mkdir()
    (blocked / "unreadable.pdf").write_text("not searched", encoding="utf-8")
    (tmp_path / "available.pdf").write_text("searched", encoding="utf-8")
    real_scandir = fs_search.os.scandir

    def scandir(path: Any) -> Any:
        if Path(path) == blocked:
            raise PermissionError("permission denied")
        return real_scandir(path)

    monkeypatch.setattr(fs_search.os, "scandir", scandir)
    context = _context(tmp_path, deepspace_permission_mode="danger-full-access")

    glob_result = asyncio.run(
        dispatch_native_tool(
            "fs_glob",
            {"pattern": "**/*.pdf", "path": str(tmp_path)},
            context,
        )
    )
    grep_result = asyncio.run(
        dispatch_native_tool(
            "fs_grep",
            {"pattern": "searched", "path": str(tmp_path)},
            context,
        )
    )

    assert glob_result is not None
    assert glob_result["matches"] == ["available.pdf"]
    assert glob_result["skipped_paths"] == [
        {"path": str(blocked), "reason": "permission denied while reading directory"}
    ]
    assert grep_result is not None
    assert grep_result["match_count"] == 1
    assert grep_result["skipped_paths"] == [
        {"path": str(blocked), "reason": "permission denied while reading directory"}
    ]


@pytest.mark.parametrize("storage_name", ["mnt", "media"])
def test_full_access_searches_explicit_storage_root(tmp_path: Path, storage_name: str) -> None:
    storage = tmp_path / storage_name
    storage.mkdir()
    (storage / "report.pdf").write_text("report", encoding="utf-8")
    context = _context(tmp_path, deepspace_permission_mode="danger-full-access")

    result = asyncio.run(
        dispatch_native_tool(
            "fs_glob",
            {"pattern": "*.pdf", "path": str(storage)},
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["search_root"] == str(storage.resolve())
    assert result["matches"] == ["report.pdf"]
    assert result["skipped_paths"] == []


def test_full_access_requires_an_explicit_flag_for_a_whole_system_search(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, deepspace_permission_mode="danger-full-access")

    result = asyncio.run(
        dispatch_native_tool(
            "fs_glob",
            {"pattern": "**/*.pdf", "path": "/"},
            context,
        )
    )

    assert result["status"] == "error"
    assert result["message"] == "Scanning the entire host requires path='/' and entire_system=true."
    assert result["path"] == "/"
    assert result["capability_failure"] is True
    assert result["stop_turn"] is True


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

    assert {"bash", "pwsh", "sandbox_execute"} <= unavailable_tool_names(settings)


def test_optional_isolated_executor_is_hidden_by_default() -> None:
    settings = _settings(Path("/tmp"))

    assert "sandbox_execute" in unavailable_tool_names(settings)


def test_optional_isolated_executor_can_be_enabled_explicitly() -> None:
    settings = _settings(Path("/tmp"), deepspace_sandbox_enabled=True)

    assert "sandbox_execute" not in unavailable_tool_names(settings)


def test_a_disabled_terminal_setting_hides_and_refuses_all_terminal_tools(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, deepspace_terminal_enabled=False)
    terminal_tools = {
        "terminal_open",
        "terminal_read",
        "terminal_write",
        "terminal_signal",
        "terminal_close",
        "terminal_list",
        "terminal_job",
    }

    assert terminal_tools <= unavailable_tool_names(settings)

    result = asyncio.run(
        dispatch_native_tool(
            "terminal_open",
            {},
            _context(tmp_path, deepspace_terminal_enabled=False),
        )
    )

    assert result == {
        "status": "unavailable",
        "error_code": "FEATURE_DISABLED",
        "message": "The terminal_open tool is disabled by the deployment setting deepspace_terminal_enabled.",
    }


def test_a_tool_call_with_a_wrong_argument_type_is_refused() -> None:
    with pytest.raises(NativeToolValidationError):
        NATIVE_TOOL_REGISTRY.validate("fs_read", {"path": 12})
