"""Types shared by job producers, the registry, and its consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.deepspace.services.jobs.brand import JobChannel, JobId, JobKind
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

SessionEventMap: dict[str, Any] = {
    "job/registered": JobRegisteredData,
    "job/settled": JobSettledData,
}


@dataclass(frozen=True, slots=True)
class JobToolCall:
    """Tool dispatch that created a job; nested calls retain its root invocation."""

    call_id: str
    root_call_id: str


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Terminal result supplied by a producer through ``JobHooks.done``."""

    status: Literal["completed", "killed", "failed"]
    detail: str | None = None
    result: str | None = None


@dataclass(frozen=True, slots=True)
class JobSourceRead:
    """One incremental read from a ``JobOutputSource``."""

    text: str
    next_offset: int
    lossy: bool
    spill_path: str | None = None


class JobOutputSource:
    """A pull source the registry pumps into the job's output ring."""

    def __init__(
        self,
        read: Callable[[int], JobSourceRead],
        channel: JobChannel | None = None,
    ) -> None:
        self._read = read
        self.channel = channel

    def read(self, from_byte: int) -> JobSourceRead:
        return self._read(from_byte)


@dataclass(frozen=True, slots=True)
class JobAppendOptions:
    """Options for one ``JobHandle.append``."""

    channel: JobChannel | None = None
    gap_before: bool = False


class JobHandle:
    """Producer face of one registered job."""

    def __init__(
        self,
        job_id: JobId,
        append: Callable[[str, JobAppendOptions | None], None],
        update_progress: Callable[[str], None],
    ) -> None:
        self.id = job_id
        self._append = append
        self._update_progress = update_progress

    def append(self, text: str, options: JobAppendOptions | None = None) -> None:
        self._append(text, options)

    def update_progress(self, line: str) -> None:
        self._update_progress(line)


@dataclass(frozen=True, slots=True)
class JobHooks:
    """Hooks through which the runtime controls and observes producer work."""

    cancel: Callable[[str | None], None]
    done: asyncio.Future[JobOutcome]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Producer declaration passed to ``JobRegistry.start``."""

    kind: JobKind
    label: str
    owner: str | None = None
    tool_call: JobToolCall | None = None
    output_limit_bytes: int | None = None
    output: list[JobOutputSource] = field(default_factory=list)

    def run(self, job: JobHandle) -> JobHooks:
        raise NotImplementedError("JobSpec.run must be implemented by producer")


JobEvent = Any
JobEventFilter = dict[str, Any]
JobEventListener = Callable[[JobEvent], None]


class JobEvents:
    """The registry's event stream."""

    def subscribe(
        self,
        filter: JobEventFilter,
        listener: JobEventListener,
    ) -> Callable[[], None]:
        raise NotImplementedError()


__all__ = [
    "JobId",
    "JobKind",
    "JobChannel",
    "JobView",
    "JobRead",
    "JobOutputRead",
    "JobRegisteredData",
    "JobSettledData",
    "JobChunk",
    "JobOutput",
    "JobStatus",
    "JobSettleCause",
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
]
