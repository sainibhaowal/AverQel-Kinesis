"""Client-safe projection vocabulary for DeepSpace Jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.deepspace.services.jobs.brand import JobChannel, JobId, JobKind

JobStatus = Literal["running", "stopping", "completed", "killed", "failed"]
JobSettleCause = Literal["producer", "kill", "teardown"]


@dataclass(frozen=True, slots=True)
class JobChunk:
    """One chunk of output from a job's ring buffer."""

    text: str
    offset: int
    channel: JobChannel | None = None
    gap_before: bool = False


@dataclass(frozen=True, slots=True)
class JobOutput:
    """Output metadata for a job projection."""

    total: int
    earliest: int
    spill_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobView:
    """Read-only projection of a job's state, safe for model/tool consumption."""

    id: JobId
    kind: JobKind
    label: str
    status: JobStatus
    owner: str | None = None
    output_limit_bytes: int | None = None
    progress: str | None = None
    detail: str | None = None
    started_at: int = 0
    finished_at: int | None = None
    output: JobOutput | None = None


@dataclass(frozen=True, slots=True)
class JobRead:
    """Result of a consuming read from the model's cursor."""

    chunks: tuple[JobChunk, ...]
    lossy: bool
    result: str | None
    job: JobView


@dataclass(frozen=True, slots=True)
class JobOutputRead:
    """Result of a non-consuming read at an absolute offset."""

    chunks: tuple[JobChunk, ...]
    next: int
    lossy: bool


@dataclass(frozen=True, slots=True)
class JobRegisteredData:
    """Log-only job registration event data."""

    job_id: JobId
    owner: str
    kind: JobKind
    label: str
    started_at: int
    tool_call: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class JobSettledData:
    """Log-only job settlement event data."""

    job_id: JobId
    owner: str
    kind: JobKind
    label: str
    started_at: int
    finished_at: int
    status: JobStatus
    cause: JobSettleCause
    producer_reported: bool
    detail: str | None = None
    tool_call: dict[str, Any] | None = None


__all__ = [
    "JobStatus",
    "JobSettleCause",
    "JobChunk",
    "JobOutput",
    "JobView",
    "JobRead",
    "JobOutputRead",
    "JobRegisteredData",
    "JobSettledData",
    "JobId",
    "JobKind",
    "JobChannel",
]
