"""Durable workflow lifecycle, progress, and cancellation primitives."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deepspace.models.workflow_job import DeepSpaceWorkflowJob, DeepSpaceWorkflowJobEvent
from app.deepspace.services.session_events import SessionEventStore
from app.realtime.event_bus import publish_event_sync
from app.system.services.cache_service import get_redis_client

WORKFLOW_START = "workflow/start"
WORKFLOW_PHASE = "workflow/phase"
WORKFLOW_LOG = "workflow/log"
WORKFLOW_AGENT_START = "workflow/agent-start"
WORKFLOW_AGENT_END = "workflow/agent-end"
WORKFLOW_END = "workflow/end"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _safe_payload(payload: dict[str, Any] | None) -> dict[str, object]:
    """Keep event payloads JSON-safe without allowing a bad value to stop a run."""
    source = payload or {}
    safe: dict[str, object] = {}
    for key, value in source.items():
        try:
            import json

            json.dumps(value, default=str)
            safe[str(key)] = value
        except (TypeError, ValueError):
            safe[str(key)] = str(value)
    return safe


class WorkflowJobStore:
    """Tenant-scoped persistence for a workflow job and its ordered events."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        mode: str,
        workflow_name: str,
        script: str | None,
        input_text: str | None,
        meta: dict[str, Any],
        args: dict[str, Any],
        roles: list[str] | None = None,
        permissions: list[str] | None = None,
    ) -> DeepSpaceWorkflowJob:
        job = DeepSpaceWorkflowJob(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            mode=mode,
            workflow_name=workflow_name,
            script=script,
            input_text=input_text,
            meta=_safe_payload(meta),
            args=_safe_payload(args),
            roles=list(roles or []),
            permissions=list(permissions or []),
            status="queued",
            result={},
        )
        self.db.add(job)
        self.db.flush()
        return job

    def append(
        self,
        job: DeepSpaceWorkflowJob,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        commit: bool = True,
    ) -> DeepSpaceWorkflowJobEvent:
        """Append an event and publish an advisory realtime snapshot."""
        current = self.db.execute(
            select(func.max(DeepSpaceWorkflowJobEvent.sequence)).where(
                DeepSpaceWorkflowJobEvent.job_id == job.id
            )
        ).scalar()
        event_payload = _safe_payload(payload)
        event = DeepSpaceWorkflowJobEvent(
            job_id=job.id,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            sequence=int(current or 0) + 1,
            event_type=event_type,
            payload=event_payload,
        )
        self.db.add(event)
        # Keep the common conversation timeline aware of workflow lifecycle
        # events as well as the workflow-specific stream.  ``run_id`` is left
        # empty deliberately: workflow jobs are not agent-run rows.
        SessionEventStore(self.db).append(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            event_type=event_type,
            payload=event_payload,
            tool_name="workflow_run",
        )
        if event_type == WORKFLOW_PHASE:
            job.current_phase = str(event_payload.get("title") or "")[:255] or None
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        self._publish(job, event)
        return event

    def set_state(
        self,
        job: DeepSpaceWorkflowJob,
        *,
        status: str,
        stop_reason: str | None = None,
        result: dict[str, Any] | None = None,
        output: str | None = None,
        error: str | None = None,
        commit: bool = True,
    ) -> None:
        now = datetime.now(UTC)
        job.status = status
        job.stop_reason = stop_reason
        if result is not None:
            job.result = _safe_payload(result)
        if output is not None:
            job.output = output[:200_000]
        if error is not None:
            job.error = error[:20_000]
        job.updated_at = now
        if status == "running" and job.started_at is None:
            job.started_at = now
        if status in {"completed", "failed", "cancelled"}:
            job.completed_at = now
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        # A status change is the one thing an observer cannot infer from the
        # event stream alone, so it is published as well as stored.
        self._publish(
            job,
            None,
            event_type=WORKFLOW_LOG,
            payload={
                "status": status,
                "stop_reason": stop_reason,
                "error": error,
            },
        )

    def cancel(self, job: DeepSpaceWorkflowJob, *, reason: str = "cancelled") -> None:
        if job.status in {"completed", "failed", "cancelled"}:
            return
        job.cancel_requested = True
        job.stop_reason = reason
        job.updated_at = datetime.now(UTC)
        self.db.commit()
        self._publish(job, None, event_type=WORKFLOW_PHASE, payload={"title": "cancelling"})

    def events(
        self, job: DeepSpaceWorkflowJob, *, limit: int = 500
    ) -> list[DeepSpaceWorkflowJobEvent]:
        return list(
            self.db.execute(
                select(DeepSpaceWorkflowJobEvent)
                .where(
                    DeepSpaceWorkflowJobEvent.job_id == job.id,
                    DeepSpaceWorkflowJobEvent.tenant_id == job.tenant_id,
                    DeepSpaceWorkflowJobEvent.user_id == job.user_id,
                    DeepSpaceWorkflowJobEvent.conversation_id == job.conversation_id,
                )
                .order_by(DeepSpaceWorkflowJobEvent.sequence.asc())
                .limit(max(1, min(limit, 5_000)))
            )
            .scalars()
            .all()
        )

    @staticmethod
    def projection(
        job: DeepSpaceWorkflowJob, events: list[DeepSpaceWorkflowJobEvent] | None = None
    ) -> dict[str, Any]:
        return {
            "run_id": str(job.id),
            "job_id": str(job.id),
            "workflow": job.workflow_name,
            "mode": job.mode,
            "status": job.status,
            "stop_reason": job.stop_reason,
            "current_phase": job.current_phase,
            "cancel_requested": bool(job.cancel_requested),
            "agents_spawned": int(job.agents_spawned or 0),
            "result": dict(job.result or {}),
            "output": job.output,
            "error": job.error,
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "created_at": _iso(job.created_at),
            "events": [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "payload": dict(event.payload or {}),
                    "created_at": _iso(event.created_at),
                }
                for event in (events or [])
            ],
        }

    @staticmethod
    def _publish(
        job: DeepSpaceWorkflowJob,
        event: DeepSpaceWorkflowJobEvent | None,
        *,
        event_type: str = WORKFLOW_LOG,
        payload: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "run_id": str(job.id),
            "job_id": str(job.id),
            "workflow": job.workflow_name,
            "status": job.status,
            "current_phase": job.current_phase,
            "stop_reason": job.stop_reason,
            "agents_spawned": int(job.agents_spawned or 0),
        }
        if event is not None:
            data.update(
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": dict(event.payload or {}),
                }
            )
            event_type = event.event_type
        if payload:
            data["payload"] = payload
        try:
            publish_event_sync(
                get_redis_client(),
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                event_type=event_type,
                resource="workflow-runs",
                data=data,
            )
        except Exception:  # noqa: BLE001 - realtime is advisory; the DB is authoritative
            return


def workflow_event_session_type(event_type: str) -> str:
    """Return a typed-session-log name for a workflow event."""
    return event_type


__all__ = [
    "WORKFLOW_AGENT_END",
    "WORKFLOW_AGENT_START",
    "WORKFLOW_END",
    "WORKFLOW_LOG",
    "WORKFLOW_PHASE",
    "WORKFLOW_START",
    "WorkflowJobStore",
]
