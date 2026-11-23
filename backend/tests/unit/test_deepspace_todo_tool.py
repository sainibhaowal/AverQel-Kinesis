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

    assert result == {"status": "ok", "plan": []}
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

    assert result == {"status": "ok", "count": 1}
    assert calls == [
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "tasks": [{"content": "Inspect files", "status": "pending"}],
            "allow_parallel_in_progress": False,
        }
    ]
