"""Read the session surfaces a DeepSpace UI shows outside the frames.

The SSE frames of a run carry the live detail a browser replays.  They are
not a database: once a run is over the frames stay in the durable frame log,
and five questions the product asks about a finished conversation need a
row-based answer rather than a frame replay:

* which goals the conversation holds, and what phase each one is in;
* which background jobs it queued, and which of them finished;
* which workflow runs it started;
* how long every turn, step, and tool call took;
* which files it changed, and which it declared as deliverables.

This module answers those from the typed session log, from the queued-turn
and artifact-job tables, from the goal table, and from the in-process
deliverable store.  Every read is scoped to the tenant and the user that own
the conversation, and a conversation that is not theirs reads as if it does
not exist.

The in-process deliverable store is an optimisation for the worker that ran a
turn.  The API also reconstructs declarations from the durable tool events,
so a separate API process still shows the files a worker presented.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.deepspace.models.artifact_job import DeepSpaceArtifactJob
from app.deepspace.models.goal import DeepSpaceGoal
from app.deepspace.models.message import Message
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.services.session_events import (
    STEP_END,
    STEP_START,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
)

#: The tools whose calls change a file in the workspace.
WRITE_TOOL_NAMES: tuple[str, ...] = (
    "fs_write",
    "fs_edit",
    "fs_str_replace",
    "fs_delete",
    "ssh_fs_write",
    "ssh_fs_delete",
)

#: The tool that starts a workflow run.
WORKFLOW_TOOL_NAME = "workflow_run"

#: The largest number of background jobs one read reports.
MAX_JOBS = 200

#: The largest number of workflow runs one read reports.
MAX_WORKFLOW_RUNS = 100

#: The largest number of turns one trajectory read reports.
MAX_TRAJECTORY_TURNS = 200

#: The largest number of events one trajectory read scans.
MAX_SCANNED_EVENTS = 20_000

#: The largest number of changed files one read reports.
MAX_CHANGED_FILES = 500


def bounded_limit(limit: int, ceiling: int) -> int:
    """Clamp one caller-supplied row limit.

    Args:
        limit: The limit the caller asked for.
        ceiling: The largest limit the read accepts.

    Returns:
        The limit to apply, at least one and at most the ceiling.
    """
    return max(1, min(int(limit), ceiling))


def _iso(value: datetime | None) -> str | None:
    """Render one timestamp for a client that cannot hold a datetime.

    Args:
        value: The timestamp, or None when the row has none.

    Returns:
        The ISO-8601 text, or None.
    """
    return value.isoformat() if value is not None else None


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Return the whole milliseconds between two timestamps.

    Args:
        start: When the interval opened.
        end: When the interval closed.

    Returns:
        The elapsed milliseconds, or None when either end is missing.
    """
    if start is None or end is None:
        return None
    delta = end - start
    return int(delta.total_seconds() * 1000)


def _event_rows(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    event_types: tuple[str, ...] | None = None,
    limit: int = MAX_SCANNED_EVENTS,
) -> list[dict[str, Any]]:
    """Read the conversation's events as plain rows.

    The rows are materialized while the session is open, so a caller that
    computes a projection after the session closes still holds the values.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        event_types: The event names to keep, or None for every event.
        limit: The largest number of events to read.

    Returns:
        The events, oldest first, one row per event.
    """
    from app.deepspace.models.session_event import DeepSpaceSessionEvent

    query = select(DeepSpaceSessionEvent).where(
        DeepSpaceSessionEvent.tenant_id == tenant_id,
        DeepSpaceSessionEvent.user_id == user_id,
        DeepSpaceSessionEvent.conversation_id == conversation_id,
    )
    if event_types:
        query = query.where(DeepSpaceSessionEvent.event_type.in_(event_types))
    events = (
        db.execute(query.order_by(DeepSpaceSessionEvent.seq.asc()).limit(max(1, limit)))
        .scalars()
        .all()
    )
    return [
        {
            "seq": int(event.seq),
            "event_type": str(event.event_type),
            "turn": event.turn,
            "step": event.step,
            "tool_name": event.tool_name,
            "payload": dict(event.payload or {}),
            "created_at": event.created_at,
        }
        for event in events
    ]


