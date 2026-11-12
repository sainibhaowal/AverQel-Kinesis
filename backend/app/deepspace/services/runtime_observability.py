"""Read-only, account-scoped live activity for the DeepSpace Runtime page."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.deepspace.models.agent_runtime import DeepSpaceAgentRun, DeepSpaceAgentStep
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.conversation_execution_limiter import _limiter_redis
from app.deepspace.services.local_subagent_dispatch import local_subagent_runtime_status
from app.deepspace.services.runtime_store import ACTIVE_RUN_STATUSES
from app.deepspace.services.session_events import TOOL_CALL, TOOL_RESULT
from app.platform.database.session import set_db_tenant_context
from app.platform.worker.celery_app import celery_app

_WORKER_PING_TIMEOUT_SECONDS = 0.35
_ACTIVE_CHILD_STATUSES = {"queued", "running"}
_FAILED_STATUSES = {"error", "failed", "max_rounds", "timeout"}


def _celery_worker_status() -> dict[str, Any]:
    """Probe worker reachability without exposing task arguments or other users' counts."""
    try:
        inspector = celery_app.control.inspect(timeout=_WORKER_PING_TIMEOUT_SECONDS)
        responses = inspector.ping() if inspector is not None else None
    except Exception:  # noqa: BLE001 - health reporting must not fail runtime settings
        responses = None
    worker_count = len(responses) if isinstance(responses, dict) else 0
    return {
        "mode": "celery",
        "status": "available" if worker_count else "unavailable",
        "worker_count": worker_count,
        "queued": None,
        "running": None,
    }


def _worker_status() -> dict[str, Any]:
    if bool(getattr(celery_app.conf, "task_always_eager", False)):
        return local_subagent_runtime_status()
    return _celery_worker_status()


def _execution_gate_status() -> dict[str, str]:
    """Report whether the shared root/child tool limit can reach Redis."""
    try:
        available = bool(_limiter_redis().ping())
    except Exception:  # noqa: BLE001 - report health without affecting runtime settings
        available = False
    return {"status": "available" if available else "unavailable"}


