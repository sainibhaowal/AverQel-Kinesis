"""The iterative runner for DeepSpace.

Ralph runs a fixed script over a fresh agent, round after round, until the
agent reports that it is done or the round budget is spent.  Each round is a
new child, so a round cannot be influenced by the previous round's context
except through the output the runner carries forward.

The script lives in the project workspace, and the runner refuses to start
without one, so the tool is never a way to run arbitrary repeated work by
accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from app.deepspace.services.subagent_runner import new_child_record, run_child_turn
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)

#: The largest script one call may read, in characters.
MAX_SCRIPT_CHARS = 100_000

#: The largest number of rounds one call may spend.
MAX_ROUNDS = 64

#: The phrase an agent uses to report that it finished.
DONE_MARKER = "RALPH_DONE"


class RalphRunArguments(ToolArguments):
    """Run a script over repeated fresh agents."""

    script: str = Field(min_length=1, max_length=1024)
    task: str | None = Field(default=None, max_length=20_000)
    max_rounds: int | None = Field(default=None, ge=1, le=MAX_ROUNDS)


def _load_script(context: ToolContext, raw: str) -> tuple[str, str]:
    """Read a script from the workspace.

    Args:
        context: The calling request's tool context.
        raw: The workspace-relative script path.

    Returns:
        The script text and its workspace-relative path.

    Raises:
        ValueError: When the script is missing or unreadable.
    """
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    try:
        resolved = resolve_within_root(root, raw)
    except WorkspacePathError as exc:
        raise ValueError(str(exc)) from exc
    document: Path = resolved.absolute
    if not document.is_file():
        raise ValueError(f"No script file named '{raw}' exists in the project workspace.")
    text = document.read_text(encoding="utf-8", errors="replace")[:MAX_SCRIPT_CHARS]
    if not text.strip():
        raise ValueError(f"The script file '{raw}' is empty.")
    return text, resolved.relative


async def _ralph_run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a script over repeated fresh agents."""
    try:
        script, relative = _load_script(context, str(arguments["script"]))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    budget = min(MAX_ROUNDS, int(arguments.get("max_rounds") or 8))
    task = str(arguments.get("task") or "").strip()
    rounds: list[dict[str, Any]] = []
    previous = ""
    for index in range(budget):
        if context.cancel_check is not None and context.cancel_check():
            return {
                "status": "cancelled",
                "script": relative,
                "rounds": rounds,
                "message": "The iteration was cancelled.",
            }
        prompt = script
        if task:
            prompt = f"{script}\n\nThe task:\n{task}"
        if previous:
            prompt = (
                f"{prompt}\n\nThe previous round reported:\n{previous}\n\n"
                "Continue from that report. When the work is finished, answer with "
                f"{DONE_MARKER}."
            )
        child = new_child_record(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            parent_run_id=context.run_id,
            parent_agent_id=context.parent_agent_id,
            origin="subagent",
            mode="one-shot",
            label=f"ralph:{relative}",
            delegation_depth=context.depth + 1,
            agent_provider=None,
            agent_model=None,
            reasoning_effort=None,
            persona=None,
            tool_filter={},
            inherited_history=None,
        )
        require_db(context).add(child)
        require_db(context).commit()
        outcome = await run_child_turn(context=context, child=child, task=prompt)
        child.result_text = outcome.text
        child.stop_reason = outcome.stop_reason
        child.turn_count = 1
        child.status = "idle" if outcome.stop_reason == "completed" else outcome.stop_reason
        child.last_error = outcome.diagnostic
        require_db(context).commit()
        previous = outcome.text
        rounds.append(
            {
                "round": index + 1,
                "agent_id": str(child.id),
                "stop_reason": outcome.stop_reason,
                "output": outcome.text,
                "diagnostic": outcome.diagnostic,
            }
        )
        if outcome.stop_reason != "completed":
            return {
                "status": "failed",
                "script": relative,
                "rounds": rounds,
                "message": f"Round {index + 1} did not complete cleanly.",
            }
        if DONE_MARKER in outcome.text:
            return {
                "status": "ok",
                "script": relative,
                "rounds": rounds,
                "round_count": len(rounds),
                "message": "The agent reported that the work is finished.",
            }
    return {
        "status": "ok",
        "script": relative,
        "rounds": rounds,
        "round_count": len(rounds),
        "message": f"The iteration used its budget of {budget} rounds without a completion report.",
    }


RALPH_DESCRIPTION = (
    "Run a script from the project workspace over repeated fresh agents until "
    f"one reports {DONE_MARKER} or the round budget is spent. Each round is a "
    "new agent that receives the previous round's report."
)

RALPH_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="ralph_run",
        description=RALPH_DESCRIPTION,
        properties={
            "script": string_property(
                "The workspace-relative path of the script to run.",
                min_length=1,
                max_length=1024,
            ),
            "task": string_property("The task the script works toward.", max_length=20_000),
            "max_rounds": integer_property(
                "The largest number of rounds to spend.", minimum=1, maximum=MAX_ROUNDS
            ),
        },
        required=["script"],
        arguments_model=RalphRunArguments,
        mode="write",
        handler=_ralph_run,
    ),
]

__all__ = ["RALPH_TOOLS", "RalphRunArguments"]
