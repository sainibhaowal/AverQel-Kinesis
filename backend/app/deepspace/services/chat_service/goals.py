"""Goals mixin for DeepSpaceChatService."""

from __future__ import annotations

import copy
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.deepspace.services.chat_service.utils import (
    NATIVE_TOOL_REGISTRY,
    _TurnState,
)

logger = logging.getLogger(__name__)


class _GoalsMixin:
    @classmethod
    def _should_resume_task_plan(cls, prompt: str, task_check: dict[str, Any]) -> bool:
        """Resume a saved plan only when the current message clearly continues it.

        A persisted task list is workspace state, not an instruction to run that
        task on every later message.  Explicit continuation language wins; a
        small amount of meaningful overlap supports natural follow-ups while
        keeping unrelated service requests independent.
        """

        if not task_check.get("task_count") or task_check.get("complete"):
            return False
        if cls._is_non_work_greeting(prompt):
            return False
        normalized = " ".join(prompt.casefold().split())
        if re.search(
            r"\b(?:continue|resume|carry on|pick up|keep working|finish|complete|proceed with)\b.*\b(?:task|plan|work|that|this)\b"
            r"|\b(?:next task|remaining tasks?|unfinished task|saved task|same task|previous task)\b",
            normalized,
        ):
            return True

        stop_words = {
            "about",
            "after",
            "again",
            "also",
            "and",
            "check",
            "do",
            "for",
            "from",
            "give",
            "into",
            "make",
            "please",
            "read",
            "save",
            "the",
            "this",
            "with",
        }
        prompt_terms = {
            term for term in re.findall(r"[a-z0-9]{4,}", normalized) if term not in stop_words
        }
        task_terms: set[str] = set()
        for task in task_check.get("tasks") or []:
            if not isinstance(task, dict) or task.get("status") == "completed":
                continue
            task_text = " ".join(
                str(task.get(field) or "") for field in ("content", "active_form")
            ).casefold()
            task_terms.update(
                term for term in re.findall(r"[a-z0-9]{4,}", task_text) if term not in stop_words
            )
        return len(prompt_terms.intersection(task_terms)) >= 2

    @classmethod
    def _allows_existing_task_state(cls, prompt: str) -> bool:
        """Allow task-ledger inspection only when the current request asks for it.

        A conversation-scoped task ledger is durable state, but it is not an
        instruction to execute on every later turn.  This gate keeps a fresh
        request from inheriting old todo reads/marks while preserving explicit
        task-management requests and the existing managed-task continuation
        path.
        """

        if cls._is_non_work_greeting(prompt):
            return False
        normalized = " ".join(prompt.casefold().split())
        return bool(
            re.search(
                r"\b(?:task|tasks|todo|todos|plan|plans|progress|checklist|work plan|saved work|unfinished)\b",
                normalized,
            )
        )

    @staticmethod
    def _next_actionable_task(task_check: dict[str, Any]) -> dict[str, Any] | None:
        tasks = task_check.get("tasks")
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if task.get("status") == "in_progress":
                return task
        completed_ids = {
            str(task.get("id"))
            for task in tasks
            if isinstance(task, dict) and task.get("status") == "completed"
        }
        for task in tasks:
            if not isinstance(task, dict) or task.get("status") != "pending":
                continue
            dependencies = task.get("dependencies")
            if not isinstance(dependencies, list) or all(
                str(dependency) in completed_ids for dependency in dependencies
            ):
                return task
        return None

    @classmethod
    def _task_lifecycle_stage(cls, task_check: dict[str, Any]) -> tuple[str, str | None]:
        if not task_check.get("task_count"):
            return "final", None
        if task_check.get("complete"):
            return "verify_final", None
        task = cls._next_actionable_task(task_check)
        if task is None:
            # There is no ready task left only when the persisted ledger is
            # terminal (for example, a task was blocked). The assistant must
            # report that real blocker instead of repeating task verification.
            return "final", None
        task_id = str(task.get("id") or "").strip() or None
        return ("work" if task.get("status") == "in_progress" else "start_task"), task_id

    @classmethod
    def _tools_for_task_lifecycle(
        cls,
        *,
        stage: str,
        all_tools: list[dict[str, Any]],
        has_work_evidence: bool = False,
    ) -> list[dict[str, Any]]:
        allowed_names: set[str]
        if stage == "read_plan":
            allowed_names = {"todo_write"}
        elif stage == "start_task":
            allowed_names = {"todo_write"}
        elif stage == "verify_task" or stage == "verify_final":
            allowed_names = {"todo_write"}
        elif stage == "final":
            # Completion is ordinary assistant text after the persisted ledger
            # has been checked; there is no extra terminal function call.
            allowed_names = set()
        else:
            # A task may need several dependent tools (for example search,
            # page reads, then artifact creation). Keep the authorized schemas
            # available until the model marks the task terminal; the backend
            # still verifies evidence.
            allowed_names = cls._tool_names(all_tools)
        selected_tools = [
            copy.deepcopy(item)
            for item in all_tools
            if isinstance(item.get("function"), dict)
            and str(item["function"].get("name") or "") in allowed_names
        ]
        stage_controls = {
            "read_plan": ("todo_write",),
            "start_task": ("todo_write",),
            "work": ("todo_write",),
            "verify_task": ("todo_write",),
            "verify_final": ("todo_write",),
        }.get(stage, ())
        selected_names = cls._tool_names(selected_tools)
        for name in stage_controls:
            if name not in selected_names:
                selected_tools.append(NATIVE_TOOL_REGISTRY.emit(name))
        todo_operation = {
            "read_plan": "read",
            "start_task": "update",
            "work": "update",
            "verify_task": "check",
            "verify_final": "check",
        }.get(stage)
        if todo_operation is not None:
            for index, item in enumerate(selected_tools):
                function = item.get("function")
                if not isinstance(function, dict) or function.get("name") != "todo_write":
                    continue
                parameters = function.get("parameters")
                if not isinstance(parameters, dict):
                    continue
                properties = parameters.get("properties")
                if not isinstance(properties, dict):
                    continue
                constrained_properties = {
                    **properties,
                    "operation": {"type": "string", "enum": [todo_operation]},
                }
                required = ["operation"]
                if todo_operation == "update":
                    constrained_properties["status"] = {
                        "type": "string",
                        "enum": (
                            ["in_progress"]
                            if stage == "start_task"
                            else (
                                ["completed", "blocked", "failed"]
                                if has_work_evidence
                                else ["blocked", "failed"]
                            )
                        ),
                    }
                    required.extend(["task_id", "status"])
                    if stage == "work":
                        required.append("evidence")
                selected_tools[index] = {
                    **item,
                    "function": {
                        **function,
                        "parameters": {
                            **parameters,
                            "properties": constrained_properties,
                            "required": required,
                        },
                    },
                }
        return selected_tools

    @classmethod
    def _tools_for_plan_mode(
        cls,
        *,
        all_tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Narrow the round's tools to the ones a planning turn may use.

        A planning turn reads and researches.  It does not change anything,
        so every write tool is withheld, and the plan tools are added so the
        agent can present its design and read the current one.

        Args:
            all_tools: The tools the turn could otherwise offer.

        Returns:
            The tools a planning turn may call.
        """
        allowed_names = {
            name
            for name in cls._tool_names(all_tools)
            if name in NATIVE_TOOL_REGISTRY.names()
            and NATIVE_TOOL_REGISTRY.spec(name).mode == "read"
        }
        allowed_names.add("get_tool_result")
        selected_tools = [
            copy.deepcopy(item)
            for item in all_tools
            if isinstance(item.get("function"), dict)
            and str(item["function"].get("name") or "") in allowed_names
        ]
        selected_names = cls._tool_names(selected_tools)
        for name in ("plan_submit", "plan_read"):
            if name not in selected_names:
                selected_tools.append(NATIVE_TOOL_REGISTRY.emit(name))
        return selected_tools

    @staticmethod
    def _task_lifecycle_instruction(*, stage: str, task_id: str | None) -> str:
        if stage == "read_plan":
            return "The plan was saved. Call todo_write with operation='read' and use the persisted task IDs and statuses."
        if stage == "start_task":
            return (
                f"Start the next ready task with todo_write operation='update', task_id {task_id!r}, "
                "and status 'in_progress'."
            )
        if stage == "work":
            return (
                f"Work on active task {task_id!r} using the authorized tools supplied for this turn. "
                "Continue through dependent work steps. Once the task is truly complete or blocked, "
                "use todo_write operation='update' with evidence; the runtime will verify the persisted ledger."
            )
        if stage == "verify_task":
            return "The current task was marked complete. Call todo_write with operation='check' to verify it and choose the next ready task."
        if stage == "verify_final":
            return "Call todo_write with operation='check'. Do not finalise until the persisted ledger confirms every task is complete."
        if stage == "final":
            return (
                "The persisted ledger was checked. Answer the user now in ordinary text. Clearly report "
                "any recorded blocker, and do not claim incomplete work succeeded."
            )
        return f"Work only on active task {task_id!r} and verify the result before marking it complete."

    async def _maybe_continue_goals(self, state: _TurnState) -> None:
        """Check for active goals and queue continuation rounds.

        This is called after each turn ends to see if any goals should continue.
        """
        if not getattr(self, "goal_driver", None):
            return
        if not getattr(self.settings, "deepspace_goal_driver_enabled", True):
            return

        from app.deepspace.models.goal import DeepSpaceGoal

        db = self.db
        if db is None:
            return

        try:
            goals = (
                db.execute(
                    select(DeepSpaceGoal).where(
                        DeepSpaceGoal.tenant_id == state.auth.tenant_id,
                        DeepSpaceGoal.user_id == state.auth.user_id,
                        DeepSpaceGoal.conversation_id == state.conversation_id,
                        DeepSpaceGoal.phase == "active",
                    )
                )
                .scalars()
                .all()
            )

            for goal in goals:
                # Use a deterministic agent_id based on conversation for goal tracking
                # In a multi-agent setup, this would be the actual agent ID
                agent_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"goal-{state.conversation_id}")

                await self.goal_driver.maybe_continue_goal(
                    conversation_id=state.conversation_id,
                    goal=goal,
                    agent_id=agent_id,
                    auth=state.auth,
                    db=db,
                )
        except Exception:  # noqa: BLE001
            logger.debug("Goal continuation check failed", exc_info=True)