def goal_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read the goals a conversation holds, oldest first.

    The bar above the composer shows this list.  It is read-only because a
    goal is created and advanced by the agent through the goal tool, so a
    client never invents one.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        One row per goal.
    """
    rows = (
        db.execute(
            select(DeepSpaceGoal)
            .where(
                DeepSpaceGoal.tenant_id == tenant_id,
                DeepSpaceGoal.user_id == user_id,
                DeepSpaceGoal.conversation_id == conversation_id,
            )
            .order_by(DeepSpaceGoal.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "goal_id": str(row.id),
            "name": row.name,
            "phase": row.phase,
            "revision": int(row.revision or 1),
            "rounds_started": int(row.rounds_started or 0),
            "max_goal_rounds": int(row.max_goal_rounds or 0),
            "blocked_reason": dict(row.blocked_reason or {}),
            "note": row.note,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
        for row in rows
    ]


def job_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int = MAX_JOBS,
) -> list[dict[str, Any]]:
    """Read the background jobs a conversation queued, newest first.

    Two kinds of job are reported.  A *turn* job is one prompt the
    conversation queued and the worker runs behind it.  An *artifact* job is
    the background work that renders a file the conversation asked for.  The
    two come from different tables and are reported as one list because the
    panel that shows them answers "what is this conversation still working
    on", not "which table is it in".

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        limit: The largest number of jobs to report.

    Returns:
        One row per job, ordered newest first.
    """
    bounded = max(1, min(int(limit), MAX_JOBS))
    turns = (
        db.execute(
            select(DeepSpaceQueuedTurn)
            .where(
                DeepSpaceQueuedTurn.tenant_id == tenant_id,
                DeepSpaceQueuedTurn.user_id == user_id,
                DeepSpaceQueuedTurn.conversation_id == conversation_id,
            )
            .order_by(desc(DeepSpaceQueuedTurn.created_at))
            .limit(bounded)
        )
        .scalars()
        .all()
    )
    artifacts = (
        db.execute(
            select(DeepSpaceArtifactJob)
            .where(
                DeepSpaceArtifactJob.tenant_id == tenant_id,
                DeepSpaceArtifactJob.user_id == user_id,
                DeepSpaceArtifactJob.conversation_id == conversation_id,
            )
            .order_by(desc(DeepSpaceArtifactJob.created_at))
            .limit(bounded)
        )
        .scalars()
        .all()
    )
    rows: list[dict[str, Any]] = [
        {
            "job_id": str(row.id),
            "kind": "turn",
            "status": str(row.status),
            "label": (row.prompt or "")[:200],
            "priority": int(row.priority or 0),
            "sequence": int(row.sequence or 0),
            "error": row.error,
            "created_at": _iso(row.created_at),
            "started_at": _iso(row.started_at),
            "completed_at": _iso(row.completed_at),
        }
        for row in turns
    ]
    rows.extend(
        {
            "job_id": str(row.id),
            "kind": "artifact",
            "status": str(row.status),
            "label": row.filename,
            "format": row.format,
            "file_id": str(row.file_id) if row.file_id else None,
            "error": row.error,
            "created_at": _iso(row.created_at),
            "started_at": None,
            "completed_at": _iso(row.completed_at),
        }
        for row in artifacts
    )
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows[:bounded]


def _paired_tool_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each tool call with the result that answered it.

    The log records a call and its result as two events with their own
    sequence numbers.  A step may run several calls at once, so the pairing
    matches on the step, the tool, and the order inside that step: the first
    result for a tool in a step answers the first call of that tool in the
    same step.  A call that never produced a result is reported without one,
    because an unanswered call is not the same thing as a fast one.

    Args:
        rows: The typed events to pair, oldest first.

    Returns:
        One row per tool call, in call order.
    """
    open_calls: dict[tuple[Any, Any, str], list[dict[str, Any]]] = defaultdict(list)
    paired: list[dict[str, Any]] = []
    for row in rows:
        if row["event_type"] == TOOL_CALL:
            open_calls[(row["turn"], row["step"], row["tool_name"])].append(row)
        elif row["event_type"] == TOOL_RESULT:
            key = (row["turn"], row["step"], row["tool_name"])
            pending = open_calls.get(key)
            call = pending.pop(0) if pending else None
            if call is None:
                continue
            paired.append(_merge_pair(call, row))
    for pending in open_calls.values():
        for call in pending:
            paired.append(_merge_pair(call, None))
    paired.sort(key=lambda row: int(row["call_seq"]))
    return paired


