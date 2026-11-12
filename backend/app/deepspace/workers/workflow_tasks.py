"""Celery worker for durable DeepSpace workflow jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select, text

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.models.workflow_job import DeepSpaceWorkflowJob
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.workflow_engine import WorkflowEngine
from app.deepspace.services.workflow_jobs import WorkflowJobStore
from app.platform.database.session import get_session_factory, set_db_tenant_context
from app.platform.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="deepspace.workflow_run")  # type: ignore[misc]
def run_workflow_job_task(
    self,
    *,
    job_id: str,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
) -> str:
    """Claim and execute one tenant-scoped workflow job."""
    del self
    session = get_session_factory()()
    parsed_job_id = uuid.UUID(job_id)
    parsed_tenant_id = uuid.UUID(tenant_id)
    parsed_user_id = uuid.UUID(user_id)
    parsed_conversation_id = uuid.UUID(conversation_id)
    try:
        bind = getattr(session, "bind", None)
        if getattr(getattr(bind, "dialect", None), "name", None) != "sqlite":
            session.execute(text("SET ROLE aks_app"))
        set_db_tenant_context(session, parsed_tenant_id)
        job = session.execute(
            select(DeepSpaceWorkflowJob).where(
                DeepSpaceWorkflowJob.id == parsed_job_id,
                DeepSpaceWorkflowJob.tenant_id == parsed_tenant_id,
                DeepSpaceWorkflowJob.user_id == parsed_user_id,
                DeepSpaceWorkflowJob.conversation_id == parsed_conversation_id,
            )
        ).scalar_one_or_none()
        if job is None:
            raise RuntimeError("The workflow job does not exist in the requested tenant scope.")
        if job.status in {"completed", "failed", "cancelled"}:
            return job.status
        if job.cancel_requested:
            WorkflowJobStore(session).set_state(job, status="cancelled", stop_reason="cancelled")
            return "cancelled"

        settings = get_settings()
        auth = AuthContext(
            user_id=parsed_user_id,
            tenant_id=parsed_tenant_id,
            roles=frozenset(str(value) for value in (job.roles or [])),
            permissions=frozenset(str(value) for value in (job.permissions or [])),
            token_id=f"deepspace-workflow:{job.id}",
            auth_type="worker",
        )
        service = DeepSpaceChatService(db=session, settings=settings)
        context = service.build_tool_context(
            auth=auth,
            conversation_id=parsed_conversation_id,
            request=None,
            workspace_root=None,
        )
        engine = WorkflowEngine(settings, context)
        asyncio.run(engine.run_persisted_job(job))
        session.commit()
        return job.status
    except Exception as exc:  # noqa: BLE001 - persist a safe terminal state
        session.rollback()
        job = session.get(DeepSpaceWorkflowJob, parsed_job_id)
        if job is not None and job.tenant_id == parsed_tenant_id:
            WorkflowJobStore(session).set_state(
                job,
                status="failed",
                stop_reason="error",
                error=str(exc)[:20_000] or exc.__class__.__name__,
            )
        logger.warning("DeepSpace workflow worker failed", exc_info=True)
        raise
    finally:
        try:
            session.rollback()
            bind = getattr(session, "bind", None)
            if getattr(getattr(bind, "dialect", None), "name", None) != "sqlite":
                session.execute(text("RESET ROLE"))
                session.commit()
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            session.rollback()
        session.close()


__all__ = ["run_workflow_job_task"]
