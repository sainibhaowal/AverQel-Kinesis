"""Turn tool_calls phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from app.deepspace.services.chat_service.turns.tool_call_validation import _ToolCallValidationMixin
from app.deepspace.services.chat_service.utils import (
    CLARIFICATION_TOOL_NAMES,
    MAX_CONNECTED_TOOL_RECOVERY_RETRIES,
    MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL,
    MAX_EMPTY_PROVIDER_RETRIES,
    MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN,
    MAX_TOOL_RETRIES,
    NATIVE_TOOL_REGISTRY,
    DeepSpaceEmptyResponseError,
    DeepSpaceToolExecutionError,
    _capability_failure_message,
    _TurnState,
    sse,
)
from app.deepspace.services.mcp_tool_broker import (
    MCP_BROKER_TOOL_NAMES,
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
)
from app.deepspace.services.runtime_transitions import RuntimePhase, transition_checkpoint
from app.deepspace.services.session_events import (
    STEP_END,
)
from app.deepspace.services.spill_policy import take_recovered_images
from app.providers.services.reasoning_capabilities import (
    is_deepseek_v4_model,
    uses_deepseek_reasoning_protocol,
)
from app.system.services.metrics_service import (
    DEEPSPACE_EXECUTION_BATCHES_TOTAL,
    DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL,
    DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL,
)

logger = logging.getLogger(__name__)


class _TurnToolCallsMixin(_ToolCallValidationMixin):
    def _request_model_final_answer_without_tools(
        self,
        state: _TurnState,
        *,
        reason: str,
    ) -> None:
        """Stop further tool execution and let the model write the final answer."""
        state.disable_tools_for_final_answer = True
        state.terminal_status = "blocked"
        self._set_dynamic_system_instruction(
            state.conversation_messages,
            key="stop_tools_final_answer",
            content=(
                "Tool execution has been stopped for this turn because: "
                f"{reason} "
                "Do not call any more tools. Respond directly to the user using only the "
                "tool results and errors already observed above, explaining what could or "
                "could not be accessed and the exact next step the user can take."
            ),
        )

    @staticmethod
    def _record_subagent_activity(state: _TurnState, payload: dict[str, Any]) -> None:
        """Record a normalized live subagent timeline event onto turn state."""
        subagent_id = str(payload.get("subagent_id") or "").strip()
        if not subagent_id:
            return
        timelines: dict[str, dict[str, Any]] = state.subagent_timelines
        entry = timelines.get(subagent_id)
        now_iso = datetime.now(UTC).isoformat()
        if entry is None:
            entry = {
                "id": subagent_id,
                "parentToolId": str(payload.get("parent_tool_id") or "").strip() or None,
                "label": str(payload.get("label") or "").strip()
                or f"Subagent {len(timelines) + 1}",
                "mode": str(payload.get("mode") or "").strip() or "one-shot",
                "mission": str(payload.get("mission") or "").strip() or None,
                "status": "running",
                "stopReason": None,
                "rounds": 0,
                "toolCount": 0,
                "summary": None,
                "startedAt": str(payload.get("started_at") or now_iso),
                "completedAt": None,
                "steps": [],
            }
            timelines[subagent_id] = entry
        else:
            if not entry.get("parentToolId") and payload.get("parent_tool_id"):
                entry["parentToolId"] = str(payload["parent_tool_id"]).strip() or None
            if payload.get("label"):
                entry["label"] = str(payload["label"]).strip() or entry["label"]
            if payload.get("mode"):
                entry["mode"] = str(payload["mode"]).strip() or entry["mode"]
            if not entry.get("mission") and payload.get("mission"):
                entry["mission"] = str(payload["mission"]).strip() or None

        kind = str(payload.get("kind") or "").strip()
        if isinstance(payload.get("seq"), int):
            entry["lastEventSeq"] = max(int(entry.get("lastEventSeq") or -1), int(payload["seq"]))
        steps: list[dict[str, Any]] = entry["steps"]
        if kind == "start":
            entry["status"] = "running"
            if payload.get("started_at"):
                entry["startedAt"] = str(payload["started_at"])
            return

        if kind in {"thinking", "model_message"}:
            round_num = int(payload.get("round") or 0)
            if round_num > int(entry.get("rounds") or 0):
                entry["rounds"] = round_num
            is_thinking = kind == "thinking"
            step_id = str(payload.get("step_id") or "").strip() or (
                f"child_{subagent_id}_{'think' if is_thinking else 'msg'}_{round_num or 1}_{len(steps) + 1}"
            )
            ts = str(payload.get("timestamp") or now_iso)
            step_record: dict[str, Any] = {
                "id": step_id,
                "stepId": step_id,
                "turnIndex": round_num or 1,
                "phase": "thinking",
                "type": "thinking" if is_thinking else "model_message",
                "title": "Internal Thought" if is_thinking else "Model message",
                "status": "running" if payload.get("status") == "running" else "completed",
                "startedAt": ts,
                "completedAt": None if payload.get("status") == "running" else ts,
                "details": str(payload.get("text") or ""),
            }
            if not is_thinking:
                step_record["data"] = {"kind": "provisional_model_message"}
            existing_idx = next(
                (i for i, existing in enumerate(steps) if existing.get("stepId") == step_id),
                None,
            )
            if existing_idx is not None:
                steps[existing_idx] = {**steps[existing_idx], **step_record}
            else:
                steps.append(step_record)
            entry["toolCount"] = sum(1 for s in steps if s.get("type") == "tool_call")
            return

        if kind == "tool_start":
            round_num = int(payload.get("round") or 0)
            if round_num > int(entry.get("rounds") or 0):
                entry["rounds"] = round_num
            tool_name = str(payload.get("tool_name") or "").strip() or None
            step_id = (
                str(payload.get("step_id") or "").strip()
                or str(payload.get("tool_id") or "").strip()
                or f"child_{subagent_id}_{len(steps) + 1}"
            )
            step_record = {
                "id": step_id,
                "stepId": step_id,
                "turnIndex": round_num or 1,
                "phase": "exploring",
                "type": "tool_call",
                "title": f"Running {tool_name}" if tool_name else "Running tool",
                "status": "running",
                "startedAt": str(payload.get("started_at") or now_iso),
                "toolName": tool_name,
                "toolId": str(payload.get("tool_id") or "").strip() or None,
                "toolInput": (
                    payload.get("tool_input")
                    if isinstance(payload.get("tool_input"), dict)
                    else None
                ),
            }
            existing_idx = next(
                (i for i, existing in enumerate(steps) if existing.get("stepId") == step_id),
                None,
            )
            if existing_idx is not None:
                steps[existing_idx] = {**steps[existing_idx], **step_record}
            else:
                steps.append(step_record)
            entry["toolCount"] = sum(1 for s in steps if s.get("type") == "tool_call")
            return

        if kind == "tool_result":
            round_num = int(payload.get("round") or 0)
            if round_num > int(entry.get("rounds") or 0):
                entry["rounds"] = round_num
            tool_name = str(payload.get("tool_name") or "").strip() or None
            step_id = str(payload.get("step_id") or "").strip()
            tool_id = str(payload.get("tool_id") or "").strip()
            existing_idx = next(
                (
                    i
                    for i, existing in enumerate(steps)
                    if (step_id and existing.get("stepId") == step_id)
                    or (tool_id and existing.get("toolId") == tool_id)
                ),
                None,
            )
            if existing_idx is None and tool_name:
                existing_idx = next(
                    (
                        i
                        for i, existing in enumerate(steps)
                        if existing.get("status") == "running"
                        and existing.get("toolName") == tool_name
                    ),
                    None,
                )
            status_val = "failed" if str(payload.get("status") or "") == "failed" else "completed"
            error_val = payload.get("error")
            output_preview = payload.get("output_preview")
            output_text = (
                str(error_val)
                if status_val == "failed" and error_val
                else (str(output_preview) if output_preview is not None else None)
            )
            resolved_step_id = step_id or tool_id or f"child_{subagent_id}_{len(steps) + 1}"
            patch: dict[str, Any] = {
                "id": resolved_step_id,
                "stepId": resolved_step_id,
                "turnIndex": round_num or 1,
                "phase": "exploring",
                "type": "tool_call",
                "title": (
                    f"{tool_name} failed"
                    if status_val == "failed" and tool_name
                    else (
                        f"Ran {tool_name}"
                        if tool_name
                        else ("Tool failed" if status_val == "failed" else "Tool completed")
                    )
                ),
                "status": status_val,
                "completedAt": str(payload.get("completed_at") or now_iso),
                "durationMs": (
                    int(payload["duration_ms"])
                    if isinstance(payload.get("duration_ms"), int | float)
                    else None
                ),
                "toolName": tool_name,
                "toolId": tool_id or None,
                "toolOutput": output_text,
                "success": status_val != "failed",
            }
            if existing_idx is not None:
                merged = {
                    **steps[existing_idx],
                    **{k: v for k, v in patch.items() if v is not None},
                }
                merged["status"] = status_val
                merged["success"] = status_val != "failed"
                steps[existing_idx] = merged
            else:
                patch["startedAt"] = str(payload.get("completed_at") or now_iso)
                steps.append(patch)
            entry["toolCount"] = sum(1 for s in steps if s.get("type") == "tool_call")
            return

        if kind == "end":
            status_val = str(payload.get("status") or "completed").strip() or "completed"
            entry["status"] = status_val
            entry["stopReason"] = str(payload.get("stop_reason") or status_val).strip()
            round_num = int(payload.get("rounds") or 0)
            if round_num > int(entry.get("rounds") or 0):
                entry["rounds"] = round_num
            summary_val = str(payload.get("summary") or "").strip()
            if summary_val:
                entry["summary"] = summary_val[:8000]
            completed_iso = str(payload.get("completed_at") or now_iso)
            entry["completedAt"] = completed_iso
            fallback_step_status = "failed" if status_val == "failed" else "completed"
            for step in steps:
                if step.get("status") == "running":
                    step["status"] = fallback_step_status
                    step["completedAt"] = step.get("completedAt") or completed_iso
                    if step.get("type") == "tool_call":
                        step["success"] = fallback_step_status != "failed"
                        tname = str(step.get("toolName") or "").strip()
                        if tname:
                            step["title"] = (
                                f"{tname} failed"
                                if fallback_step_status == "failed"
                                else f"Ran {tname}"
                            )
                        elif fallback_step_status == "failed":
                            step["title"] = "Tool failed"
                        else:
                            step["title"] = "Tool completed"
            entry["toolCount"] = sum(1 for s in steps if s.get("type") == "tool_call")

    @staticmethod
    def _utc_iso(value: datetime | None) -> str:
        """Serialize DB timestamps cleanly in UTC so client timers never apply local-TZ skew."""
        if value is None:
            return datetime.now(UTC).isoformat()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.isoformat()

    @staticmethod
    def _restore_subagent_timelines(state: _TurnState) -> None:
        """Rehydrate previously saved subagent timelines when resuming an interrupted turn."""
        if state.assistant_message is None:
            return
        meta = getattr(state.assistant_message, "metadata_json", None)
        saved = meta.get("subagent_timelines") if isinstance(meta, dict) else None
        if not isinstance(saved, list):
            return
        for raw in saved:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("id") or "").strip()
            if not cid or cid in state.subagent_timelines:
                continue
            state.subagent_timelines[cid] = dict(raw)
            state.bg_subagent_started.add(cid)
            if str(raw.get("status") or "").strip().lower() not in {"running", "queued"}:
                state.bg_subagent_ended.add(cid)
            steps = raw.get("steps")
            raw_last_seq = raw.get("lastEventSeq")
            max_seq = (
                max(int(state.bg_subagent_event_seq.get(cid, -1)), raw_last_seq)
                if isinstance(raw_last_seq, int)
                else int(state.bg_subagent_event_seq.get(cid, -1))
            )
            if isinstance(steps, list):
                prefix = f"child_{cid}_seq_"
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    sid = str(step.get("stepId") or "")
                    if sid.startswith(prefix):
                        suffix = sid[len(prefix) :]
                        if suffix.isdigit():
                            max_seq = max(max_seq, int(suffix))
            if max_seq >= 0:
                state.bg_subagent_event_seq[cid] = max_seq

    def _emit_queued_subagent_start(
        self,
        state: _TurnState,
        *,
        tool_name: str,
        arguments: Any,
        payload: Any,
        turn_index: int,
    ) -> list[str]:
        """Emit an immediate subagent_activity start frame when a delegation tool queues a child."""
        if tool_name not in {"subagent", "subagent_fork", "spawn_teammate"}:
            return []
        if not isinstance(payload, dict):
            return []
        sub_id = str(payload.get("agent_id") or "").strip()
        if not sub_id and isinstance(payload.get("member"), dict):
            sub_id = str(payload["member"].get("child_id") or "").strip()
        if not sub_id or sub_id in state.inline_subagent_ids or sub_id in state.bg_subagent_started:
            return []
        state.bg_subagent_started.add(sub_id)
        args = arguments if isinstance(arguments, dict) else {}
        member = payload.get("member") if isinstance(payload.get("member"), dict) else {}
        label = (
            payload.get("label")
            or member.get("name")
            or args.get("description")
            or args.get("name")
            or sub_id
        )
        mode = (
            payload.get("mode")
            or args.get("mode")
            or ("fork" if tool_name == "subagent_fork" else "one-shot")
        )
        mission = str(args.get("task") or args.get("initial_task") or "").strip()[:500] or None
        raw_depth = payload.get("depth")
        depth = int(raw_depth) if isinstance(raw_depth, int) and raw_depth >= 1 else 1
        activity = {
            "event": "subagent_activity",
            "kind": "start",
            "subagent_id": sub_id,
            "label": str(label),
            "mode": str(mode),
            "mission": mission,
            "depth": depth,
            "started_at": datetime.now(UTC).isoformat(),
            "turn_index": turn_index,
        }
        self._record_subagent_activity(state, activity)
        return [sse("subagent_activity", activity)]

    @contextlib.contextmanager
    def _fresh_poll_db(self, tenant_id: uuid.UUID) -> Any:
        """Open a fresh read session for cross-thread polling so SQLite WAL/Postgres sees commits immediately."""
        bind = getattr(self.db, "get_bind", lambda: None)()
        if bind is not None:
            try:
                from sqlalchemy.engine import Engine
                from sqlalchemy.orm import sessionmaker

                from app.platform.database.session import set_db_tenant_context

                if (
                    isinstance(bind, Engine)
                    and str(getattr(bind, "url", "")) != "sqlite:///:memory:"
                ):
                    poll_session = sessionmaker(
                        bind=bind,
                        autocommit=False,
                        autoflush=False,
                        expire_on_commit=False,
                    )()
                    try:
                        set_db_tenant_context(poll_session, tenant_id)
                        yield poll_session
                    finally:
                        # Detach loaded objects before rollback so any caller
                        # inspecting rows outside the context keeps all loaded
                        # column attributes instead of hitting DetachedInstanceError.
                        with contextlib.suppress(Exception):
                            poll_session.expunge_all()
                        with contextlib.suppress(Exception):
                            poll_session.rollback()
                        with contextlib.suppress(Exception):
                            poll_session.close()
                    return
            except Exception:  # noqa: BLE001
                pass
        with contextlib.suppress(Exception):
            if hasattr(self.db, "expire_all"):
                self.db.expire_all()
        yield self.db

    def _poll_background_subagent_events(
        self,
        state: _TurnState,
        *,
        tracked_ids: set[uuid.UUID] | None = None,
    ) -> list[dict[str, Any]]:
        """Read unemitted child lifecycle and tool events from DB for background children."""
        if (
            state.run_id is None
            or getattr(self, "db", None) is None
            or not hasattr(self.db, "execute")
        ):
            return []
        from sqlalchemy import select

        from app.deepspace.models.session_event import DeepSpaceSessionEvent
        from app.deepspace.models.subagent import DeepSpaceSubagentRun
        from app.deepspace.services.session_events import (
            SUBAGENT_MODEL_MESSAGE,
            SUBAGENT_THINKING,
            TOOL_CALL,
            TOOL_RESULT,
        )

        inline_ids = state.inline_subagent_ids
        seen_seq_by_child = state.bg_subagent_event_seq
        started_ids = state.bg_subagent_started
        ended_ids = state.bg_subagent_ended
        open_steps = state.bg_subagent_open_steps

        try:
            with self._fresh_poll_db(state.auth.tenant_id) as poll_db:
                child_stmt = select(DeepSpaceSubagentRun).where(
                    DeepSpaceSubagentRun.tenant_id == state.auth.tenant_id,
                    DeepSpaceSubagentRun.user_id == state.auth.user_id,
                    DeepSpaceSubagentRun.conversation_id == state.conversation_id,
                    DeepSpaceSubagentRun.parent_run_id == state.run_id,
                )
                children = list(poll_db.execute(child_stmt).scalars().all())
                if tracked_ids is not None:
                    children = [row for row in children if row.id in tracked_ids]
                children = [row for row in children if str(row.id) not in inline_ids]
                if not children:
                    return []
                child_ids = [row.id for row in children]
                ev_rows = list(
                    poll_db.execute(
                        select(DeepSpaceSessionEvent)
                        .where(
                            DeepSpaceSessionEvent.tenant_id == state.auth.tenant_id,
                            DeepSpaceSessionEvent.user_id == state.auth.user_id,
                            DeepSpaceSessionEvent.conversation_id == state.conversation_id,
                            DeepSpaceSessionEvent.subagent_id.in_(child_ids),
                        )
                        .order_by(DeepSpaceSessionEvent.seq.asc())
                    )
                    .scalars()
                    .all()
                )
        except Exception:  # noqa: BLE001
            return []

        child_by_id = {row.id: row for row in children}
        events_out: list[dict[str, Any]] = []

        def _child_mission(row: DeepSpaceSubagentRun) -> str | None:
            for source in (row.pending_messages, row.turn_history):
                if isinstance(source, list):
                    for msg in source:
                        if isinstance(msg, dict) and msg.get("content"):
                            return str(msg["content"]).strip()[:500] or None
            return None

        for row in children:
            cid = str(row.id)
            if cid not in started_ids:
                started_ids.add(cid)
                started_iso = self._utc_iso(row.created_at)
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "start",
                        "subagent_id": cid,
                        "label": row.label,
                        "mode": row.mode,
                        "mission": _child_mission(row),
                        "depth": int(row.delegation_depth or 1),
                        "started_at": started_iso,
                        "turn_index": state.round_index,
                    }
                )

        for ev in ev_rows:
            if ev.subagent_id is None:
                continue
            cid = str(ev.subagent_id)
            if cid in inline_ids:
                continue
            prev_seq = seen_seq_by_child.get(cid, -1)
            if int(ev.seq) <= prev_seq:
                continue
            seen_seq_by_child[cid] = int(ev.seq)
            child_row = child_by_id.get(ev.subagent_id)
            if child_row is None:
                continue
            if cid not in started_ids:
                started_ids.add(cid)
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "start",
                        "subagent_id": cid,
                        "label": child_row.label,
                        "mode": child_row.mode,
                        "mission": _child_mission(child_row),
                        "depth": int(child_row.delegation_depth or 1),
                        "started_at": self._utc_iso(child_row.created_at),
                        "turn_index": state.round_index,
                    }
                )
            ev_payload = dict(ev.payload or {}) if isinstance(ev.payload, dict) else {}
            tname = str(ev.tool_name or "").strip()
            call_id = str(ev_payload.get("call_id") or "").strip()
            created_iso = self._utc_iso(ev.created_at)
            round_val = int(ev.step or 1)
            if ev.event_type == SUBAGENT_THINKING:
                step_id = (
                    str(ev_payload.get("step_id") or "").strip()
                    or f"child_{cid}_think_seq_{ev.seq}"
                )
                think_round = int(ev_payload.get("round") or round_val or 1)
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "thinking",
                        "subagent_id": cid,
                        "label": child_row.label,
                        "mode": child_row.mode,
                        "round": think_round,
                        "seq": int(ev.seq),
                        "step_id": step_id,
                        "text": str(ev_payload.get("text") or ""),
                        "status": "completed",
                        "timestamp": created_iso,
                        "turn_index": state.round_index,
                    }
                )
            elif ev.event_type == SUBAGENT_MODEL_MESSAGE:
                step_id = (
                    str(ev_payload.get("step_id") or "").strip() or f"child_{cid}_msg_seq_{ev.seq}"
                )
                msg_round = int(ev_payload.get("round") or round_val or 1)
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "model_message",
                        "subagent_id": cid,
                        "label": child_row.label,
                        "mode": child_row.mode,
                        "round": msg_round,
                        "seq": int(ev.seq),
                        "step_id": step_id,
                        "text": str(ev_payload.get("text") or ""),
                        "status": "completed",
                        "timestamp": created_iso,
                        "turn_index": state.round_index,
                    }
                )
            elif ev.event_type == TOOL_CALL:
                step_id = f"child_{cid}_call_{call_id}" if call_id else f"child_{cid}_seq_{ev.seq}"
                open_steps.setdefault((cid, tname), []).append(step_id)
                args_val = ev_payload.get("arguments")
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "tool_start",
                        "subagent_id": cid,
                        "label": child_row.label,
                        "mode": child_row.mode,
                        "round": round_val,
                        "seq": int(ev.seq),
                        "step_id": step_id,
                        "tool_id": call_id or step_id,
                        "tool_name": tname,
                        "tool_input": args_val if isinstance(args_val, dict) else None,
                        "started_at": created_iso,
                        "turn_index": state.round_index,
                    }
                )
            elif ev.event_type == TOOL_RESULT:
                queue = open_steps.get((cid, tname))
                if call_id:
                    step_id = f"child_{cid}_call_{call_id}"
                    if queue and step_id in queue:
                        queue.remove(step_id)
                else:
                    step_id = queue.pop(0) if queue else f"child_{cid}_seq_{ev.seq}"
                status_val = str(ev_payload.get("status") or "completed")
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "tool_result",
                        "subagent_id": cid,
                        "label": child_row.label,
                        "mode": child_row.mode,
                        "round": round_val,
                        "seq": int(ev.seq),
                        "step_id": step_id,
                        "tool_id": call_id or step_id,
                        "tool_name": tname,
                        "status": "failed" if status_val == "failed" else "completed",
                        "duration_ms": ev_payload.get("duration_ms"),
                        "error": ev_payload.get("error"),
                        "output_preview": ev_payload.get("output_preview"),
                        "completed_at": created_iso,
                        "turn_index": state.round_index,
                    }
                )

        for row in children:
            cid = str(row.id)
            if row.status not in {"queued", "running"} and cid not in ended_ids:
                ended_ids.add(cid)
                events_out.append(
                    {
                        "event": "subagent_activity",
                        "kind": "end",
                        "subagent_id": cid,
                        "label": row.label,
                        "mode": row.mode,
                        "status": row.status,
                        "stop_reason": str(row.stop_reason or row.status or "completed"),
                        "rounds": int(row.turn_count or 0),
                        "summary": str(row.result_text or "")[:8000],
                        "completed_at": self._utc_iso(row.updated_at),
                        "turn_index": state.round_index,
                    }
                )

        return events_out

    async def _await_and_deliver_active_children(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Wait for active background children spawned in this run and deliver results.

        Mirrors DSH's `waitForChildren` + `subagent-settled` handoff: if the parent
        attempts to finish its round while children started during ``state.run_id``
        are still running, wait safely for them to settle and resume the parent
        so it can synthesize their complete answers.
        """
        if (
            state.run_id is None
            or getattr(state, "subagent_auto_wait_cycles", 0) >= 4
            or getattr(self, "db", None) is None
            or not hasattr(self.db, "execute")
            or state.awaiting_approval is not None
            or state.awaiting_user is not None
            or state.plan_review is not None
            or state.disable_tools_for_final_answer
            or state.forced_answer is not None
        ):
            return

        from sqlalchemy import select

        from app.deepspace.models.subagent import DeepSpaceSubagentRun

        delivered: set[str] = getattr(state, "delivered_child_ids", set())

        def _query_run_children() -> list[DeepSpaceSubagentRun]:
            with self._fresh_poll_db(state.auth.tenant_id) as poll_db:
                return list(
                    poll_db.execute(
                        select(DeepSpaceSubagentRun).where(
                            DeepSpaceSubagentRun.tenant_id == state.auth.tenant_id,
                            DeepSpaceSubagentRun.user_id == state.auth.user_id,
                            DeepSpaceSubagentRun.conversation_id == state.conversation_id,
                            DeepSpaceSubagentRun.parent_run_id == state.run_id,
                        )
                    )
                    .scalars()
                    .all()
                )

        try:
            initial_rows = _query_run_children()
        except Exception:  # noqa: BLE001
            return

        tracked_ids = {
            row.id
            for row in initial_rows
            if row.status in {"queued", "running"} and str(row.id) not in delivered
        }
        if not tracked_ids:
            return

        # Give eager/Celery workers a brief moment to move from queued -> running;
        # if nothing starts running (e.g. worker offline or mocked dispatch), do
        # not stall the turn.
        queue_grace_deadline = time.monotonic() + 0.35
        while time.monotonic() < queue_grace_deadline:
            for activity in self._poll_background_subagent_events(state, tracked_ids=tracked_ids):
                self._record_subagent_activity(state, activity)
                yield sse("subagent_activity", activity)
            try:
                rows = [r for r in _query_run_children() if r.id in tracked_ids]
            except Exception:  # noqa: BLE001
                return
            if any(r.status != "queued" for r in rows):
                break
            await asyncio.sleep(0.08)

        rows = [r for r in _query_run_children() if r.id in tracked_ids]
        if all(r.status == "queued" for r in rows):
            return

        wait_step_id = f"wait_subagents_r{state.round_index}"
        total_tracked = len(tracked_ids)
        wait_step_emitted = False
        last_done_count = sum(1 for r in rows if r.status not in {"queued", "running"})
        if any(r.status in {"queued", "running"} for r in rows):
            wait_step_emitted = True
            yield sse(
                "tool_start",
                {
                    "tool_id": wait_step_id,
                    "step_id": wait_step_id,
                    "tool_name": "subagent_wait",
                    "tool_input": {"subagent_count": total_tracked},
                    "message": (
                        f"Waiting for response from {total_tracked} subagent"
                        + ("s" if total_tracked != 1 else "")
                    ),
                    "turn_index": state.round_index,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        while any(r.status in {"queued", "running"} for r in rows):
            for activity in self._poll_background_subagent_events(state, tracked_ids=tracked_ids):
                self._record_subagent_activity(state, activity)
                yield sse("subagent_activity", activity)
            done_count = sum(1 for r in rows if r.status not in {"queued", "running"})
            if wait_step_emitted and done_count > last_done_count:
                last_done_count = done_count
                yield sse(
                    "tool_progress",
                    {
                        "tool_id": wait_step_id,
                        "step_id": wait_step_id,
                        "tool_name": "subagent_wait",
                        "message": f"Completed {done_count}/{total_tracked} subagents…",
                        "turn_index": state.round_index,
                    },
                )
            if time.monotonic() >= state.loop_deadline:
                break
            if await self._request_disconnected(state.request):
                return
            if self.runtime.is_cancel_requested(run_id=state.run_id):
                return
            await asyncio.sleep(0.2)
            try:
                rows = [r for r in _query_run_children() if r.id in tracked_ids]
            except Exception:  # noqa: BLE001
                return

        for activity in self._poll_background_subagent_events(state, tracked_ids=tracked_ids):
            self._record_subagent_activity(state, activity)
            yield sse("subagent_activity", activity)

        settled = [
            r for r in rows if r.status not in {"queued", "running"} and str(r.id) not in delivered
        ]
        if not settled:
            return
        if wait_step_emitted:
            yield sse(
                "tool_result",
                {
                    "tool_id": wait_step_id,
                    "step_id": wait_step_id,
                    "tool_name": "subagent_wait",
                    "success": True,
                    "message": (
                        f"Collected responses from {len(settled)} subagent"
                        + ("s" if len(settled) != 1 else "")
                    ),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "turn_index": state.round_index,
                },
            )

        interim_text = "".join(state.answer_parts[state.round_answer_start :]).strip()
        if interim_text:
            del state.answer_parts[state.round_answer_start :]
            self._record_model_message_step(
                run_id=state.run_id,
                auth=state.auth,
                conversation_id=state.conversation_id,
                text=interim_text,
                turn_index=state.round_index,
            )
            yield sse(
                "model_message",
                {
                    "text": interim_text,
                    "turn_index": state.round_index,
                    "status": "completed",
                },
            )
            state.conversation_messages.append({"role": "assistant", "content": interim_text})

        summaries: list[str] = []
        payload_agents: list[dict[str, Any]] = []
        for child in settled:
            delivered.add(str(child.id))
            answer_text = str(child.result_text or "").strip()[:20_000]
            stop_reason = str(child.stop_reason or child.status or "completed")
            error_note = f"\nError: {child.last_error}" if child.last_error else ""
            summaries.append(
                f"- Child `{child.id}` ({child.label}) [{child.status} / {stop_reason}]:\n"
                f"{answer_text or '(no answer text returned)'}{error_note}"
            )
            payload_agents.append(
                {
                    "agent_id": str(child.id),
                    "label": child.label,
                    "status": child.status,
                    "stop_reason": stop_reason,
                }
            )

        state.conversation_messages.append(
            {
                "role": "user",
                "content": (
                    "[Subagent completion results delivered automatically]\n"
                    "Use these completed child results to finalize your answer (or spawn follow-up work if needed):\n\n"
                    + "\n\n".join(summaries)
                ),
            }
        )
        yield sse(
            "subagent_settled",
            {
                "agents": payload_agents,
                "turn_index": state.round_index,
            },
        )
        state.subagent_auto_wait_cycles = getattr(state, "subagent_auto_wait_cycles", 0) + 1
        state.control = "continue"

    async def _handle_tool_calls(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 3 of a turn: validate, police, batch and run the model's tool calls."""
        if state.disable_tools_for_final_answer:
            state.control = "break"
            return
        try:
            workspace_root = state.conversation_workspace_root or self._resolve_workspace_root(
                state
            )
        except Exception:  # noqa: BLE001 - a missing root fails path checks closed.
            workspace_root = None
        self.tool_policy.set_workspace_root(workspace_root)
        state.normalized_calls = [call for _, call in state.normalized_call_items]
        state.provider_continuation = "".join(state.provider_continuation_parts)
        if (
            state.normalized_calls
            and uses_deepseek_reasoning_protocol(
                state.candidate.provider_type, state.candidate.model_name
            )
            and state.provider_continuation
        ):
            await self._persist_provider_continuation(
                reasoning_content=state.provider_continuation,
                auth=state.auth,
                conversation_id=state.conversation_id,
                assistant_message=state.assistant_message,
                turn_index=state.round_index,
            )
        state.round_has_text = (
            len(state.answer_parts) > state.round_answer_start
            or len(state.thinking_parts) > state.round_thinking_start
            or len(state.generated_artifacts) > state.round_artifact_start
        )
        yield sse(
            "metrics",
            {
                "contextUsedTokens": state.last_context_used_tokens,
                "contextRemainingTokens": state.last_context_remaining_tokens,
                "contextUsage": state.last_context_usage,
                "contextUsageSource": "estimated_local",
                "sessionInputTokens": state.session_input_tokens,
                "sessionOutputTokens": state.session_output_tokens,
                "sessionTotalTokens": state.session_input_tokens + state.session_output_tokens,
                "requestInputTokens": state.request_input_tokens,
                "requestOutputTokens": state.request_output_tokens,
                "requestInputTokensSource": "serialized_request_estimate",
                "requestOutputTokensSource": "serialized_response_estimate",
                "maxOutputTokens": state.request_max_tokens,
                "providerUsage": state.last_provider_usage,
                "providerUsageScope": (
                    "latest_reported_round"
                    if state.last_provider_usage.get("usage_source") == "provider"
                    else "unavailable"
                ),
                "providerUsageRoundCount": state.provider_usage_rounds_seen,
                "providerTurnUsage": state.provider_turn_usage_snapshot(),
                "cachedInputTokens": None,
                "uncachedInputTokens": state.last_context_used_tokens,
                "tokenCategorySource": "serialized_request_estimate",
                **self._context_budget_state(
                    used_tokens=state.last_context_used_tokens or 0,
                    context_limit=state.candidate.context_window,
                    reserved_output_tokens=state.last_reserved_output_tokens,
                    compacted=state.last_context_compacted,
                ),
                **(
                    {"contextLimit": state.candidate.context_window}
                    if state.candidate.context_window
                    else {}
                ),
                **(
                    {"contextLimitSource": state.candidate.context_window_source}
                    if state.candidate.context_window_source
                    else {}
                ),
            },
        )
        if not state.normalized_calls and not state.round_has_text:
            if state.empty_provider_retries < MAX_EMPTY_PROVIDER_RETRIES:
                state.empty_provider_retries += 1
                state.control = "continue"
                return
            raise DeepSpaceEmptyResponseError(
                f"{state.candidate.provider_type}/{state.candidate.model_name} returned no answer, reasoning, or tool events."
            )
        if not state.normalized_calls:
            state.prose_answer = "".join(state.answer_parts[state.round_answer_start :]).strip()
            state.round_thinking = "".join(state.thinking_parts[state.round_thinking_start :])
            # Provider reasoning is activity metadata, not a usable
            # answer. Some reasoning-capable models can terminate a
            # turn after emitting only thinking events. Without this
            # guard, the loop falls through to finalization, persists
            # an empty assistant message, and emits `done` as if the
            # turn completed successfully.
            if (
                not state.prose_answer
                and not state.generated_artifacts[state.round_artifact_start :]
            ):
                if state.empty_provider_retries < MAX_EMPTY_PROVIDER_RETRIES:
                    state.empty_provider_retries += 1
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="reasoning_only_recovery",
                        content=(
                            "Your previous response contained only internal reasoning. "
                            "Return a concise user-visible answer now. Do not stop after "
                            "thinking and do not expose internal reasoning as the answer."
                        ),
                    )
                    state.control = "continue"
                    return
                raise DeepSpaceEmptyResponseError(
                    f"{state.candidate.provider_type}/{state.candidate.model_name} returned reasoning "
                    "without a user-visible answer."
                )
            state.fake_tool_output = self._looks_like_fake_tool_output(
                f"{state.prose_answer}\n{state.round_thinking}"
            )
            if state.fake_tool_output or (
                state.connected_service_tool_required
                and not state.connected_service_action_completed
            ):
                del state.answer_parts[state.round_answer_start :]
                del state.thinking_parts[state.round_thinking_start :]
                if state.connected_tool_recovery_retries < MAX_CONNECTED_TOOL_RECOVERY_RETRIES:
                    state.connected_tool_recovery_retries += 1
                    self._record_model_message_step(
                        run_id=state.run_id,
                        auth=state.auth,
                        conversation_id=state.conversation_id,
                        text=state.prose_answer,
                        turn_index=state.round_index,
                    )
                    yield sse(
                        "model_message",
                        {
                            "text": state.prose_answer,
                            "turn_index": state.round_index,
                            "status": "completed",
                        },
                    )
                    yield sse("replace", {"content": "", "replayed": False})
                    state.recovery_instruction = (
                        "Your prior response printed tool-shaped JSON, XML, DSML, or function-call "
                        "syntax as ordinary text. Do not print tool syntax. Reply to the user in "
                        "plain language, or use a real structured tool call only when the task needs one."
                    )
                    if state.connected_service_tool_required:
                        state.recovery_instruction = (
                            "This request requires a real structured call to the attached service. "
                            "Do not print <function-call>, JSON, XML, or internal reasoning as text. "
                            "Call the appropriate provided MCP tool now. If no suitable tool exists, "
                            "say so only after this structured-call attempt."
                        )
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="tool_output_recovery",
                        content=state.recovery_instruction,
                    )
                    state.control = "continue"
                    return
                if (
                    state.connected_service_tool_required
                    and not state.connected_service_action_completed
                ):
                    state.terminal_status = "blocked"
                    state.forced_answer = self._connected_service_failure_message(
                        state.mcp_bindings
                    )
                    state.control = "break"
                    return
                if state.fake_tool_output:
                    state.terminal_status = "blocked"
                    state.forced_answer = (
                        "Hello! How can I help you in DeepSpace today?"
                        if state.defer_task_for_greeting
                        else (
                            "DeepSpace stopped safely because the selected model printed a fake tool "
                            "call instead of using the structured tool interface. No tool action was performed."
                        )
                    )
                    state.control = "break"
                    return
            if state.managed_task_run:
                if state.task_lifecycle_stage == "final":
                    # The persisted task check has completed or recorded
                    # a terminal blocker. The user-facing response is
                    # ordinary assistant text, with no final tool call.
                    state.control = "break"
                    return
                state.task_lifecycle_prompt_retries += 1
                if state.task_lifecycle_prompt_retries <= MAX_EMPTY_PROVIDER_RETRIES + 1:
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="task_lifecycle",
                        content=self._task_lifecycle_instruction(
                            stage=state.task_lifecycle_stage,
                            task_id=state.active_task_id,
                        ),
                    )
                    state.control = "continue"
                    return
                state.terminal_status = "blocked"
                # Do not manufacture a tool call or mark work complete.
                # If the model did provide a useful prose response, keep
                # it visible while the durable plan remains paused; this
                # avoids turning an adapter capability mismatch into a
                # synthetic system fault.
                state.forced_answer = "".join(
                    state.answer_parts[state.round_answer_start :]
                ).strip() or (
                    "DeepSpace paused this task because the selected model did not return the "
                    "required structured tool call. The plan and its unfinished work remain saved."
                )
                state.control = "break"
                return
            if (
                state.tool_call_required
                and not state.tool_requirement_satisfied
                and not state.connected_service_tool_required
            ):
                if state.available_tools and state.tool_call_recovery_retries < MAX_TOOL_RETRIES:
                    state.tool_call_recovery_retries += 1
                    if state.prose_answer:
                        self._record_model_message_step(
                            run_id=state.run_id,
                            auth=state.auth,
                            conversation_id=state.conversation_id,
                            text=state.prose_answer,
                            turn_index=state.round_index,
                        )
                        yield sse(
                            "model_message",
                            {
                                "text": state.prose_answer,
                                "turn_index": state.round_index,
                                "status": "completed",
                            },
                        )
                    del state.answer_parts[state.round_answer_start :]
                    del state.thinking_parts[state.round_thinking_start :]
                    yield sse("replace", {"content": "", "replayed": False})
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="required_live_tool_call",
                        content=(
                            "You answered without using a structured tool. This request still "
                            "requires a real tool result. Call the most relevant supplied tool now. "
                            "Do not repeat a prose answer or claim the check was completed."
                        ),
                    )
                    state.control = "continue"
                    return
                state.terminal_status = "blocked"
                state.forced_answer = (
                    "I could not complete that live check because the model did not call a "
                    "supplied tool. No check was performed. Try again with a tool-capable model "
                    "and a preset that permits the needed tool."
                )
                state.control = "break"
                return
            if state.available_tools and self._looks_like_clarification_request(state.prose_answer):
                if state.clarification_retries < 1:
                    state.clarification_retries += 1
                    # The first response was streamed optimistically.
                    # Replace it in the UI before retrying so users do
                    # not see a duplicate prose question above the
                    # eventual interactive card.
                    del state.answer_parts[state.round_answer_start :]
                    del state.thinking_parts[state.round_thinking_start :]
                    self._record_model_message_step(
                        run_id=state.run_id,
                        auth=state.auth,
                        conversation_id=state.conversation_id,
                        text=state.prose_answer,
                        turn_index=state.round_index,
                    )
                    yield sse(
                        "model_message",
                        {
                            "text": state.prose_answer,
                            "turn_index": state.round_index,
                            "status": "completed",
                        },
                    )
                    yield sse("replace", {"content": "", "replayed": False})
                    self._set_dynamic_system_instruction(
                        state.conversation_messages,
                        key="clarification_recovery",
                        content=(
                            "Your last response asked the user for clarification in prose. "
                            "This turn requires an interactive clarification: call the ask_user "
                            "tool now with one concise question and optional choices. Do not answer "
                            "in prose and do not call another tool first."
                        ),
                    )
                    state.control = "continue"
                    return
                # A second provider response still asked for missing
                # information without using ask_user. Convert only
                # this narrowly detected case into the same persisted
                # question lifecycle used by a real tool call.
                state.question_id = str(uuid.uuid4())
                state.awaiting_user = {
                    "awaiting_user": True,
                    "question": state.prose_answer[:2000],
                    "options": [],
                    "question_id": state.question_id,
                    "call_id": f"clarification_{state.question_id}",
                    "step_id": f"clarification_{state.round_index}",
                    "tool_name": "ask_user",
                    "tool_input": {
                        "question": state.prose_answer[:2000],
                        "options": [],
                    },
                    "turn_index": state.round_index,
                }
                del state.answer_parts[state.round_answer_start :]
                del state.thinking_parts[state.round_thinking_start :]
                self._record_model_message_step(
                    run_id=state.run_id,
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    text=state.prose_answer,
                    turn_index=state.round_index,
                )
                yield sse(
                    "model_message",
                    {
                        "text": state.prose_answer,
                        "turn_index": state.round_index,
                        "status": "completed",
                    },
                )
                yield sse("replace", {"content": "", "replayed": False})
                yield sse(
                    "ask_user_question",
                    {
                        "tool_name": "ask_user",
                        "tool_id": state.awaiting_user["call_id"],
                        "step_id": state.awaiting_user["step_id"],
                        "message": state.awaiting_user["question"],
                        "options": [],
                        "question_id": state.question_id,
                        "turn_index": state.round_index,
                    },
                )
                state.control = "break"
                return
            async for progress_frame in self._await_and_deliver_active_children(state):
                yield progress_frame
            if state.control == "continue":
                return
            state.control = "break"
            return
        # Prose emitted before a real structured tool call is model
        # narration for this turn, not the final user-facing answer.
        # Preserve it as a durable timeline step instead of letting
        # it remain at the bottom and then disappear on completion.
        state.prose_before_tools = "".join(state.answer_parts[state.round_answer_start :]).strip()
        if state.prose_before_tools:
            self._record_model_message_step(
                run_id=state.run_id,
                auth=state.auth,
                conversation_id=state.conversation_id,
                text=state.prose_before_tools,
                turn_index=state.round_index,
            )
            yield sse(
                "model_message",
                {
                    "text": state.prose_before_tools,
                    "turn_index": state.round_index,
                    "status": "completed",
                },
            )
            del state.answer_parts[state.round_answer_start :]
            yield sse("replace", {"content": "", "replayed": False})
        state.tool_exchange_start = len(state.conversation_messages)
        state.assistant_tool_message = {
            "role": "assistant",
            "content": "" if is_deepseek_v4_model(state.candidate.model_name) else None,
            "tool_calls": state.normalized_calls,
        }
        if (
            uses_deepseek_reasoning_protocol(
                state.candidate.provider_type, state.candidate.model_name
            )
            and state.provider_continuation
        ):
            state.assistant_tool_message["reasoning_content"] = state.provider_continuation
        state.conversation_messages.append(state.assistant_tool_message)
        # Record message time for time context
        if hasattr(state, "time_context_service"):
            state.time_context_service.record_message_time(state.conversation_id, time.time())
        async for event in self._validate_tool_calls(state):
            yield event

        state.invalid_tool_error_messages.extend(state.round_invalid_tool_error_messages)
        state.invalid_tool_names.extend(state.round_invalid_tool_names)

        if state.invalid_tool_call_ids:
            state.assistant_tool_message = state.conversation_messages[state.tool_exchange_start]
            state.retained_calls = [
                call
                for call in state.assistant_tool_message.get("tool_calls", [])
                if str(call.get("id") or "") not in state.invalid_tool_call_ids
            ]
            state.retained_messages = [
                message
                for message in state.conversation_messages[state.tool_exchange_start + 1 :]
                if not (
                    message.get("role") == "tool"
                    and str(message.get("tool_call_id") or "") in state.invalid_tool_call_ids
                )
            ]
            del state.conversation_messages[state.tool_exchange_start :]
            if state.retained_calls:
                state.conversation_messages.append(
                    {
                        **state.assistant_tool_message,
                        "tool_calls": state.retained_calls,
                    }
                )
            state.conversation_messages.extend(state.retained_messages)

        if (
            state.invalid_tool_arguments
            and not state.valid_calls
            and state.awaiting_approval is None
        ):
            if state.invalid_tool_argument_retries < MAX_TOOL_RETRIES:
                state.invalid_tool_argument_retries += 1
                state.validation_details = " ".join(state.invalid_tool_error_messages[-3:])[:2_000]
                self._set_dynamic_system_instruction(
                    state.conversation_messages,
                    key="invalid_tool_arguments",
                    content=(
                        "Your previous structured tool arguments failed schema validation. Retry the "
                        "same action using exactly one valid JSON object matching the supplied tool "
                        "schema. Preserve required fields, keep bounded fields within their limits, "
                        "and do not write tool markup or prose. Validation details: "
                        f"{state.validation_details}"
                    ),
                )
                state.control = "continue"
                return
            state.validation_details = " ".join(state.invalid_tool_error_messages[-3:])[:1_500]
            state.validation_tool_name = (
                state.invalid_tool_names[-1] if state.invalid_tool_names else None
            )
            if state.run_id is not None:
                try:
                    self.runtime.update_checkpoint(
                        run_id=state.run_id,
                        status="failed",
                        checkpoint={
                            "status": "failed",
                            "phase": "tool_validation",
                            "turn_index": state.round_index,
                            "tool_name": state.validation_tool_name,
                            "error_category": "validation",
                            "validation_message": state.validation_details,
                            "resume_available": True,
                            "next_action": "retry_tool_arguments",
                            "task_lifecycle_stage": state.task_lifecycle_stage,
                            "active_task_id": state.active_task_id,
                        },
                        last_error="invalid_tool_arguments",
                    )
                except Exception:  # noqa: BLE001
                    # A checkpoint write must not replace the original
                    # validation result with a storage error.
                    logger.warning(
                        "DeepSpace could not persist invalid-tool checkpoint",
                        exc_info=True,
                    )
            state.forced_answer = (
                "DeepSpace could not validate the tool arguments after one correction attempt. "
                "Completed tool results and the current task checkpoint were saved. Retry to "
                "continue from the last successful step."
            )
            state.terminal_status = "failed"
            state.control = "break"
            return

        if state.awaiting_approval is not None:
            state.valid_calls = []
            if state.run_id is not None:
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="awaiting_approval",
                    checkpoint={
                        "status": "awaiting_approval",
                        "turn_index": state.round_index,
                        "phase": "approval",
                        "pending_approval": state.awaiting_approval,
                        "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                    },
                )
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="approval_requested",
                    status="awaiting_approval",
                    tool_name=str(state.awaiting_approval["tool_name"]),
                    tool_call_id=str(state.awaiting_approval["call_id"]),
                    input_json=dict(state.awaiting_approval["tool_input"]),
                    result_json={
                        "approval_id": str(state.awaiting_approval["approval_id"]),
                        "risk_level": str(state.awaiting_approval["risk_level"]),
                    },
                )
            yield sse("permission_request", state.awaiting_approval)
            yield sse("approval_request", state.awaiting_approval)

        if state.run_id is not None and state.valid_calls:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.MODEL_REQUESTED,
                    next_phase=RuntimePhase.ACTIONS_VALIDATED,
                    turn_index=state.round_index,
                    details={"tool_count": len(state.valid_calls)},
                ),
            )
        for state.item in state.valid_calls:  # noqa: B020
            state.tool_name = str(state.item["tool_name"])
            if state.run_id is not None:
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="tool_start",
                    status="running",
                    tool_name=state.tool_name,
                    tool_call_id=str(state.item["call_id"]),
                    input_json=state.item["arguments"],
                    result_json={"turn_index": state.round_index},
                )
            yield sse(
                "tool_start",
                {
                    "tool_name": state.tool_name,
                    "tool_id": state.item["call_id"],
                    "step_id": state.item["step_id"],
                    "tool_input": state.item["arguments"],
                    "permission_level": "auto",
                    "turn_index": state.round_index,
                    "started_at": self._now(),
                },
            )

        state.read_semaphore = asyncio.Semaphore(
            max(
                1,
                min(
                    int(getattr(self.settings, "deepspace_max_concurrent_tools", 8)),
                    32,
                ),
            )
        )
        state.write_lock = asyncio.Lock()
        state.execution_batches = self._plan_tool_execution_batches(
            state.valid_calls,
            mcp_bindings=state.mcp_bindings,
        )
        if state.run_id is not None:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.ACTIONS_VALIDATED,
                    next_phase=RuntimePhase.EXECUTING,
                    turn_index=state.round_index,
                    details={
                        "tool_count": len(state.valid_calls),
                        "batch_count": len(state.execution_batches),
                    },
                ),
            )
            self.runtime.record_step(
                run_id=state.run_id,
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                step_type="execution_batch_plan",
                status="running",
                result_json={
                    "batch_count": len(state.execution_batches),
                    "parallel_read_batches": sum(
                        1 for batch in state.execution_batches if batch.parallel
                    ),
                    "tool_count": len(state.valid_calls),
                },
            )
            DEEPSPACE_EXECUTION_BATCHES_TOTAL.labels(mode="planned").inc(
                len(state.execution_batches)
            )

        state.results_by_call_id = {}
        for state.batch in state.execution_batches:  # noqa: B020
            progress_queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
            activity_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            def _make_progress_cb(
                call_id: str,
                tname: str,
                queue: asyncio.Queue[tuple[str, str, str]] = progress_queue,
            ) -> Any:
                def _cb(msg: str) -> None:
                    with contextlib.suppress(Exception):
                        queue.put_nowait((call_id, tname, str(msg)))

                return _cb

            def _make_activity_cb(
                call_id: str,
                queue: asyncio.Queue[dict[str, Any]] = activity_queue,
            ) -> Any:
                def _cb(payload: dict[str, Any]) -> None:
                    if not isinstance(payload, dict):
                        return
                    with contextlib.suppress(Exception):
                        enriched = dict(payload)
                        enriched.setdefault("parent_tool_id", call_id)
                        queue.put_nowait(enriched)

                return _cb

            state.coroutines = [
                self._run_tool_call(
                    tool_name=str(item["tool_name"]),
                    arguments=item["arguments"],
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    web_provider=state.web_provider,
                    web_candidate=state.web_candidate,
                    web_search_options=state.web_search_options,
                    request=state.request,
                    loop_deadline=state.loop_deadline,
                    run_id=state.run_id,
                    turn=state.turn_number,
                    step=state.round_index,
                    read_semaphore=state.read_semaphore,
                    write_lock=state.write_lock,
                    mcp_binding=state.mcp_bindings.get(str(item["tool_name"])),
                    mcp_bindings=state.mcp_bindings,
                    assistant_message_id=state.assistant_message.id,
                    ignore_existing_tasks=(
                        not state.allow_existing_task_state and not state.managed_task_run
                    ),
                    user_prompt=state.prompt,
                    native_tool_names=state.native_tool_names,
                    progress_callback=_make_progress_cb(
                        str(item["call_id"]),
                        str(item["tool_name"]),
                    ),
                    activity_callback=_make_activity_cb(str(item["call_id"])),
                    thinking_enabled=bool(state.thinking_enabled),
                    reasoning_effort=(state.selected_effort or state.reasoning_effort),
                )
                for item in state.batch.calls
            ]
            tool_spans = [
                (
                    str(item["call_id"]),
                    self._open_telemetry_span(
                        state,
                        self.session_telemetry.tool_span(
                            state.conversation_id,
                            state.round_index,
                            int(item.get("step", 0) or 0),
                            str(item["tool_name"]),
                        ),
                    ),
                )
                for item in state.batch.calls
            ]
            batch_future: asyncio.Future[Any] = (
                asyncio.ensure_future(asyncio.gather(*state.coroutines))
                if state.batch.parallel
                else asyncio.ensure_future(state.coroutines[0])
            )
            last_bg_poll = time.monotonic()
            while not batch_future.done():
                emitted_any = False
                while not progress_queue.empty():
                    emitted_any = True
                    cid, tname, pmsg = progress_queue.get_nowait()
                    yield sse(
                        "tool_progress",
                        {
                            "tool_id": cid,
                            "tool_name": tname,
                            "message": pmsg,
                            "turn_index": state.round_index,
                        },
                    )
                while not activity_queue.empty():
                    emitted_any = True
                    activity = activity_queue.get_nowait()
                    sub_id = str(activity.get("subagent_id") or "").strip()
                    if sub_id:
                        state.inline_subagent_ids.add(sub_id)
                    activity_payload = {**activity, "turn_index": state.round_index}
                    self._record_subagent_activity(state, activity_payload)
                    yield sse("subagent_activity", activity_payload)
                now_mono = time.monotonic()
                if now_mono - last_bg_poll >= 0.2:
                    last_bg_poll = now_mono
                    for bg_activity in self._poll_background_subagent_events(state):
                        emitted_any = True
                        self._record_subagent_activity(state, bg_activity)
                        yield sse("subagent_activity", bg_activity)
                if not emitted_any:
                    await asyncio.sleep(0.03)
            while not progress_queue.empty():
                cid, tname, pmsg = progress_queue.get_nowait()
                yield sse(
                    "tool_progress",
                    {
                        "tool_id": cid,
                        "tool_name": tname,
                        "message": pmsg,
                        "turn_index": state.round_index,
                    },
                )
            while not activity_queue.empty():
                activity = activity_queue.get_nowait()
                sub_id = str(activity.get("subagent_id") or "").strip()
                if sub_id:
                    state.inline_subagent_ids.add(sub_id)
                activity_payload = {**activity, "turn_index": state.round_index}
                self._record_subagent_activity(state, activity_payload)
                yield sse("subagent_activity", activity_payload)
            raw_batch_result = await batch_future
            state.batch_results = (
                list(raw_batch_result) if state.batch.parallel else [raw_batch_result]
            )
            for bg_activity in self._poll_background_subagent_events(state):
                self._record_subagent_activity(state, bg_activity)
                yield sse("subagent_activity", bg_activity)
            state.tool_call_count = (state.tool_call_count or 0) + len(state.batch.calls)
            # Record repeat tool reminders for native tool calls in this batch
            tracker = self.repeat_tool_reminder(state.conversation_id)
            if tracker is not None:
                for item, _result in zip(state.batch.calls, state.batch_results, strict=True):
                    tool_name = str(item.get("tool_name") or "")
                    if NATIVE_TOOL_REGISTRY.contains(tool_name):
                        arguments = item.get("arguments") or {}
                        reminder = tracker.record(
                            str(state.conversation_id),
                            tool_name,
                            arguments,
                        )
                        if reminder:
                            state.repeat_tool_reminders = getattr(
                                state, "repeat_tool_reminders", []
                            )
                            state.repeat_tool_reminders.append(
                                {"tool_name": tool_name, "message": reminder}
                            )
            for call_id, handle in tool_spans:
                result = next(
                    (item for item in state.batch_results if str(item.get("call_id")) == call_id),
                    None,
                )
                self.session_telemetry.record_tool_result(
                    handle.span,
                    success=bool(result.get("success")) if result else False,
                    duration_ms=int(result.get("duration_ms") or 0) if result else 0,
                    error=(result.get("error") if result else None),
                )
                handle.close()
            state.results_by_call_id.update(
                {
                    str(item["call_id"]): result
                    for item, result in zip(state.batch.calls, state.batch_results, strict=True)
                }
            )
        state.results = [
            state.results_by_call_id[str(item["call_id"])] for item in state.valid_calls
        ]
        if isinstance(state.mcp_budget, dict):
            state.mcp_call_count = max(
                int(state.mcp_call_count or 0), int(state.mcp_budget.get("calls", 0))
            )
            state.mcp_discovery_call_count = max(
                int(state.mcp_discovery_call_count or 0),
                int(state.mcp_budget.get("discovery", 0)),
            )
            state.mcp_result_chars = max(
                int(state.mcp_result_chars or 0),
                int(state.mcp_budget.get("result_chars", 0)),
            )
        if any(
            str(item.get("tool_name") or "") not in CLARIFICATION_TOOL_NAMES
            and (
                str(item.get("tool_name") or "") not in {MCP_SEARCH_TOOLS, MCP_GET_TOOL_SCHEMA}
                or (state.tool_call_required and not state.connected_service_tool_required)
            )
            for item in state.valid_calls
        ):
            # A structured action actually reached the executor. A denied or
            # failed result is still useful evidence for the model to explain.
            # Catalog/schema calls satisfy a catalog request, but they do not
            # satisfy a request to act inside the connected service.
            state.tool_requirement_satisfied = True
        if state.run_id is not None and state.valid_calls:
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint=transition_checkpoint(
                    previous=RuntimePhase.EXECUTING,
                    next_phase=RuntimePhase.RESULTS_RECORDED,
                    turn_index=state.round_index,
                    details={"tool_count": len(state.valid_calls)},
                ),
            )
        for state.item, state.result in zip(  # noqa: B020
            state.valid_calls, state.results, strict=True
        ):
            state.tool_name = str(state.item["tool_name"])
            state.call_id = str(state.item["call_id"])
            state.step_id = str(state.item["step_id"])
            state.success = bool(state.result.get("success"))
            if not state.success:
                state.result = self._normalize_tool_failure_result(state.tool_name, state.result)
            state.raw_tool_payload = state.result.get("payload")
            state.tool_payload = (
                state.raw_tool_payload if isinstance(state.raw_tool_payload, dict) else {}
            )
            if state.success and self._coordination_read_no_progress(state):
                read_streak = int((state.coordination_read_streaks or {}).get(state.tool_name, 0))
                state.tool_payload = {
                    "status": "ok",
                    "stop_turn": True,
                    "coordination_guard": True,
                    "error_code": "COORDINATION_NO_PROGRESS",
                    "read_streak": read_streak,
                    "message": (
                        "The team state did not change after repeated coordination reads. "
                        "Stop polling and answer from the latest observation; saved progress remains available."
                    ),
                }
            state.guard_refusal = bool(
                state.success
                and (
                    state.tool_payload.get("stop_turn") is True
                    or state.tool_payload.get("duplicate_guard") is True
                )
            )
            if state.success:
                state.is_mcp_payload = (
                    state.tool_name in MCP_BROKER_TOOL_NAMES
                    or state.mcp_bindings.get(state.tool_name) is not None
                )
                if state.is_mcp_payload:
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_result_chars = max(
                            int(state.mcp_result_chars or 0),
                            int(state.mcp_budget.get("result_chars", 0)),
                        )
                    state.payload_chars = len(
                        json.dumps(state.tool_payload, ensure_ascii=False, default=str)
                    )
                    state.remaining_result_chars = max(
                        0, self.mcp_max_result_chars_per_turn - state.mcp_result_chars
                    )
                    if state.payload_chars > state.remaining_result_chars:
                        state.tool_payload = self.mcp_broker.bound_result(
                            state.tool_payload,
                            max_chars=state.remaining_result_chars,
                        )
                    state.mcp_result_chars += len(
                        json.dumps(state.tool_payload, ensure_ascii=False, default=str)
                    )
                    if isinstance(state.mcp_budget, dict):
                        state.mcp_budget["result_chars"] = max(
                            int(state.mcp_budget.get("result_chars", 0)),
                            int(state.mcp_result_chars),
                        )
                if state.mcp_bindings.get(state.tool_name) is not None:
                    # Keep the bounded full result in tenant-scoped,
                    # short-lived server storage. The model receives
                    # only a compact preview plus an opaque reference;
                    # it can request more through the broker when
                    # genuinely needed.
                    state.result_ref = self.mcp_result_store.put(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        value=state.tool_payload,
                    )
                    DEEPSPACE_MCP_RESULT_REFERENCES_TOTAL.inc()
                    DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                        kind="mcp", status="created"
                    ).inc()
                    state.preview = json.dumps(
                        state.tool_payload, ensure_ascii=False, default=str, separators=(",", ":")
                    )
                    state.tool_payload = {
                        "status": "ok",
                        "result_ref": state.result_ref,
                        "preview": state.preview[: state.round_budget.mcp_preview_chars],
                        "preview_truncated": len(state.preview)
                        > state.round_budget.mcp_preview_chars,
                        "preview_chars_limit": state.round_budget.mcp_preview_chars,
                        "message": (
                            "Use mcp_broker with operation=result and this result_ref "
                            "only if more detail is required."
                        ),
                    }
                if state.mcp_bindings.get(state.tool_name) is not None:
                    # A successful MCP result satisfies the connected
                    # service requirement for this turn. The model may
                    # now provide a normal final answer without being
                    # forced through another structured-call retry.
                    state.connected_service_action_completed = True
                if state.tool_name in {
                    "subagent",
                    "subagent_fork",
                    "spawn_teammate",
                } and isinstance(state.tool_payload, dict):
                    for frame in self._emit_queued_subagent_start(
                        state,
                        tool_name=state.tool_name,
                        arguments=state.item.get("arguments"),
                        payload=state.tool_payload,
                        turn_index=state.round_index,
                    ):
                        yield frame
                if state.tool_name == "write" and state.item["arguments"].get("target") == "memory":
                    state.memory_written_this_turn = True
                if state.tool_name == "spill_read":
                    state.pending_images.extend(take_recovered_images(state.tool_payload))
                state.raw_image = state.tool_payload.get("_image_base64")
                if isinstance(state.raw_image, str) and state.raw_image:
                    state.pending_images.append(state.raw_image)
                elif state.tool_name == "read_image":
                    # A local image is handed to the model as an image part,
                    # and its bytes are kept out of the stored result so the
                    # session log does not carry megabytes of base64.
                    image_data = state.tool_payload.get("data")
                    if isinstance(image_data, str) and image_data:
                        state.pending_images.append(image_data)
                        state.tool_payload = {
                            key: value for key, value in state.tool_payload.items() if key != "data"
                        }
                elif state.tool_name in {"browser_screenshot", "computer_screenshot"}:
                    state.artifact = self._consume_screenshot_tool_result(state)
                    if isinstance(state.artifact, dict) and state.artifact.get("id"):
                        yield sse(
                            "artifact",
                            {"artifact": state.artifact, "turn_index": state.round_index},
                        )
                state.tool_payload = self.tool_policy.after_tool(
                    state.tool_name, state.tool_payload
                )
                if (
                    state.tool_name in {"bash", "pwsh"}
                    and state.tool_payload.get("failure_kind") == "command_not_found"
                ):
                    state.forced_answer = (
                        "The shell could not find a requested command in this runtime's PATH. "
                        "I stopped instead of repeating it; no live result was obtained."
                    )
                    state.terminal_status = "blocked"
                if state.guard_refusal:
                    capability_failure_message = _capability_failure_message(state.tool_payload)
                    if capability_failure_message is not None:
                        state.forced_answer = capability_failure_message
                    elif state.tool_payload.get("coordination_guard") is True:
                        state.forced_answer = (
                            "DeepSpace stopped repeated team-status polling because no new "
                            "coordination state appeared. The latest saved team progress remains available."
                        )
                    elif state.tool_payload.get("stop_turn") is True:
                        self._request_model_final_answer_without_tools(
                            state,
                            reason=(
                                "the overall turn safety budget was reached. No further tool "
                                "calls can run this turn; use completed results and state what "
                                "remains unknown."
                            ),
                        )
                    else:
                        state.forced_answer = (
                            "DeepSpace stopped this turn at its safety limit. No additional tool "
                            "action was executed; saved progress remains available."
                        )
                    state.terminal_status = "blocked"
                # Native results use the same scoped-reference pattern
                # as MCP once their model-visible category budget is
                # exhausted. The stored value is post-policy and can
                # only be retrieved by this tenant/user/conversation.
                if (
                    not state.is_mcp_payload
                    and state.tool_name != "get_tool_result"
                    and state.round_budget.tool_result_budget_tokens is not None
                ):
                    state.rendered_payload = json.dumps(
                        state.tool_payload,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                    # A result far larger than the context window is spilled
                    # to durable storage first.  The short-lived reference
                    # below is a convenience; the spill is the only remaining
                    # copy, so it must survive the reference's expiry.
                    if (
                        len(state.rendered_payload) > self._spill_threshold_chars()
                        and state.tool_name != "spill_read"
                    ):
                        state.spilled_payload, state.spill_ref = self._spill_tool_result(
                            auth=state.auth,
                            tool_name=state.tool_name,
                            payload=state.tool_payload,
                            preview_budget_tokens=max(
                                64,
                                min(
                                    1_024,
                                    int(state.round_budget.tool_result_budget_tokens or 1_024) // 4,
                                ),
                            ),
                        )
                        if state.spill_ref is not None:
                            state.tool_payload = state.spilled_payload
                            if not any(
                                str(tool.get("function", {}).get("name") or "") == "spill_read"
                                for tool in state.available_tools
                            ):
                                state.available_tools.append(
                                    NATIVE_TOOL_REGISTRY.emit("spill_read")
                                )
                            DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                                kind="spill", status="created"
                            ).inc()
                        elif state.spilled_payload.get("status") == "refused":
                            # The result was too large to keep, so the
                            # refusal is what the model reads and the
                            # preview below carries the refusal rather than
                            # the result that was too large.
                            state.tool_payload = state.spilled_payload
                            state.rendered_payload = json.dumps(
                                state.tool_payload,
                                ensure_ascii=False,
                                default=str,
                                separators=(",", ":"),
                            )
                    state.native_limit_chars = state.round_budget.tool_result_budget_tokens * 4
                    state.remaining_native_chars = max(
                        0, state.native_limit_chars - state.native_result_chars
                    )
                    state.preview_limit = min(
                        state.round_budget.mcp_preview_chars,
                        state.remaining_native_chars,
                    )
                    if len(state.rendered_payload) > state.preview_limit:
                        state.result_ref = self.tool_result_store.put(
                            tenant_id=state.auth.tenant_id,
                            user_id=state.auth.user_id,
                            conversation_id=state.conversation_id,
                            value=state.tool_payload,
                        )
                        if not any(
                            str(tool.get("function", {}).get("name") or "") == "get_tool_result"
                            for tool in state.available_tools
                        ):
                            state.available_tools.append(
                                NATIVE_TOOL_REGISTRY.emit("get_tool_result")
                            )
                        state.tool_payload = {
                            "status": "ok",
                            "result_ref": state.result_ref,
                            "preview": state.rendered_payload[: state.preview_limit],
                            "preview_truncated": True,
                            "preview_chars_limit": state.preview_limit,
                            "message": "Use get_tool_result with this result_ref only if more detail is required.",
                        }
                        DEEPSPACE_RESULT_REFERENCE_EVENTS_TOTAL.labels(
                            kind="native", status="created"
                        ).inc()
                    state.native_result_chars += min(
                        len(state.rendered_payload), state.preview_limit
                    )
                # Materialize sandbox outputs as private, durable
                # artifacts and expose them through the same timeline
                # used by provider-generated media. Input files remain
                # ephemeral inside the executor and are never copied.
                if state.tool_name == "sandbox_execute":
                    state.persisted = []
                    for state.generated in state.tool_payload.get("files", []):  # noqa: B020
                        if not isinstance(state.generated, dict):
                            continue
                        state.encoded = state.generated.get("data_base64")
                        if not isinstance(state.encoded, str) or not state.encoded:
                            continue
                        try:
                            state.artifact = self.media_artifacts.persist_content_base64(
                                tenant_id=state.auth.tenant_id,
                                user_id=state.auth.user_id,
                                conversation_id=state.conversation_id,
                                message_id=state.assistant_message.id,
                                content_type=str(
                                    state.generated.get("content_type")
                                    or "application/octet-stream"
                                ),
                                data_base64=state.encoded,
                                provider_type="sandbox",
                                model_name=None,
                                title=str(state.generated.get("name") or "Sandbox output"),
                                metadata={
                                    "source": "sandbox_execute",
                                    "turn_index": state.round_index,
                                },
                                requested_kind=(
                                    "chart"
                                    if str(state.generated.get("content_type") or "").startswith(
                                        ("image/", "image/svg")
                                    )
                                    else None
                                ),
                            )
                        except (ValueError, TypeError):
                            logger.warning("Rejected unsafe sandbox output artifact")
                            continue
                        state.persisted.append(state.artifact)
                        state.generated_artifacts.append(state.artifact)
                        yield sse(
                            "artifact",
                            {"artifact": state.artifact, "turn_index": state.round_index},
                        )
                    if state.persisted:
                        state.tool_payload["artifacts"] = state.persisted
                    # Never send binary base64 back through the model
                    # context or browser timeline; artifact metadata is
                    # sufficient for the user to preview/download it.
                    state.tool_payload["files"] = [
                        {
                            "name": artifact.get("title"),
                            "content_type": artifact.get("content_type"),
                            "size_bytes": artifact.get("size_bytes"),
                            "artifact_id": artifact.get("id"),
                        }
                        for artifact in state.persisted
                    ]
                state.panel_artifact = state.tool_payload.get("panel_artifact")
                if isinstance(state.panel_artifact, dict) and state.panel_artifact.get("id"):
                    state.generated_artifacts.append(state.panel_artifact)
                    yield sse(
                        "artifact",
                        {"artifact": state.panel_artifact, "turn_index": state.round_index},
                    )
                if state.tool_name in {
                    "web_search",
                    "web_fetch",
                    "url_read",
                    "image_read",
                } and not state.tool_payload.get("is_error", False):
                    if state.tool_name == "web_search":
                        state.native_research_stats["searches"] += 1
                        state.native_research_stats["results"] += len(
                            [
                                item
                                for item in state.tool_payload.get("citations", [])
                                if isinstance(item, dict)
                            ]
                        )
                    elif state.tool_name in {"web_fetch", "url_read"}:
                        if state.tool_payload.get("retrieval_status") in {
                            "blocked",
                            "unavailable",
                        }:
                            state.native_research_stats["blocked_pages"] += 1
                        else:
                            state.native_research_stats["pages"] += 1
                    else:
                        state.native_research_stats["images"] += 1
                    for state.citation in state.tool_payload.get("citations", []):  # noqa: B020
                        if isinstance(state.citation, dict):
                            state.citation_status = state.citation.get("retrieval_status")
                            if state.tool_name == "web_search" and not state.citation_status:
                                state.citation_status = "search_snippet_only"
                            state.citations.append(
                                {
                                    **state.citation,
                                    "retrieval_status": state.citation_status,
                                    "id": len(state.citations) + 1,
                                }
                            )
                    if state.tool_name == "image_read" and not state.tool_payload.get("citations"):
                        state.image_url = str(state.item["arguments"].get("url") or "").strip()
                        if state.image_url.startswith(("http://", "https://")):
                            state.citations.append(
                                {
                                    "id": len(state.citations) + 1,
                                    "title": "Inspected image",
                                    "url": state.image_url,
                                    "source": "web_fetch",
                                }
                            )
                if state.tool_name == "find" and state.item["arguments"].get("target") == "memory":
                    for state.memory in state.tool_payload.get("memories", []):  # noqa: B020
                        if not isinstance(state.memory, dict) or not state.memory.get("id"):
                            continue
                        state.memory_ref = {
                            "id": str(state.memory["id"]),
                            "key": str(state.memory.get("key") or "memory")[:120],
                            "source": str(state.memory.get("source") or "memory"),
                        }
                        if state.memory_ref not in state.used_memories:
                            state.used_memories.append(state.memory_ref)
                    if state.used_memories:
                        yield sse("memory_used", {"memories": state.used_memories})
                if (
                    not state.managed_task_run
                    and state.tool_name == "todo_write"
                    and str(state.item["arguments"].get("operation") or "") == "replace"
                    and state.item["arguments"].get("todos") is None
                ):
                    state.task_check = self.task_store.check_tasks(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                    )
                    if state.task_check.get("task_count"):
                        # The model has chosen a real persisted plan.
                        # From the next turn onward the lifecycle guard
                        # protects its order, evidence, and completion.
                        state.managed_task_run = True
                        state.task_lifecycle_stage = "read_plan"
                        state.active_task_id = None
                elif state.managed_task_run:
                    if (
                        state.task_lifecycle_stage == "read_plan"
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "read"
                    ):
                        state.raw_task_check = state.tool_payload.get("task_check")
                        if isinstance(state.raw_task_check, dict):
                            state.task_check = state.raw_task_check
                        else:
                            state.task_check = self.task_store.check_tasks(
                                tenant_id=state.auth.tenant_id,
                                user_id=state.auth.user_id,
                                conversation_id=state.conversation_id,
                            )
                        state.task_lifecycle_stage, state.active_task_id = (
                            self._task_lifecycle_stage(state.task_check)
                        )
                        if state.task_lifecycle_stage == "final" and not state.task_check.get(
                            "complete"
                        ):
                            state.terminal_status = "blocked"
                    elif (
                        state.task_lifecycle_stage == "start_task"
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "update"
                    ):
                        state.task_lifecycle_stage = "work"
                        state.task_has_work_evidence = False
                    elif state.task_lifecycle_stage == "work":
                        if (
                            state.tool_name == "todo_write"
                            and str(state.item["arguments"].get("operation") or "") == "update"
                        ):
                            state.task_lifecycle_stage = "verify_task"
                        elif state.tool_name not in {
                            "get_tool_result",
                            "ask_user",
                            "ask_user_question",
                            "todo_write",
                            MCP_SEARCH_TOOLS,
                            MCP_GET_TOOL_SCHEMA,
                            MCP_GET_RESULT,
                        }:
                            state.task_has_work_evidence = True
                    elif (
                        state.task_lifecycle_stage in {"verify_task", "verify_final"}
                        and state.tool_name == "todo_write"
                        and str(state.item["arguments"].get("operation") or "") == "check"
                    ):
                        state.task_check = state.tool_payload
                        state.task_lifecycle_stage, state.active_task_id = (
                            self._task_lifecycle_stage(state.task_check)
                        )
                        if state.task_check.get("complete"):
                            state.task_lifecycle_stage = "final"
                        elif state.task_lifecycle_stage == "final":
                            state.terminal_status = "blocked"
                if (
                    str(state.tool_payload.get("status") or "").strip().lower() == "mode_required"
                    and state.tool_payload.get("required_mode") == "danger-full-access"
                    and state.run_id is not None
                ):
                    from app.deepspace.services.hook_gate import APPROVED_FULL_ACCESS_CALL

                    if APPROVED_FULL_ACCESS_CALL.get() is None:
                        escalation_approval = {
                            "approval_id": f"escalate_{state.call_id}",
                            "tool_name": state.tool_name,
                            "call_id": state.call_id,
                            "step_id": state.step_id,
                            "turn_index": state.round_index,
                            "tool_input": dict(state.item["arguments"]),
                            "required_mode": "danger-full-access",
                            "risk_level": "full-access",
                            "decision": "pending",
                            "message": f"{state.tool_name} wants to access resources outside the workspace boundary.",
                            "warning": str(state.tool_payload.get("message") or ""),
                        }
                        state.awaiting_approval = escalation_approval
                        self.runtime.update_checkpoint(
                            run_id=state.run_id,
                            status="awaiting_approval",
                            checkpoint={
                                "status": "awaiting_approval",
                                "turn_index": state.round_index,
                                "phase": "approval",
                                "pending_approval": escalation_approval,
                                "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                            },
                        )
                        self.runtime.record_step(
                            run_id=state.run_id,
                            tenant_id=state.auth.tenant_id,
                            user_id=state.auth.user_id,
                            conversation_id=state.conversation_id,
                            step_type="approval_requested",
                            status="awaiting_approval",
                            tool_name=state.tool_name,
                            tool_call_id=state.call_id,
                            input_json=dict(state.item["arguments"]),
                            result_json={
                                "approval_id": escalation_approval["approval_id"],
                                "risk_level": "full-access",
                                "required_mode": "danger-full-access",
                            },
                        )
                        yield sse("permission_request", escalation_approval)
                        yield sse("approval_request", escalation_approval)
                        state.terminal_status = "awaiting_approval"
                        state.control = "break"
                        return

                state.output = json.dumps(
                    state.tool_payload, ensure_ascii=False, separators=(",", ":")
                )
                # A successful call breaks any consecutive-failure streak for
                # this tool so a later, unrelated failure does not inherit it.
                if isinstance(state.consecutive_tool_failures, dict):
                    payload_status = str(state.tool_payload.get("status") or "").strip().lower()
                    if payload_status in {
                        "mode_required",
                        "denied",
                        "unavailable",
                        "failed",
                        "error",
                    }:
                        state.consecutive_tool_failures[state.tool_name] = (
                            int(state.consecutive_tool_failures.get(state.tool_name, 0) or 0) + 1
                        )
                        if (
                            state.consecutive_tool_failures[state.tool_name]
                            >= MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL
                        ):
                            self._request_model_final_answer_without_tools(
                                state,
                                reason=(
                                    f"{state.tool_name} failed or was blocked "
                                    f"({payload_status}) "
                                    f"{state.consecutive_tool_failures[state.tool_name]} times in a row."
                                ),
                            )
                    else:
                        state.consecutive_tool_failures[state.tool_name] = 0
            else:
                state.output = json.dumps(
                    {
                        "error": str(
                            state.result.get("error") or f"{state.tool_name} failed safely."
                        ),
                        "error_category": str(state.result.get("error_category") or "tool"),
                        "recoverable": bool(state.result.get("recoverable")),
                        **(
                            {"recovery": state.result["recovery"]}
                            if state.result.get("recovery")
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                state.tool_error_text = str(
                    state.result.get("error") or f"{state.tool_name} failed safely."
                ).strip()
                state.tool_execution_failure = (
                    f"{state.tool_name} failed: {state.tool_error_text[:1600]}"
                )
                state.tool_execution_failure_names.add(state.tool_name)
                if bool(state.result.get("recoverable")):
                    state.recoverable_tool_failure_count = (
                        int(state.recoverable_tool_failure_count or 0) + 1
                    )
                else:
                    state.tool_execution_failure_fatal = state.tool_execution_failure
                # Track consecutive failures of the same tool regardless of
                # argument variations. A model that keeps retrying a failing
                # read/glob with different paths is stuck, not making progress.
                if isinstance(state.consecutive_tool_failures, dict):
                    state.consecutive_tool_failures[state.tool_name] = (
                        int(state.consecutive_tool_failures.get(state.tool_name, 0) or 0) + 1
                    )
                    if (
                        state.consecutive_tool_failures[state.tool_name]
                        >= MAX_CONSECUTIVE_TOOL_FAILURES_PER_TOOL
                    ):
                        self._request_model_final_answer_without_tools(
                            state,
                            reason=(
                                f"{state.tool_name} failed "
                                f"{state.consecutive_tool_failures[state.tool_name]} times in a row."
                            ),
                        )
            if state.run_id is not None:
                self.runtime.record_step(
                    run_id=state.run_id,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    step_type="tool_result",
                    status=(
                        "blocked"
                        if state.guard_refusal
                        else ("completed" if state.success else "failed")
                    ),
                    tool_name=state.tool_name,
                    tool_call_id=state.call_id,
                    input_json=state.item["arguments"],
                    result_json={
                        "success": state.success,
                        "guard_refusal": state.guard_refusal,
                        "error_category": (
                            state.result.get("error_category") if not state.success else None
                        ),
                        "recoverable": (
                            bool(state.result.get("recoverable")) if not state.success else False
                        ),
                        "output": state.output,
                        **(
                            {"diff": state.tool_payload["diff"]}
                            if isinstance(state.tool_payload, dict)
                            and isinstance(state.tool_payload.get("diff"), dict)
                            else {}
                        ),
                        "turn_index": state.round_index,
                    },
                )
                # Persist the exact post-tool continuation point. A
                # reconnect or worker retry can resume from this
                # verified result without repeating the tool call.
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="running",
                    checkpoint={
                        "status": "running",
                        "phase": "tool_result",
                        "turn_index": state.round_index,
                        "tool_name": state.tool_name,
                        "tool_call_id": state.call_id,
                        "tool_success": state.success,
                        "next_phase": "model",
                        "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                    },
                )
            if state.success:
                payload_status = (
                    str(state.tool_payload.get("status") or "").strip().lower()
                    if isinstance(state.tool_payload, dict)
                    else ""
                )
                effective_tool_success = not state.guard_refusal and payload_status not in {
                    "error",
                    "failed",
                    "denied",
                    "blocked",
                    "mode_required",
                }
                yield sse(
                    "tool_result",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "tool_input": state.item["arguments"],
                        "output": state.output,
                        **(
                            {"diff": state.tool_payload["diff"]}
                            if effective_tool_success
                            and isinstance(state.tool_payload, dict)
                            and isinstance(state.tool_payload.get("diff"), dict)
                            else {}
                        ),
                        "success": effective_tool_success,
                        "guard_refusal": state.guard_refusal,
                        "turn_index": state.round_index,
                        "completed_at": self._now(),
                    },
                )
                if state.tool_name in CLARIFICATION_TOOL_NAMES and state.tool_payload.get(
                    "awaiting_user"
                ):
                    # Do not re-signal the same pending clarification. A resumed
                    # or replayed turn can reach this executor for a call id that
                    # is already awaiting an answer; emitting again would stack
                    # duplicate "Clarifying Objective" rows in the timeline.
                    already_signaled = bool(
                        state.awaiting_user
                        and str(state.awaiting_user.get("call_id") or "") == str(state.call_id)
                    )
                    if not already_signaled:
                        questions_payload = (
                            state.tool_payload.get("questions")
                            if isinstance(state.tool_payload.get("questions"), list)
                            else (
                                state.item["arguments"].get("questions")
                                if isinstance(state.item.get("arguments"), dict)
                                and isinstance(state.item["arguments"].get("questions"), list)
                                else None
                            )
                        )
                        first_question_item = (
                            questions_payload[0]
                            if questions_payload and isinstance(questions_payload[0], dict)
                            else {}
                        )
                        primary_question = (
                            str(
                                state.tool_payload.get("question")
                                or first_question_item.get("question")
                                or ""
                            ).strip()
                            or None
                        )
                        raw_options = state.tool_payload.get("options")
                        if not isinstance(raw_options, list) and isinstance(
                            first_question_item.get("options"), list
                        ):
                            raw_options = [
                                (
                                    f"{opt.get('label', '')} — {opt.get('description', '')}"
                                    if isinstance(opt, dict) and opt.get("description")
                                    else str(
                                        opt.get("label", "") if isinstance(opt, dict) else opt or ""
                                    )
                                ).strip()
                                for opt in first_question_item["options"]
                            ]
                        normalized_options = [
                            str(opt).strip() for opt in (raw_options or []) if str(opt).strip()
                        ]
                        state.awaiting_user = {
                            **state.tool_payload,
                            **(
                                {"questions": questions_payload}
                                if questions_payload is not None
                                else {}
                            ),
                            **({"question": primary_question} if primary_question else {}),
                            "options": normalized_options,
                            "question_id": str(uuid.uuid4()),
                            "call_id": state.call_id,
                            "step_id": state.step_id,
                            "tool_name": state.tool_name,
                            "tool_input": state.item["arguments"],
                            "turn_index": state.round_index,
                        }
                        yield sse(
                            "ask_user_question",
                            {
                                "tool_name": state.tool_name,
                                "tool_id": state.call_id,
                                "step_id": state.step_id,
                                "message": primary_question,
                                "options": normalized_options,
                                **(
                                    {"questions": questions_payload}
                                    if questions_payload is not None
                                    else {}
                                ),
                                "question_id": state.awaiting_user["question_id"],
                                "turn_index": state.round_index,
                            },
                        )
                if state.tool_name == "plan_submit" and state.tool_payload.get("plan"):
                    # The plan is with the user now.  The turn pauses so the
                    # agent does not start the work before the review.
                    state.plan_review = {
                        "plan_id": str(state.tool_payload["plan"].get("id") or ""),
                        "title": str(state.tool_payload["plan"].get("title") or ""),
                        "content": str(state.tool_payload["plan"].get("content") or ""),
                        "status": str(state.tool_payload["plan"].get("status") or "proposed"),
                        "call_id": state.call_id,
                        "step_id": state.step_id,
                        "turn_index": state.round_index,
                    }
                    yield sse(
                        "plan_submitted",
                        {
                            "plan_id": state.plan_review["plan_id"],
                            "title": state.plan_review["title"],
                            "content": state.plan_review["content"],
                            "status": state.plan_review["status"],
                            "tool_id": state.call_id,
                            "step_id": state.step_id,
                            "turn_index": state.round_index,
                        },
                    )
            else:
                state.error_category = str(state.result.get("error_category") or "tool")
                mode_payload = (
                    state.tool_payload
                    if state.tool_payload.get("status") == "mode_required"
                    else (
                        state.result.get("payload")
                        if isinstance(state.result.get("payload"), dict)
                        and state.result["payload"].get("status") == "mode_required"
                        else None
                    )
                )
                if (
                    state.result.get("error_code") == "MODE_REQUIRED"
                    or (
                        mode_payload is not None
                        and mode_payload.get("required_mode") == "danger-full-access"
                    )
                ) and state.run_id is not None:
                    from app.deepspace.services.hook_gate import APPROVED_FULL_ACCESS_CALL

                    if APPROVED_FULL_ACCESS_CALL.get() is None:
                        escalation_approval = {
                            "approval_id": f"escalate_{state.call_id}",
                            "tool_name": state.tool_name,
                            "call_id": state.call_id,
                            "step_id": state.step_id,
                            "turn_index": state.round_index,
                            "tool_input": dict(state.item["arguments"]),
                            "required_mode": "danger-full-access",
                            "risk_level": "full-access",
                            "decision": "pending",
                            "message": (
                                f"{state.tool_name} wants to access resources outside the workspace boundary."
                            ),
                            "warning": str(
                                (mode_payload or {}).get("message")
                                or state.result.get("error")
                                or "Switch to Full access before asking DeepSpace to use it."
                            ),
                        }
                        state.awaiting_approval = escalation_approval
                        self.runtime.update_checkpoint(
                            run_id=state.run_id,
                            status="awaiting_approval",
                            checkpoint={
                                "status": "awaiting_approval",
                                "turn_index": state.round_index,
                                "phase": "approval",
                                "pending_approval": escalation_approval,
                                "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                            },
                        )
                        self.runtime.record_step(
                            run_id=state.run_id,
                            tenant_id=state.auth.tenant_id,
                            user_id=state.auth.user_id,
                            conversation_id=state.conversation_id,
                            step_type="approval_requested",
                            status="awaiting_approval",
                            tool_name=state.tool_name,
                            tool_call_id=state.call_id,
                            input_json=dict(state.item["arguments"]),
                            result_json={
                                "approval_id": escalation_approval["approval_id"],
                                "risk_level": "full-access",
                                "required_mode": "danger-full-access",
                            },
                        )
                        yield sse("permission_request", escalation_approval)
                        yield sse("approval_request", escalation_approval)
                        state.terminal_status = "awaiting_approval"
                        state.control = "break"
                        return
                if state.result.get("requires_approval"):
                    # A hook asked about the call instead of refusing it.  The
                    # call did not run, and the turn hands it to the approval
                    # flow so the user decides.
                    state.tool_execution_failure = None
                    state.tool_execution_failure_fatal = None
                    state.tool_execution_failure_names.discard(state.tool_name)
                    state.awaiting_approval = self._hook_approval_request(state)
                    if state.run_id is not None and state.awaiting_approval is not None:
                        self.runtime.update_checkpoint(
                            run_id=state.run_id,
                            status="awaiting_approval",
                            checkpoint={
                                "status": "awaiting_approval",
                                "turn_index": state.round_index,
                                "phase": "approval",
                                "pending_approval": state.awaiting_approval,
                                "loop_guard": self._loop_guard_snapshot(state.conversation_id),
                            },
                        )
                        self.runtime.record_step(
                            run_id=state.run_id,
                            tenant_id=state.auth.tenant_id,
                            user_id=state.auth.user_id,
                            conversation_id=state.conversation_id,
                            step_type="approval_requested",
                            status="awaiting_approval",
                            tool_name=state.tool_name,
                            tool_call_id=state.call_id,
                            input_json=dict(state.item["arguments"]),
                            result_json={
                                "approval_id": str(
                                    state.awaiting_approval.get("approval_id") or ""
                                ),
                                "risk_level": str(state.awaiting_approval.get("risk_level") or ""),
                            },
                        )
                    yield sse("permission_request", state.awaiting_approval)
                    yield sse("approval_request", state.awaiting_approval)
                    break
                if state.mcp_bindings.get(state.tool_name) is not None:
                    state.mcp_error_text = str(state.result.get("error") or "").casefold()
                    state.policy_block_markers = (
                        "risk ceiling",
                        "read-only mode",
                        "blocked by policy",
                        "not in the connection allowlist",
                        "connection policy is not configured",
                    )
                    if any(marker in state.mcp_error_text for marker in state.policy_block_markers):
                        state.forced_answer = (
                            "The connected MCP action was blocked by its configured policy. "
                            "No external action was performed; update that connection's policy "
                            "only if this action is intended."
                        )
                        state.terminal_status = "blocked"
                    state.failure_key = hashlib.sha256(
                        json.dumps(
                            {
                                "tool": state.tool_name,
                                "category": state.error_category,
                                "error": str(state.result.get("error") or "")[:500],
                            },
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    state.repeated_mcp_failures[state.failure_key] = (
                        state.repeated_mcp_failures.get(state.failure_key, 0) + 1
                    )
                    if state.repeated_mcp_failures[state.failure_key] >= 2:
                        state.forced_answer = (
                            "The connected MCP service returned the same failure twice, so "
                            "DeepSpace stopped retrying to avoid a loop. Reconnect the service "
                            "or try again after it is healthy."
                        )
                        state.terminal_status = "blocked"
                yield sse(
                    "tool_error",
                    {
                        "tool_name": state.tool_name,
                        "tool_id": state.call_id,
                        "step_id": state.step_id,
                        "error": state.output,
                        "error_category": state.error_category,
                        "recoverable": bool(state.result.get("recoverable")),
                        **(
                            {"recovery": state.result["recovery"]}
                            if state.result.get("recovery")
                            else {}
                        ),
                    },
                )
            state.conversation_messages.append(
                {"role": "tool", "tool_call_id": state.call_id, "content": state.output}
            )
            if state.control == "break" or state.disable_tools_for_final_answer:
                break
        if state.tool_execution_failure_fatal is not None:
            raise DeepSpaceToolExecutionError(state.tool_execution_failure_fatal)
        if (
            not state.disable_tools_for_final_answer
            and int(state.recoverable_tool_failure_count or 0)
            >= MAX_RECOVERABLE_TOOL_FAILURES_PER_TURN
        ):
            self._request_model_final_answer_without_tools(
                state,
                reason="Several tool operations could not complete.",
            )
        self._record_session_event(
            auth=state.auth,
            conversation_id=state.conversation_id,
            run_id=state.run_id,
            event_type=STEP_END,
            turn=state.turn_number,
            step=state.round_index,
            payload={"phase": "model"},
        )

        # Inject repeat tool reminders into the conversation for the next round
        repeat_reminders = getattr(state, "repeat_tool_reminders", [])
        if repeat_reminders:
            for reminder in repeat_reminders:
                self._set_dynamic_system_instruction(
                    state.conversation_messages,
                    key=f"repeat_tool_reminder_{reminder.get('tool_name', 'unknown')}",
                    content=reminder.get("message", ""),
                )

        if (
            state.forced_answer is not None
            or state.awaiting_user is not None
            or state.awaiting_approval is not None
            or state.plan_review is not None
        ):
            state.control = "break"
            return