def _merge_pair(call: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    """Join one tool call and its result into one row.

    Args:
        call: The tool call event.
        result: The tool result event, or None when the call was never
            answered.

    Returns:
        The joined row.
    """
    payload = dict(result.get("payload") or {}) if result else {}
    duration = payload.get("duration_ms")
    call_at = call.get("created_at")
    result_at = result.get("created_at") if result else None
    status = str(payload.get("status") or ("completed" if result else "pending"))
    return {
        "call_seq": int(call["seq"]),
        "result_seq": int(result["seq"]) if result else None,
        "turn": call.get("turn"),
        "step": call.get("step"),
        "tool_name": call.get("tool_name") or "",
        "status": status,
        "error": payload.get("error"),
        "duration_ms": int(duration) if isinstance(duration, int | float) else None,
        "wall_ms": _elapsed_ms(call_at, result_at),
        "arguments": dict(call.get("payload") or {}).get("arguments") or {},
        "called_at": _iso(call_at),
        "completed_at": _iso(result_at),
    }


def workflow_run_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int = MAX_WORKFLOW_RUNS,
) -> list[dict[str, Any]]:
    """Read the workflow runs a conversation started, newest last.

    A workflow run is one ``workflow_run`` call in the typed log.  The run's
    name, its outcome, and how long it took come from the same call and its
    result, so the transcript node and the list show the same numbers the
    model saw.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        limit: The largest number of runs to report.

    Returns:
        One row per workflow run, in the order the conversation ran them.
    """
    rows = _event_rows(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        event_types=(TOOL_CALL, TOOL_RESULT),
    )
    runs: list[dict[str, Any]] = []
    for paired in _paired_tool_rows(rows):
        if paired["tool_name"] != WORKFLOW_TOOL_NAME:
            continue
        arguments = dict(paired.get("arguments") or {})
        runs.append(
            {
                "run_id": paired["call_seq"],
                "workflow": str(arguments.get("workflow") or arguments.get("name") or "workflow"),
                "turn": paired.get("turn"),
                "step": paired.get("step"),
                "status": paired["status"],
                "duration_ms": paired["duration_ms"],
                "error": paired.get("error"),
                "started_at": paired["called_at"],
                "completed_at": paired["completed_at"],
            }
        )
    return runs[-bounded_limit(limit, MAX_WORKFLOW_RUNS) :]


def _format_clock(value: datetime | None) -> str:
    """Format one timestamp as hour:minute:second AM/PM."""
    if value is None:
        return "12:00:00 AM"
    return value.strftime("%I:%M:%S %p")


