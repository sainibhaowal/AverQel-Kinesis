"""Todo/task plan tool for DeepSpace.

Manages the conversation's task plan with two input shapes:
1. Harness-compatible whole-list snapshot (todos)
2. Durable ledger operations (replace/read/update/check)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    string_property,
)

# ============================================================
# Argument Models
# ============================================================


class HarnessTodoItem(ToolArguments):
    """One entry in the Harness-compatible whole-list todo snapshot."""

    content: str = Field(min_length=1, max_length=1_000)
    status: Literal["pending", "in_progress", "completed"]


class TodoWriteArguments(ToolArguments):
    """The todo_write tool with two accepted input shapes."""

    operation: Literal["replace", "read", "update", "check"] | None = None
    tasks: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=40)
    todos: list[HarnessTodoItem] | None = Field(default=None, min_length=1, max_length=40)
    task_id: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["pending", "in_progress", "completed", "blocked", "failed"] | None = None
    evidence: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_operation(self) -> TodoWriteArguments:
        if self.todos is not None:
            if self.operation is not None or self.tasks is not None:
                raise ValueError(
                    "todo_write accepts either a todos list or a ledger operation, not both"
                )
            contents = [item.content.strip() for item in self.todos]
            if len(contents) != len(set(contents)):
                raise ValueError("todos must not repeat a content line")
            return self

        if self.operation is None:
            raise ValueError("todo_write requires either a todos list or an operation")
        if self.operation in ("read", "check"):
            return self
        if self.operation == "update":
            if not self.task_id or not self.status:
                raise ValueError("todo_write(update) requires task_id and status")
            if (
                self.status in {"completed", "blocked", "failed"}
                and not str(self.evidence or "").strip()
            ):
                raise ValueError("terminal todo_write(update) requires evidence")
            return self
        if not self.tasks:
            raise ValueError("todo_write(replace) requires a tasks array")
        if len(self.tasks) <= 1:
            return self

        task_ids = [str(task.get("id", "")).strip() for task in self.tasks]
        if not all(task_ids):
            raise ValueError("multi-step plans require an id for every task")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("multi-step plans require unique task ids")
        for task, task_id in zip(self.tasks, task_ids, strict=True):
            if not str(task.get("success_criteria", "")).strip():
                raise ValueError(f"task {task_id!r} requires success_criteria")
            if not str(task.get("verification", "")).strip():
                raise ValueError(f"task {task_id!r} requires verification")
            dependencies = [
                str(item).strip() for item in (task.get("dependencies") or []) if str(item).strip()
            ]
            if task_id in dependencies:
                raise ValueError(f"task {task_id!r} cannot depend on itself")
            unknown = [item for item in dependencies if item not in task_ids]
            if unknown:
                raise ValueError(f"task {task_id!r} has unknown dependencies")

        dependencies_by_task = {
            task_id: [
                str(item).strip() for item in (task.get("dependencies") or []) if str(item).strip()
            ]
            for task, task_id in zip(self.tasks, task_ids, strict=True)
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError("task dependencies contain a cycle")
            visiting.add(task_id)
            for dependency in dependencies_by_task[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


# ============================================================
# Handler
# ============================================================


async def _todo_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Manage the conversation's task plan."""
    from sqlalchemy.orm import Session

    from app.deepspace.services.task_store import TaskStore

    db: Session | None = context.db
    if db is None:
        return {
            "status": "error",
            "error_code": "NO_DATABASE",
            "message": "Task store requires database session",
        }

    task_store = TaskStore(db)
    operation = arguments.get("operation")

    try:
        if arguments.get("todos") is not None:
            # Harness whole-list form
            todos = [
                {"content": item.content, "status": item.status} for item in arguments["todos"]
            ]
            task_store.replace_todos(context.conversation_id, todos)
            return {"status": "ok", "count": len(todos)}

        if operation == "replace":
            tasks = arguments["tasks"]
            task_store.replace_plan(context.conversation_id, tasks)
            return {"status": "ok", "count": len(tasks)}

        if operation == "read":
            plan = task_store.get_plan(context.conversation_id)
            return {"status": "ok", "plan": plan}

        if operation == "update":
            task_id = arguments["task_id"]
            status = arguments["status"]
            evidence = arguments.get("evidence", "")
            task_store.update_task(context.conversation_id, task_id, status, evidence)
            return {"status": "ok", "task_id": task_id}

        if operation == "check":
            issues = task_store.verify_plan(context.conversation_id)
            return {"status": "ok", "issues": issues}

        return {
            "status": "error",
            "error_code": "INVALID_OPERATION",
            "message": f"Unknown operation: {operation}",
        }

    except Exception as e:
        return {"status": "error", "error_code": "TODO_WRITE_FAILED", "message": str(e)}


# ============================================================
# Tool Spec
# ============================================================

TODO_WRITE_TOOL = define_native_tool(
    name="todo_write",
    description=(
        "Manage the conversation's task plan. Supply a todos list to replace the visible plan "
        "with the Harness-compatible whole-list snapshot; each item is {content, status} with "
        "status pending, in_progress, or completed. Use the durable ledger operations instead "
        "when the plan needs ids, dependencies, and evidence: replace creates or edits the "
        "ordered plan, read retrieves tasks, update starts or marks one task with evidence, "
        "and check verifies dependencies, blockers, and completion. Multi-step replacement "
        "plans need task ids, success criteria, verification, and real dependencies."
    ),
    properties={
        "operation": string_property(
            "The ledger operation to perform.",
            enum=["replace", "read", "update", "check"],
        ),
        "tasks": array_property(
            "Full task list for replace operation.",
            max_items=40,
        ),
        "todos": array_property(
            "Whole-list replacement plan (Harness format).",
            max_items=40,
        ),
        "task_id": string_property("Task ID for update operation.", max_length=120),
        "status": string_property(
            "Task status for update operation.",
            enum=["pending", "in_progress", "completed", "blocked", "failed"],
        ),
        "evidence": string_property("Completion evidence for terminal status.", max_length=1_000),
    },
    required=[],
    arguments_model=TodoWriteArguments,
    mode="write",
    handler=_todo_write,
)

TODO_TOOLS: list[NativeToolSpec] = [TODO_WRITE_TOOL]

__all__ = ["TODO_TOOLS", "TODO_WRITE_TOOL", "TodoWriteArguments", "HarnessTodoItem", "_todo_write"]
