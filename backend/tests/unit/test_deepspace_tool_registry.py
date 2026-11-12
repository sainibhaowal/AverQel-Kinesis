from __future__ import annotations

import pytest

from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY
from app.deepspace.services.native_tool_broker import NativeToolBroker
from app.deepspace.services.tool_registry import NativeToolValidationError


def test_registry_contains_dynamic_tools_and_unifies_document_operations() -> None:
    assert NATIVE_TOOL_REGISTRY.contains("web_search")
    assert NATIVE_TOOL_REGISTRY.contains("discover_tools")
    assert NATIVE_TOOL_REGISTRY.contains("read")
    assert NATIVE_TOOL_REGISTRY.contains("analyze")
    assert not NATIVE_TOOL_REGISTRY.contains("document_read")
    assert not NATIVE_TOOL_REGISTRY.contains("document_query")
    assert not NATIVE_TOOL_REGISTRY.contains("document_compare")
    assert not NATIVE_TOOL_REGISTRY.contains("todo_read")
    assert not NATIVE_TOOL_REGISTRY.contains("todo_mark")
    assert not NATIVE_TOOL_REGISTRY.contains("todo_check")
    assert not NATIVE_TOOL_REGISTRY.contains("observe")
    assert not NATIVE_TOOL_REGISTRY.contains("final")


def test_catalog_pages_reach_team_job_workflow_and_delivery_tools() -> None:
    authorized = set(NATIVE_TOOL_REGISTRY.names()) - {"discover_tools", "get_tool_result"}
    broker = NativeToolBroker()
    discovered: list[str] = []
    offset = 0
    first_page_names: set[str] = set()

    while True:
        page = broker.discover(
            query="inspect DeepSpace capabilities",
            registry=NATIVE_TOOL_REGISTRY,
            allowed_names=authorized,
            offset=offset,
        )
        assert page["status"] == "catalog"
        page_names = {item["name"] for item in page["available_tools"]}
        if offset == 0:
            first_page_names = page_names
        discovered.extend(item["name"] for item in page["available_tools"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert len(discovered) == len(set(discovered))
    assert set(discovered) == authorized
    assert {
        "spawn_teammate",
        "team_task_update",
        "job_output",
        "workflow_run",
        "present",
    } <= set(discovered)
    assert (
        not {
            "spawn_teammate",
            "team_task_update",
            "job_output",
            "workflow_run",
            "present",
        }
        & first_page_names
    )

    refused = broker.discover(
        query="update a team task",
        registry=NATIVE_TOOL_REGISTRY,
        allowed_names={"fs_glob"},
        tool_names=["team_task_update"],
    )
    assert refused["status"] == "invalid_selection"
    assert refused["loaded_tools"] == []


def test_registry_emission_is_defensive_and_schema_versioned() -> None:
    first = NATIVE_TOOL_REGISTRY.emit("web_search")
    second = NATIVE_TOOL_REGISTRY.emit("web_search")

    first["function"]["parameters"]["properties"]["query"]["maxLength"] = 1

    assert second["function"]["parameters"]["properties"]["query"]["maxLength"] == 512
    assert len(NATIVE_TOOL_REGISTRY.spec("web_search").schema_digest) == 24


def test_registry_normalizes_valid_arguments_without_extra_fields() -> None:
    arguments = NATIVE_TOOL_REGISTRY.validate(
        "web_search", {"query": "provider caching", "max_results": 4}
    )

    assert arguments == {"query": "provider caching", "max_results": 4}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("web_search", {"query": "x"}),
        ("sandbox_execute", {"language": "ruby", "code": "puts 1"}),
        ("todo_write", {"operation": "update", "task_id": "t1", "status": "made_up"}),
        ("read", {"target": "host_filesystem"}),
        ("todo_write", {"operation": "update", "task_id": "t1"}),
        ("analyze", {"operation": "compare_documents", "left_file_id": "only-one"}),
        ("write", {"target": "library", "unexpected": "field"}),
    ],
)
def test_registry_rejects_invalid_or_unexpected_native_arguments(
    name: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(NativeToolValidationError):
        NATIVE_TOOL_REGISTRY.validate(name, arguments)


def test_unified_read_and_analyze_validate_library_operations() -> None:
    read_arguments = NATIVE_TOOL_REGISTRY.validate(
        "read",
        {"target": "library", "operation": "query", "query": "Q4 revenue", "limit": 3},
    )
    analyze_arguments = NATIVE_TOOL_REGISTRY.validate(
        "analyze",
        {"operation": "compare_documents", "file_ids": ["file-a", "file-b"]},
    )

    assert read_arguments["operation"] == "query"
    assert read_arguments["target"] == "library"
    assert analyze_arguments["file_ids"] == ["file-a", "file-b"]


def test_unified_workspace_read_requires_a_path_without_changing_other_targets() -> None:
    with pytest.raises(NativeToolValidationError, match="requires a non-empty path"):
        NATIVE_TOOL_REGISTRY.validate("read", {"target": "workspace"})
    with pytest.raises(NativeToolValidationError, match="requires a non-empty path"):
        NATIVE_TOOL_REGISTRY.validate("read", {"target": "workspace", "path": "  "})

    workspace_read = NATIVE_TOOL_REGISTRY.validate(
        "read", {"target": "workspace", "path": "reports/q4.pdf"}
    )
    library_listing = NATIVE_TOOL_REGISTRY.validate("read", {"target": "library"})
    note_read = NATIVE_TOOL_REGISTRY.validate("read", {"target": "note"})

    assert workspace_read["path"] == "reports/q4.pdf"
    assert library_listing == {"target": "library", "operation": "read"}
    assert note_read == {"target": "note", "operation": "read"}


def test_unified_read_schema_marks_workspace_path_as_required() -> None:
    schema = NATIVE_TOOL_REGISTRY.emit("read")["function"]

    assert "path is required" in schema["description"]
    assert (
        "Required when target='workspace'"
        in schema["parameters"]["properties"]["path"]["description"]
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///home/AverQel/Downloads/Q4-FY23-Revenue-and-Earnings.pdf",
        "javascript:alert(1)",
        "ftp://example.test/document.pdf",
    ],
)
def test_browser_navigate_rejects_non_web_urls_during_tool_validation(url: str) -> None:
    with pytest.raises(NativeToolValidationError, match="web URLs only"):
        NATIVE_TOOL_REGISTRY.validate("browser_navigate", {"url": url})


