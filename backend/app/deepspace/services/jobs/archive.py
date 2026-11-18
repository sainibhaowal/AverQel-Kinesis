"""Archive admission for DeepSpace Jobs."""

from __future__ import annotations

from typing import Any

from app.deepspace.services.jobs.registry import JobRegistry


def install_job_archive_admission(ctx: Any, registry: JobRegistry) -> None:
    """
    Install archive admission hook.

    The Workspace registry asks what still runs for a Session before hiding it;
    owned jobs answer here for every implementation.
    """
    # This is a placeholder for the archive admission integration
    # In DSH, this connects to the workspace registry
    # For DeepSpace, we'd integrate with the workspace service
    pass


__all__ = ["install_job_archive_admission"]