def runtime_activity_status(*, db: Session, auth: AuthContext, settings: Any) -> dict[str, Any]:
    """Return current worker, agent, and tool activity scoped to this user.

    The tool list contains names only. It never includes tool arguments,
    results, prompts, credentials, or data from another tenant or account.
    """
    set_db_tenant_context(db, auth.tenant_id)
    now = datetime.now(UTC)
    recent_since = now - timedelta(hours=24)
    root_runs = db.execute(
        select(DeepSpaceAgentRun.id, DeepSpaceAgentRun.status, DeepSpaceAgentRun.updated_at)
        .where(
            DeepSpaceAgentRun.tenant_id == auth.tenant_id,
            DeepSpaceAgentRun.user_id == auth.user_id,
            DeepSpaceAgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(DeepSpaceAgentRun.updated_at.desc())
        .limit(100)
    ).all()
    root_run_ids = [row.id for row in root_runs]

    children = db.execute(
        select(
            DeepSpaceSubagentRun.id,
            DeepSpaceSubagentRun.label,
            DeepSpaceSubagentRun.status,
            DeepSpaceSubagentRun.busy,
            DeepSpaceSubagentRun.delegation_depth,
            DeepSpaceSubagentRun.updated_at,
        )
        .where(
            DeepSpaceSubagentRun.tenant_id == auth.tenant_id,
            DeepSpaceSubagentRun.user_id == auth.user_id,
            DeepSpaceSubagentRun.status.in_(_ACTIVE_CHILD_STATUSES),
        )
        .order_by(DeepSpaceSubagentRun.updated_at.desc())
        .limit(100)
    ).all()
    active_child_ids = [row.id for row in children if bool(row.busy)]

    active_tools: list[dict[str, Any]] = []
    if root_run_ids:
        root_steps = db.execute(
            select(
                DeepSpaceAgentStep.run_id,
                DeepSpaceAgentStep.step_type,
                DeepSpaceAgentStep.status,
                DeepSpaceAgentStep.tool_name,
                DeepSpaceAgentStep.tool_call_id,
                DeepSpaceAgentStep.created_at,
            )
            .where(
                DeepSpaceAgentStep.tenant_id == auth.tenant_id,
                DeepSpaceAgentStep.user_id == auth.user_id,
                DeepSpaceAgentStep.run_id.in_(root_run_ids),
                DeepSpaceAgentStep.step_type.in_({"tool_start", "tool_result"}),
                DeepSpaceAgentStep.tool_call_id.is_not(None),
            )
            .order_by(DeepSpaceAgentStep.run_id, DeepSpaceAgentStep.sequence.desc())
            .limit(2_000)
        ).all()
        seen_calls: set[tuple[uuid.UUID, str]] = set()
        for row in root_steps:
            key = (row.run_id, str(row.tool_call_id))
            if key in seen_calls:
                continue
            seen_calls.add(key)
            if row.step_type == "tool_start" and row.status == "running":
                active_tools.append(
                    {
                        "tool_name": str(row.tool_name or "unknown"),
                        "agent": "Main agent",
                        "started_at": row.created_at.isoformat() if row.created_at else None,
                    }
                )

    if active_child_ids:
        child_events = db.execute(
            select(
                DeepSpaceSessionEvent.subagent_id,
                DeepSpaceSessionEvent.event_type,
                DeepSpaceSessionEvent.tool_name,
            )
            .where(
                DeepSpaceSessionEvent.tenant_id == auth.tenant_id,
                DeepSpaceSessionEvent.user_id == auth.user_id,
                DeepSpaceSessionEvent.subagent_id.in_(active_child_ids),
                DeepSpaceSessionEvent.event_type.in_({TOOL_CALL, TOOL_RESULT}),
            )
            .order_by(DeepSpaceSessionEvent.seq.desc())
            .limit(2_000)
        ).all()
        unmatched_results: Counter[tuple[uuid.UUID, str]] = Counter()
        active_child_tool_names: set[tuple[uuid.UUID, str]] = set()
        for row in child_events:
            tool_name = str(row.tool_name or "unknown")
            key = (row.subagent_id, tool_name)
            if row.event_type == TOOL_RESULT:
                unmatched_results[key] += 1
            elif unmatched_results[key]:
                unmatched_results[key] -= 1
            else:
                active_child_tool_names.add(key)
        child_labels = {row.id: str(row.label or "Child agent") for row in children}
        active_tools.extend(
            {
                "tool_name": tool_name,
                "agent": child_labels.get(child_id, "Child agent"),
                "started_at": None,
            }
            for child_id, tool_name in active_child_tool_names
        )

    failed_root = db.execute(
        select(func.count()).where(
            DeepSpaceAgentRun.tenant_id == auth.tenant_id,
            DeepSpaceAgentRun.user_id == auth.user_id,
            DeepSpaceAgentRun.status.in_(_FAILED_STATUSES),
            DeepSpaceAgentRun.updated_at >= recent_since,
        )
    ).scalar_one()
    failed_children = db.execute(
        select(func.count()).where(
            DeepSpaceSubagentRun.tenant_id == auth.tenant_id,
            DeepSpaceSubagentRun.user_id == auth.user_id,
            DeepSpaceSubagentRun.status.in_(_FAILED_STATUSES),
            DeepSpaceSubagentRun.updated_at >= recent_since,
        )
    ).scalar_one()

    queued = sum(1 for row in children if row.status == "queued" and not row.busy)
    running = sum(1 for row in children if bool(row.busy)) + sum(
        1 for row in root_runs if row.status == "running"
    )
    agents = [
        {
            "label": str(row.label or "Child agent"),
            "status": "running" if row.busy else str(row.status),
            "depth": int(row.delegation_depth or 0),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in children
    ]
    agents.extend(
        {
            "label": "Main agent",
            "status": str(row.status),
            "depth": 0,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in root_runs
    )
    return {
        "status": "available",
        "observed_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scope": "current user and tenant; worker reachability is deployment-wide",
        "workers": _worker_status(),
        "execution_gate": _execution_gate_status(),
        "jobs": {
            "queued": queued,
            "running": running,
            "failed_last_24h": int(failed_root or 0) + int(failed_children or 0),
        },
        "agents": agents[:50],
        "active_tools": active_tools[:100],
    }


__all__ = ["runtime_activity_status"]