@pytest.mark.parametrize("url", ["https://example.test/report.pdf", "localhost:3000"])
def test_browser_navigate_keeps_supported_web_urls(url: str) -> None:
    assert NATIVE_TOOL_REGISTRY.validate("browser_navigate", {"url": url})["url"] == url


def test_browser_navigate_schema_explains_that_local_files_are_not_urls() -> None:
    schema = NATIVE_TOOL_REGISTRY.emit("browser_navigate")["function"]

    assert "HTTP(S) web URL" in schema["description"]
    assert "Never use file://" in schema["parameters"]["properties"]["url"]["description"]


def test_multi_step_plans_require_executable_task_contracts() -> None:
    with pytest.raises(NativeToolValidationError, match="success_criteria"):
        NATIVE_TOOL_REGISTRY.validate(
            "todo_write",
            {
                "operation": "replace",
                "tasks": [
                    {"id": "research", "content": "Research sources"},
                    {
                        "id": "write",
                        "content": "Write report",
                        "verification": "Inspect the report",
                    },
                ],
            },
        )

    plan = NATIVE_TOOL_REGISTRY.validate(
        "todo_write",
        {
            "operation": "replace",
            "tasks": [
                {
                    "id": "research",
                    "content": "Research sources",
                    "success_criteria": "Primary sources are recorded",
                    "verification": "Review source URLs and notes",
                },
                {
                    "id": "write",
                    "content": "Write report",
                    "success_criteria": "The report is saved",
                    "verification": "Read the saved artifact",
                    "dependencies": ["research"],
                },
            ],
        },
    )

    assert plan["tasks"][1]["dependencies"] == ["research"]


def test_todo_write_unifies_read_update_and_completion_check() -> None:
    assert NATIVE_TOOL_REGISTRY.validate("todo_write", {"operation": "read"}) == {
        "operation": "read"
    }
    update = NATIVE_TOOL_REGISTRY.validate(
        "todo_write",
        {
            "operation": "update",
            "task_id": "research",
            "status": "completed",
            "evidence": "Primary sources saved to the artifact.",
        },
    )
    assert update["status"] == "completed"
    assert (
        NATIVE_TOOL_REGISTRY.validate("todo_write", {"operation": "check"})["operation"] == "check"
    )


def test_discovery_contract_supports_catalog_then_exact_schema_selection() -> None:
    arguments = NATIVE_TOOL_REGISTRY.validate(
        "discover_tools",
        {"query": "compare the two selected reports", "tool_names": ["analyze", "read"]},
    )

    assert arguments == {
        "query": "compare the two selected reports",
        "tool_names": ["analyze", "read"],
    }

    with pytest.raises(NativeToolValidationError):
        NATIVE_TOOL_REGISTRY.validate("discover_tools", {"query": "compare reports", "limit": 3})

    page = NATIVE_TOOL_REGISTRY.validate(
        "discover_tools", {"query": "find team tools", "offset": 40}
    )
    assert page == {"query": "find team tools", "offset": 40}
    with pytest.raises(NativeToolValidationError):
        NATIVE_TOOL_REGISTRY.validate("discover_tools", {"query": "find team tools", "offset": -1})
