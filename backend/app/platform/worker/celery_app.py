from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from celery import Celery  # type: ignore[import-untyped]
from celery.app.task import Task  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.platform.database import model_registry  # noqa: F401

settings = get_settings()

is_eager = settings.celery_task_always_eager or settings.redis_url.startswith("memory://")
broker_url = "memory://" if is_eager else settings.redis_url
backend_url = "cache+memory://" if is_eager else settings.redis_url

celery_app = Celery(
    "aks-worker",
    broker=broker_url,
    backend=backend_url,
    include=[
        "app.ingestion.workers.tasks",
        "app.documents.workers.tasks_webhooks",
        "app.documents.workers.tasks_classification",
        "app.system.workers.tasks_maintenance",
        "app.system.workers.tasks_retention",
        "app.system.workers.tasks_notifications",
        "app.integrations.workers.tasks_connectors",
        "app.integrations.workers.tasks_mcp",
        "app.integrations.workers.tasks_mcp_catalog",
        "app.deepspace.workers.tasks",
        "app.deepspace.workers.subagent_tasks",
        "app.deepspace.workers.library_uploads",
        "app.deepspace.workers.schedules",
        "app.query.workers.dataset_indexing",
    ],
)

celery_app.conf.update(
    task_always_eager=is_eager,
    task_eager_propagates=is_eager,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_default_queue="ingestion_light",
    task_routes={
        "ingestion.process_job": {"queue": "ingestion_heavy"},
        "ingestion.ping": {"queue": "ingestion_light"},
        "documents.*": {"queue": "maintenance"},
        "maintenance.process_data_deletion": {"queue": "maintenance"},
        "maintenance.retention_cleanup": {"queue": "maintenance"},
        "notifications.*": {"queue": "maintenance"},
        "maintenance.heartbeat": {"queue": "maintenance"},
        "maintenance.storage_cleanup": {"queue": "maintenance"},
        "maintenance.deepspace_context_cleanup": {"queue": "maintenance"},
        "maintenance.storage_retention_scan": {"queue": "maintenance"},
        "app.integrations.workers.tasks_connectors.*": {"queue": "maintenance"},
        "mcp.refresh_server_catalog": {"queue": "mcp_catalog"},
        "mcp.*": {"queue": "maintenance"},
        "mcp.sync_official_catalog": {"queue": "maintenance"},
        "deepspace.run": {"queue": "deepspace"},
        # A delegated child's own turn runs a model loop, so it belongs on
        # the model queue rather than behind document extraction.
        "deepspace.subagent_turn": {"queue": "deepspace"},
        # Queue dispatch is short database coordination. Keep it off the
        # long-running model queue so active generations cannot starve it.
        "deepspace.dispatch_turn_queue": {"queue": "deepspace_control"},
        # Large Library imports are intentionally isolated from interactive
        # DeepSpace chat turns. File publication and document extraction have
        # separate queues so slow OCR cannot block attachment availability.
        "deepspace.library_upload_finalize": {"queue": "library_uploads"},
        "deepspace.library_file_extract": {"queue": "library_extraction"},
        "deepspace.library_dataset_profile": {"queue": "dataset_indexing"},
        "deepspace.library_media_derivative": {"queue": "media_derivatives"},
        "deepspace.dispatch_schedules": {"queue": "deepspace"},
        "deepspace.artifact_create": {"queue": "deepspace"},
        "deepspace.index_conversation": {"queue": "dataset_indexing"},
        "queries.index_dataset": {"queue": "grounded_query_datasets"},
    },
    beat_schedule={
        "maintenance-heartbeat": {
            "task": "maintenance.heartbeat",
            "schedule": crontab(minute="*/5"),
        },
        "maintenance-retention-cleanup": {
            "task": "maintenance.retention_cleanup",
            "schedule": crontab(hour=2, minute=0),
        },
        "notifications-email-outbox": {
            "task": "notifications.dispatch_email_outbox",
            "schedule": crontab(minute="*"),
        },
        "notifications-support-sla": {
            "task": "notifications.check_support_sla",
            "schedule": crontab(minute="*/5"),
        },
        "notifications-retention-cleanup": {
            "task": "notifications.cleanup_retention",
            "schedule": crontab(hour=4, minute=20, day_of_week="sun"),
        },
        "maintenance-storage-cleanup": {
            "task": "maintenance.storage_cleanup",
            "schedule": crontab(minute="*/5"),
        },
        "maintenance-deepspace-context-cleanup": {
            "task": "maintenance.deepspace_context_cleanup",
            "schedule": crontab(hour=3, minute=30),
        },
        # Policy thresholds are evaluated monthly; the user setting controls
        # the age. Automatic archive is opt-in for local/staging only and
        # permanent purge has no worker or schedule.
        "maintenance-storage-retention-scan": {
            "task": "maintenance.storage_retention_scan",
            "schedule": crontab(hour=4, minute=0, day_of_month="1"),
        },
        "documents-classification-schedules": {
            "task": "documents.apply_classification_schedules",
            "schedule": crontab(minute="*/5"),
        },
        "documents-webhook-outbox": {
            "task": "documents.dispatch_pending_webhook_deliveries",
            "schedule": crontab(minute="*"),
        },
        "connector-sync-all": {
            "task": "app.integrations.workers.tasks_connectors.sync_all_connectors",
            "schedule": crontab(minute=0),  # Every hour
        },
        "mcp-refresh-enabled-servers": {
            "task": "mcp.refresh_enabled_servers",
            "schedule": crontab(minute="*/2"),
        },
        "mcp-sync-official-catalog": {
            "task": "mcp.sync_official_catalog",
            "schedule": crontab(hour=3, minute=17),
        },
        "deepspace-dispatch-schedules": {
            "task": "deepspace.dispatch_schedules",
            "schedule": crontab(minute="*"),
        },
    },
)

_eager_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="celery-eager")
_orig_apply_async = Task.apply_async


def _eager_aware_apply_async(
    self: Task,
    args: Any = None,
    kwargs: Any = None,
    task_id: Any = None,
    **options: Any,
) -> Any:
    """Run tasks in a dedicated worker thread pool when eager execution is triggered from an event loop."""
    app = getattr(self, "_get_app", lambda: celery_app)()
    if not app.conf.task_always_eager:
        return _orig_apply_async(self, args=args, kwargs=kwargs, task_id=task_id, **options)

    in_loop = False
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    is_bg_task = self.name in {
        "deepspace.run",
        "deepspace.dispatch_turn_queue",
        "deepspace.artifact_create",
        "ingestion.process_job",
    }

    if in_loop or is_bg_task:
        resolved_task_id = str(task_id or uuid.uuid4())
        _eager_executor.submit(
            self.apply,
            args=args,
            kwargs=kwargs,
            task_id=resolved_task_id,
            **options,
        )
        return self.AsyncResult(resolved_task_id)

    return _orig_apply_async(self, args=args, kwargs=kwargs, task_id=task_id, **options)


Task.apply_async = _eager_aware_apply_async