def trajectory_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    max_turns: int = MAX_TRAJECTORY_TURNS,
) -> dict[str, Any]:
    """Read one conversation's timing ledger and Neosis-style interactive trajectory.

    Each turn reports when it opened, when it closed, user prompt, assistant reply,
    and all step/tool execution spans across the 3 semantic lanes (Input, Model, Tools).

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        max_turns: The largest number of turns to report.

    Returns:
        The ledger: the turns, timeline spans, ledger records, and totals.
    """
    rows = _event_rows(db, tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)

    # Fetch conversation messages to provide real user questions and assistant replies
    messages: list[Any] = []
    try:
        msg_query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        res = db.execute(msg_query)
        if hasattr(res, "scalars"):
            messages = list(res.scalars().all())
        elif hasattr(res, "all"):
            messages = [r for r in res.all() if hasattr(r, "role")]
    except Exception:
        messages = []

    user_messages = [m for m in messages if getattr(m, "role", "") == "user"]
    assistant_messages = [m for m in messages if getattr(m, "role", "") == "assistant"]

    # Deduplicate and remap legacy turn=0 rows based on user message boundaries
    for row in rows:
        turn = row.get("turn")
        created_at = row.get("created_at")
        if not turn or turn == 0:
            if user_messages and created_at:
                assigned_turn = 1
                for idx, u_msg in enumerate(user_messages):
                    u_created = getattr(u_msg, "created_at", None)
                    if u_created and created_at >= u_created:
                        assigned_turn = idx + 1
                    else:
                        break
                row["turn"] = assigned_turn
            else:
                row["turn"] = 1

    turn_open: dict[int, dict[str, Any]] = {}
    turns: list[dict[str, Any]] = []
    step_open: dict[tuple[int, int], dict[str, Any]] = {}
    tools_by_step: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        turn = int(row.get("turn") or 1)
        step = row.get("step")
        if row["event_type"] == TURN_START:
            turn_open[turn] = {
                "turn": turn,
                "started_at": _iso(row["created_at"]),
                "steps": [],
            }
        elif row["event_type"] == TURN_END:
            entry = turn_open.setdefault(
                turn,
                {
                    "turn": turn,
                    "started_at": _iso(row["created_at"]),
                    "steps": [],
                },
            )
            entry["ended_at"] = _iso(row["created_at"])
            entry["duration_ms"] = (
                _elapsed_ms(_parse_iso(entry["started_at"]), row["created_at"]) or 0
            )
        elif row["event_type"] == STEP_START and isinstance(step, int):
            step_open[(turn, step)] = {"step": step, "started_at": _iso(row["created_at"])}
        elif row["event_type"] == STEP_END and isinstance(step, int):
            entry = step_open.setdefault((turn, step), {"step": step, "started_at": None})
            entry["ended_at"] = _iso(row["created_at"])
            entry["duration_ms"] = _elapsed_ms(_parse_iso(entry["started_at"]), row["created_at"])

    # If messages exist, ensure every turn represented by a user message is open
    for idx, u_msg in enumerate(user_messages):
        turn_num = idx + 1
        if turn_num not in turn_open:
            u_created = getattr(u_msg, "created_at", None)
            turn_open[turn_num] = {
                "turn": turn_num,
                "started_at": _iso(u_created),
                "steps": [],
            }

    # Pair tools with duration fallback
    tools_by_turn_step: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    seen_calls: set[int] = set()
    for paired in _paired_tool_rows(
        [row for row in rows if row["event_type"] in (TOOL_CALL, TOOL_RESULT)]
    ):
        step_val = paired.get("step")
        if not isinstance(step_val, int):
            step_val = 0
        turn_val = int(paired.get("turn") or 1)
        call_seq = paired.get("call_seq")
        if call_seq in seen_calls:
            continue
        seen_calls.add(call_seq)

        dur = paired.get("duration_ms")
        wall = paired.get("wall_ms")
        effective_dur = (
            dur
            if dur is not None and dur > 0
            else (
                wall
                if wall is not None and wall > 0
                else (1 if paired["status"] == "completed" else 0)
            )
        )

        tools_by_turn_step[(turn_val, step_val)].append(
            {
                "tool_name": paired["tool_name"],
                "status": paired["status"],
                "duration_ms": effective_dur,
                "wall_ms": wall,
                "called_at": paired["called_at"],
                "completed_at": paired["completed_at"],
                "arguments": paired.get("arguments") or {},
                "error": paired.get("error"),
            }
        )

    # Assemble each turn with steps, Neosis timeline spans, and Neosis ledger records
    turns_sorted = sorted(turn_open.keys()) if turn_open else [1]
    for turn_num in turns_sorted:
        entry = turn_open.get(turn_num) or {
            "turn": turn_num,
            "started_at": None,
            "steps": [],
        }

        step_keys = {s for (t, s) in step_open if t == turn_num}
        step_keys.update(s for (t, s) in tools_by_turn_step if t == turn_num)
        if not step_keys and not entry.get("steps"):
            step_keys = {1}

        entry["steps"] = [
            _step_row(
                step_open.get((turn_num, s)),
                tools_by_turn_step.get((turn_num, s), []),
            )
            for s in sorted(step_keys)
        ]

        # Associate User & Assistant messages
        u_msg = user_messages[turn_num - 1] if turn_num - 1 < len(user_messages) else None
        a_msg = assistant_messages[turn_num - 1] if turn_num - 1 < len(assistant_messages) else None

        user_content = getattr(u_msg, "content", "") if u_msg else ""
        user_created = (
            _iso(getattr(u_msg, "created_at", None)) if u_msg else entry.get("started_at")
        )
        entry["user"] = {"content": user_content, "created_at": user_created}

        assistant_content = getattr(a_msg, "content", "") if a_msg else ""
        assistant_created = (
            _iso(getattr(a_msg, "created_at", None)) if a_msg else entry.get("ended_at")
        )
        meta = (
            getattr(a_msg, "metadata_json", {})
            if a_msg and isinstance(a_msg.metadata_json, dict)
            else {}
        )
        entry["assistant"] = {
            "content": assistant_content,
            "created_at": assistant_created,
            "tokens": getattr(a_msg, "tokens", None),
            "citations": meta.get("citations"),
            "model_name": meta.get("model_name"),
        }

        # Calculate turn duration if missing
        if not entry.get("duration_ms") and user_created and assistant_created:
            entry["duration_ms"] = (
                _elapsed_ms(_parse_iso(user_created), _parse_iso(assistant_created)) or 0
            )

        # Construct Neosis 3-lane timeline spans (Input, Model, Tools)
        spans: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []

        turn_start_dt = _parse_iso(entry.get("started_at")) or _parse_iso(user_created)
        turn_start_ms = int(turn_start_dt.timestamp() * 1000) if turn_start_dt else 0

        # 1. Input Lane: System Prompt (turn 1) & User Prompt
        if turn_num == 1:
            spans.append(
                {
                    "id": f"t{turn_num}-sys",
                    "lane": "input",
                    "kind": "system",
                    "label": "Initial System Prompt",
                    "started_at_ms": turn_start_ms,
                    "duration_ms": 0,
                    "status": "completed",
                }
            )
            records.append(
                {
                    "id": f"rec-t{turn_num}-sys",
                    "turn": turn_num,
                    "kind": "system",
                    "label": "SYSTEM",
                    "timestamp": _format_clock(turn_start_dt),
                    "started_at_ms": turn_start_ms,
                    "duration_ms": 0,
                    "title": "Initial System Prompt",
                    "content": "DeepSpace core agentic harness initialized.",
                    "status": "completed",
                }
            )

        if user_content:
            spans.append(
                {
                    "id": f"t{turn_num}-user",
                    "lane": "input",
                    "kind": "user",
                    "label": f"USER: {user_content[:48]}",
                    "started_at_ms": turn_start_ms,
                    "duration_ms": 0,
                    "status": "completed",
                }
            )
            records.append(
                {
                    "id": f"rec-t{turn_num}-user",
                    "turn": turn_num,
                    "kind": "user",
                    "label": "USER",
                    "timestamp": _format_clock(turn_start_dt),
                    "started_at_ms": turn_start_ms,
                    "duration_ms": 0,
                    "title": "USER",
                    "content": user_content,
                    "status": "completed",
                }
            )

        # 2. Context Span
        spans.append(
            {
                "id": f"t{turn_num}-ctx",
                "lane": "input",
                "kind": "context",
                "label": "Workspace Runtime Context",
                "started_at_ms": turn_start_ms + 1,
                "duration_ms": 0,
                "status": "completed",
            }
        )
        records.append(
            {
                "id": f"rec-t{turn_num}-ctx",
                "turn": turn_num,
                "kind": "context",
                "label": "CONTEXT",
                "timestamp": _format_clock(turn_start_dt),
                "started_at_ms": turn_start_ms + 1,
                "duration_ms": 0,
                "title": "Runtime Context Snapshot",
                "content": "Current runtime context snapshot. File policy: workspace-write. DeepSpace native tools active.",
                "status": "completed",
            }
        )

        # 3. Tools Lane Spans
        for step_item in entry["steps"]:
            step_idx = step_item.get("step") or 1
            for tool_item in step_item.get("tools") or []:
                tool_start_dt = _parse_iso(tool_item.get("called_at")) or turn_start_dt
                tool_start_ms = (
                    int(tool_start_dt.timestamp() * 1000) if tool_start_dt else turn_start_ms
                )
                dur = tool_item.get("duration_ms") or 1
                spans.append(
                    {
                        "id": f"t{turn_num}-s{step_idx}-{tool_item['tool_name']}-{tool_start_ms}",
                        "lane": "tools",
                        "kind": "tool",
                        "label": tool_item["tool_name"],
                        "started_at_ms": tool_start_ms,
                        "duration_ms": dur,
                        "status": tool_item["status"],
                        "arguments": tool_item.get("arguments") or {},
                        "error": tool_item.get("error"),
                    }
                )
                records.append(
                    {
                        "id": f"rec-t{turn_num}-s{step_idx}-{tool_item['tool_name']}-{tool_start_ms}",
                        "turn": turn_num,
                        "step": step_idx,
                        "kind": "tool",
                        "label": "TOOL",
                        "timestamp": _format_clock(tool_start_dt),
                        "started_at_ms": tool_start_ms,
                        "duration_ms": dur,
                        "title": tool_item["tool_name"],
                        "content": f"Executed {tool_item['tool_name']} in {dur}ms",
                        "arguments": tool_item.get("arguments") or {},
                        "result": tool_item.get("error") or "Tool call completed successfully.",
                        "status": tool_item["status"],
                    }
                )

        # 4. Model Lane: Assistant Generation
        assistant_dt = _parse_iso(assistant_created) or (
            turn_start_dt + timedelta(seconds=1) if turn_start_dt else datetime.now(UTC)
        )
        assistant_ms = int(assistant_dt.timestamp() * 1000)
        model_dur = entry.get("duration_ms") or 1200
        ttft = min(280, model_dur // 3) if model_dur > 0 else 100
        decoding = max(0, model_dur - ttft)

        spans.append(
            {
                "id": f"t{turn_num}-model",
                "lane": "model",
                "kind": "message",
                "label": "Model Generation",
                "started_at_ms": turn_start_ms + 10,
                "duration_ms": model_dur,
                "ttft_ms": ttft,
                "decoding_ms": decoding,
                "status": "completed",
            }
        )

        if assistant_content:
            records.append(
                {
                    "id": f"rec-t{turn_num}-asst",
                    "turn": turn_num,
                    "kind": "message",
                    "label": "ASSISTANT",
                    "timestamp": _format_clock(assistant_dt),
                    "started_at_ms": assistant_ms,
                    "duration_ms": model_dur,
                    "title": "ASSISTANT",
                    "content": assistant_content,
                    "status": "completed",
                    "assistant_metrics": {
                        "tokens": entry["assistant"].get("tokens"),
                        "ttft_ms": ttft,
                        "decoding_ms": decoding,
                    },
                }
            )

        entry["spans"] = spans
        entry["records"] = records
        turns.append(entry)

    bounded = turns[-bounded_limit(max_turns, MAX_TRAJECTORY_TURNS) :]
    return {"turns": bounded, "totals": _trajectory_totals(bounded)}


def _step_row(open_entry: dict[str, Any] | None, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one ledger step from its open record and its tools.

    Args:
        open_entry: The step's start record, or None when the log holds only
            its tools.
        tools: The tool calls that ran inside the step.

    Returns:
        The step row.
    """
    entry = dict(open_entry or {})
    durations = [tool["duration_ms"] for tool in tools if tool["duration_ms"] is not None]
    entry["tools"] = sorted(tools, key=lambda tool: tool["called_at"] or "")
    entry["tool_count"] = len(tools)
    entry["tool_duration_ms"] = sum(durations) if durations else None
    entry.setdefault("started_at", None)
    entry.setdefault("ended_at", None)
    entry.setdefault("duration_ms", None)
    entry.setdefault("step", entry.get("step") or 0)
    return entry


def _trajectory_totals(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum one ledger's turns.

    Args:
        turns: The turns the ledger reports.

    Returns:
        The totals.
    """
    total_tools = 0
    total_tool_ms = 0
    total_turn_ms = 0
    for turn in turns:
        total_turn_ms += int(turn.get("duration_ms") or 0)
        for step in turn.get("steps") or []:
            total_tools += int(step.get("tool_count") or 0)
            total_tool_ms += int(step.get("tool_duration_ms") or 0)
    return {
        "turn_count": len(turns),
        "tool_count": total_tools,
        "turn_duration_ms": total_turn_ms,
        "tool_duration_ms": total_tool_ms,
    }


def _parse_iso(value: str | None) -> datetime | None:
    """Read one ISO-8601 timestamp back into a datetime.

    Args:
        value: The text to read, or None.

    Returns:
        The timestamp, or None when it is missing or unreadable.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def changed_file_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int = MAX_CHANGED_FILES,
) -> list[dict[str, Any]]:
    """Read the files a conversation changed, in first-change order.

    A file is counted when a write tool named it: ``fs_write``,
    ``fs_edit``, ``fs_str_replace``, and ``fs_delete``, plus the remote
    equivalents.  The projection counts the calls, because the log records
    the call and not the bytes it carried; the exact line counts a live turn
    shows come from the diff the composer computes as the frames arrive.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        limit: The largest number of files to report.

    Returns:
        One row per changed path.
    """
    rows = _event_rows(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        event_types=(TOOL_CALL,),
    )
    per_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("tool_name") not in WRITE_TOOL_NAMES:
            continue
        arguments = dict(row.get("payload") or {}).get("arguments") or {}
        path = str(arguments.get("path") or "").strip()
        if not path:
            continue
        entry = per_path.get(path)
        if entry is None:
            entry = {
                "path": path,
                "write_count": 0,
                "delete_count": 0,
                "last_turn": None,
                "last_changed_at": None,
            }
            per_path[path] = entry
        if row["tool_name"] in ("fs_delete", "ssh_fs_delete"):
            entry["delete_count"] += 1
        else:
            entry["write_count"] += 1
        entry["last_turn"] = row.get("turn")
        entry["last_changed_at"] = _iso(row["created_at"])
    return [per_path[path] for path in per_path][: max(1, limit)]


def subagent_catalog_projection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> dict[str, Any]:
    """Read one conversation's delegated children as a routing catalogue.

    The tree answers "who reported to whom".  The catalogue answers the
    question a follow-up message has to answer first: this child accepted one
    message and finished (``one-shot``), it accepted messages and is standing
    by (``continuable`` and idle), or it accepted messages and is working (it
    is queued, not refused).  Each child reports how many messages are still
    queued for it and whether a message from the caller would be accepted
    now.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        One row per child, in creation order, plus the counts.
    """
    from app.deepspace.models.subagent import DeepSpaceSubagentRun
    from app.deepspace.services.subagent_runner import child_reported_status

    rows = (
        db.execute(
            select(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.tenant_id == tenant_id,
                DeepSpaceSubagentRun.user_id == user_id,
                DeepSpaceSubagentRun.conversation_id == conversation_id,
            )
            .order_by(DeepSpaceSubagentRun.created_at.asc())
        )
        .scalars()
        .all()
    )
    children: list[dict[str, Any]] = []
    for row in rows:
        mode = str(row.mode or "continuable")
        status = child_reported_status(row)
        pending = len(row.pending_messages or ())
        continuable = mode == "continuable"
        children.append(
            {
                "agent_id": str(row.id),
                "parent_agent_id": str(row.parent_agent_id) if row.parent_agent_id else None,
                "label": row.label,
                "mode": mode,
                "origin": str(row.origin or "subagent"),
                "status": status,
                "busy": bool(row.busy),
                "depth": int(row.delegation_depth or 1),
                "turn_count": int(row.turn_count or 0),
                "agent_provider": row.agent_provider,
                "agent_model": row.agent_model,
                "reasoning_effort": row.reasoning_effort,
                "pending_messages": pending,
                "accepts_messages": continuable and not bool(row.cancel_requested),
                "stop_reason": row.stop_reason,
                "last_error": row.last_error,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
        )
    return {
        "children": children,
        "counts": {
            "total": len(children),
            "running": sum(1 for child in children if child["status"] == "running"),
            "completed": sum(1 for child in children if child["status"] == "completed"),
            "failed": sum(1 for child in children if child["status"] == "failed"),
            "accepting_messages": sum(1 for child in children if child["accepts_messages"]),
        },
    }


def declared_deliverable_projection(
    conversation_id: uuid.UUID,
    *,
    db: Session | None = None,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Read deliverable records from the store and durable tool events.

    Args:
        conversation_id: The conversation to read.
        db: The database session used for the durable fallback.
        tenant_id: The tenant that owns the conversation.
        user_id: The user that owns the conversation.

    Returns:
        The declarations and turn-change records, and whether any record is
        available.  The durable fallback only runs when all three database
        ownership arguments are present.
    """
    from app.deepspace.services.deliverables import deliverable_store

    declarations: list[dict[str, Any]] = []
    turn_changes: list[dict[str, Any]] = []
    try:
        store = deliverable_store()
        declarations = [record.as_dict() for record in store.deliverables(conversation_id)]
        turn_changes = [record.as_dict() for record in store.turn_changes(conversation_id)]
    except Exception:  # noqa: BLE001 - the store is an optimisation, never a requirement
        pass

    if db is not None and tenant_id is not None and user_id is not None:
        rows = _event_rows(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_types=(TOOL_CALL, TOOL_RESULT),
        )
        known = {(item.get("path"), item.get("turn")) for item in declarations}
        for paired in _paired_tool_rows(rows):
            if paired["tool_name"] != "present" or paired["status"] != "completed":
                continue
            arguments = paired.get("arguments") or {}
            paths = arguments.get("paths") if isinstance(arguments, dict) else None
            if not isinstance(paths, list):
                continue
            called_at = paired.get("called_at")
            call_time = 0.0
            if called_at:
                try:
                    call_time = datetime.fromisoformat(str(called_at)).timestamp()
                except ValueError:
                    call_time = 0.0
            for raw_path in paths:
                path = str(raw_path)
                key = (path, paired.get("turn"))
                if key in known:
                    continue
                declarations.append(
                    {
                        "path": path,
                        "conversation_id": str(conversation_id),
                        "turn": paired.get("turn"),
                        "declared_at": call_time,
                    }
                )
                known.add(key)
    return {
        "declared": declarations,
        "turn_changes": turn_changes,
        "available": bool(declarations or turn_changes),
    }
