"""DeepSpace Jobs Service — native Python implementation of @deepseek-ai/dsh-jobs."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.deepspace.services.jobs.brand import (
    JobChannel,
    JobId,
    JobKind,
    job_channel,
    job_id,
    job_kind,
)
from app.deepspace.services.jobs.config import JobsConfig
from app.deepspace.services.jobs.local_registry import LocalJobRegistry
from app.deepspace.services.jobs.registry import JobRegistry
from app.deepspace.services.jobs.tools import (
    JOB_KILL_TOOL,
    JOB_LIST_TOOL,
    JOB_OUTPUT_TOOL,
    JOBS_TOOLS,
)
from app.deepspace.services.jobs.types import (
    JobAppendOptions,
    JobEvent,
    JobEventFilter,
    JobEventListener,
    JobEvents,
    JobHandle,
    JobHooks,
    JobOutcome,
    JobOutputSource,
    JobSourceRead,
    JobSpec,
    JobToolCall,
)
from app.deepspace.services.jobs.view import (
    JobChunk,
    JobOutput,
    JobOutputRead,
    JobRead,
    JobRegisteredData,
    JobSettleCause,
    JobSettledData,
    JobStatus,
    JobView,
)

# Global registry instance
_JOBS_REGISTRY: LocalJobRegistry | None = None


def get_jobs_registry(settings: Settings | None = None) -> LocalJobRegistry:
    """Get or create the global jobs registry."""
    global _JOBS_REGISTRY
    if _JOBS_REGISTRY is None:
        from app.core.config import get_settings

        cfg = JobsConfig.from_settings(settings or get_settings())
        _JOBS_REGISTRY = LocalJobRegistry(
            ctx=None, config=cfg.__dict__ if hasattr(cfg, "__dict__") else {}
        )
    return _JOBS_REGISTRY


def init_jobs_registry(ctx: Any, settings: Settings) -> LocalJobRegistry:
    """Initialize the jobs registry with a context."""
    global _JOBS_REGISTRY
    cfg = JobsConfig.from_settings(settings)
    _JOBS_REGISTRY = LocalJobRegistry(ctx=ctx, config=cfg.__dict__)
    return _JOBS_REGISTRY


__all__ = [
    # Brand
    "JobId",
    "job_id",
    "JobKind",
    "job_kind",
    "JobChannel",
    "job_channel",
    # View types
    "JobStatus",
    "JobSettleCause",
    "JobChunk",
    "JobOutput",
    "JobView",
    "JobRead",
    "JobOutputRead",
    "JobRegisteredData",
    "JobSettledData",
    # Types
    "JobToolCall",
    "JobOutcome",
    "JobSourceRead",
    "JobOutputSource",
    "JobAppendOptions",
    "JobHandle",
    "JobHooks",
    "JobSpec",
    "JobEvent",
    "JobEventFilter",
    "JobEventListener",
    "JobEvents",
    # Registry
    "JobRegistry",
    "LocalJobRegistry",
    # Config
    "JobsConfig",
    # Factory
    "get_jobs_registry",
    "init_jobs_registry",
    # Tools
    "JOBS_TOOLS",
    "JOB_OUTPUT_TOOL",
    "JOB_LIST_TOOL",
    "JOB_KILL_TOOL",
]
