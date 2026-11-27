from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.deepspace.services import task_loop
from app.deepspace.services.tools import todo

pytestmark = pytest.mark.unit_no_db


@pytest.mark.asyncio
async def test_todo_tool_uses_the_authenticated_conversation_scope(monkeypatch) -> None:
    tenant_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    calls: list[tuple[str, dict[str, object]]] = []

    class _TaskStore:
        def __init__(self, _db: object) -> None:
            pass

        def read_tasks(self, **scope: object) -> list[dict[str, object]]:
            calls.append(("read_tasks", scope))
            return []

    monkeypatch.setattr(task_loop, "DeepSpaceTaskLoopStore", _TaskStore)
    context = SimpleNamespace(
        db=object(),
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=SimpleNamespace(),
    )

    result = await todo._todo_write({"operation": "read"}, context)

    assert result == {"status": "ok", "plan": [], "tasks": []}
    assert calls == [
        (
            "read_tasks",
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
            },
        )
    ]


@pytest.mark.asyncio
async def test_harness_todos_are_replaced_in_the_scoped_task_ledger(monkeypatch) -> None:
    tenant_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    calls: list[dict[str, object]] = []

    class _TaskStore:
        def __init__(self, _db: object) -> None:
            pass

        def replace_tasks(self, **kwargs: object) -> list[dict[str, object]]:
            calls.append(kwargs)
            return []

    monkeypatch.setattr(task_loop, "DeepSpaceTaskLoopStore", _TaskStore)
    context = SimpleNamespace(
        db=object(),
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=SimpleNamespace(deepspace_todo_allow_parallel_in_progress=False),
    )

    result = await todo._todo_write(
        {"todos": [{"content": "Inspect files", "status": "pending"}]},
        context,
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["pending"] == 1
    assert result["in_progress"] == 0
    assert result["completed"] == 0
    assert result["todos"] == [{"content": "Inspect files", "status": "pending"}]
    assert "Updated todo list:" in result["summary"]
    assert calls == [
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "tasks": [{"content": "Inspect files", "status": "pending"}],
            "allow_parallel_in_progress": False,
        }
    ]


@pytest.mark.asyncio
async def test_todo_write_accepts_operation_replace_with_todos(monkeypatch) -> None:
    tenant_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    calls: list[dict[str, object]] = []

    class _TaskStore:
        def __init__(self, _db: object) -> None:
            pass

        def replace_tasks(self, **kwargs: object) -> list[dict[str, object]]:
            calls.append(kwargs)
            return []

    monkeypatch.setattr(task_loop, "DeepSpaceTaskLoopStore", _TaskStore)
    payload = {
        "operation": "replace",
        "todos": [
            {"content": "Define scope", "status": "in_progress"},
            {"content": "Verify citations", "status": "pending"},
        ],
    }
    validated = todo.TodoWriteArguments.model_validate(payload)
    assert validated.todos is not None
    assert len(validated.todos) == 2

    context = SimpleNamespace(
        db=object(),
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=SimpleNamespace(deepspace_todo_allow_parallel_in_progress=True),
    )
    result = await todo._todo_write(payload, context)
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["pending"] == 1
    assert result["in_progress"] == 1
    assert result["completed"] == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_todo_write_schema_exposes_object_items_and_coerces_kv_task_strings() -> None:
    schema = todo.TODO_WRITE_TOOL.definition["function"]["parameters"]
    assert schema["properties"]["todos"]["items"]["type"] == "object"
    assert schema["properties"]["tasks"]["items"]["type"] == "object"
    assert "content" in schema["properties"]["todos"]["items"]["properties"]

    validated = todo.TodoWriteArguments.model_validate(
        {
            "operation": "replace",
            "tasks": [
                "id=research; task=Collect sources; success_criteria=5 sources; verification=Read full pages; dependencies=none",
                "id=compare; task=Compare models; success_criteria=Clear matrix; verification=Cross-check; dependencies=research",
            ],
        }
    )
    assert validated.tasks is not None
    assert len(validated.tasks) == 2
    assert validated.tasks[0]["id"] == "research"
    assert validated.tasks[0]["content"] == "Collect sources"
    assert validated.tasks[0]["dependencies"] == []
    assert validated.tasks[1]["dependencies"] == ["research"]
