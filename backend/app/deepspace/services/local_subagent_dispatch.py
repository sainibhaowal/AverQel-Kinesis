"""Bounded background execution for eager, local-first Celery deployments."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

LOCAL_SUBAGENT_WORKERS = 4
MAX_LOCAL_SUBAGENT_OUTSTANDING = 64

_executor = ThreadPoolExecutor(
    max_workers=LOCAL_SUBAGENT_WORKERS,
    thread_name_prefix="deepspace-subagent",
)
_lock = threading.Lock()
_queued = 0
_running = 0


def _run_task(task: Callable[..., Any], kwargs: dict[str, Any]) -> None:
    global _queued, _running
    with _lock:
        _queued = max(0, _queued - 1)
        _running += 1
    try:
        task(**kwargs)
    except Exception:  # noqa: BLE001 - the worker task persists its own failure state
        logger.warning("Local DeepSpace subagent task failed", exc_info=True)
    finally:
        with _lock:
            _running = max(0, _running - 1)


def submit_local_subagent_task(task: Callable[..., Any], *, kwargs: dict[str, Any]) -> bool:
    """Queue a durable child task without blocking the local API event loop.

    The task itself owns durable state in the database. This in-process queue
    only supplies bounded concurrency when Celery is configured eager for a
    desktop/local deployment.
    """
    global _queued
    with _lock:
        if _queued + _running >= MAX_LOCAL_SUBAGENT_OUTSTANDING:
            return False
        _queued += 1
    try:
        _executor.submit(_run_task, task, dict(kwargs))
    except Exception:  # noqa: BLE001
        with _lock:
            _queued = max(0, _queued - 1)
        logger.warning("Local DeepSpace subagent task could not be queued", exc_info=True)
        return False
    return True


def local_subagent_runtime_status() -> dict[str, int | str]:
    """Report embedded worker availability without leaking cross-user activity."""
    return {
        "mode": "embedded-background-pool",
        "status": "available",
        "worker_count": LOCAL_SUBAGENT_WORKERS,
        "capacity": LOCAL_SUBAGENT_WORKERS,
    }


__all__ = [
    "LOCAL_SUBAGENT_WORKERS",
    "MAX_LOCAL_SUBAGENT_OUTSTANDING",
    "local_subagent_runtime_status",
    "submit_local_subagent_task",
]
