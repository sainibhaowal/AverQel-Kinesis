"""Process-local implementation of the DeepSpace Job Registry."""

from __future__ import annotations

import asyncio
import builtins
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.deepspace.services.jobs.brand import JobId, JobKind
from app.deepspace.services.jobs.events import JobEventHub, JobLayer
from app.deepspace.services.jobs.pump import PumpHandle, start_pump
from app.deepspace.services.jobs.registry import JobRegistry
from app.deepspace.services.jobs.ring import OutputRing
from app.deepspace.services.jobs.types import (
    JobAppendOptions,
    JobEventFilter,
    JobEventListener,
    JobEvents,
    JobHandle,
    JobOutcome,
    JobOutputRead,
    JobRead,
    JobRegisteredData,
    JobSettleCause,
    JobSettledData,
    JobSpec,
    JobStatus,
    JobToolCall,
    JobView,
)
from app.deepspace.services.jobs.view import JobOutput
from app.deepspace.services.util.scope import ScopedLayers, scope_of
from app.deepspace.utils.brand import SessionId

DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER = 10
DEFAULT_RETAIN_BYTES = 256 * 1024
DEFAULT_SETTLED_RETAIN_BYTES = 16 * 1024
DEFAULT_PUMP_POLL_MS = 150

TASK_WAIT_TIMEOUT = "TASK_WAIT_TIMEOUT"


@dataclass
class ProducerState:
    progress: str | None = None
    job: TrackedJob | None = None


@dataclass
class TrackedJob:
    id: JobId
    tool_call: JobToolCall | None
    kind: JobKind
    label: str
    output_limit_bytes: int | None
    owner: Any | None  # Agent
    cancel: Any  # Callable[[str | None], None]
    status: JobStatus
    ring: OutputRing
    model_cursor: int = 0
    result_delivered: bool = False
    state: ProducerState = field(default_factory=ProducerState)
    detail: str | None = None
    result: str | None = None
    started_at: int = 0
    finished_at: int | None = None
    kill_reason: str | None = None
    settle_cause: JobSettleCause | None = None
    settled: asyncio.Future[None] = field(default_factory=asyncio.Future)
    wait_resolvers: set[Callable[[], None]] = field(default_factory=set)
    pump: PumpHandle | None = None
    spill_paths: list[str | None] = field(default_factory=list)


def is_terminal(status: JobStatus) -> bool:
    return status in ("completed", "killed", "failed")


class LocalJobRegistry(JobRegistry):
    """In-memory process-local job registry implementation."""

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        cfg = config or {}
        self._max_concurrent = cfg.get(
            "max_concurrent_jobs_per_owner", DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER
        )
        self._retain_bytes = cfg.get("retain_bytes", DEFAULT_RETAIN_BYTES)
        self._settled_retain_bytes = cfg.get("settled_retain_bytes", DEFAULT_SETTLED_RETAIN_BYTES)
        self._pump_poll_ms = cfg.get("pump_poll_ms", DEFAULT_PUMP_POLL_MS)

        self._store: dict[JobId, TrackedJob] = {}
        self._layers = ScopedLayers[JobLayer](lambda scope_key: JobLayer(), lambda: None)
        self._hub = JobEventHub(
            self._layers,
            lambda msg: (ctx.logger.warning(msg) if ctx and hasattr(ctx, "logger") else None),
        )
        self._owner_cleanups: dict[Any, Callable[[], Any]] = {}
        self._self_ctx = ctx

        if ctx and hasattr(ctx, "effect"):
            ctx.effect(lambda: self.dispose_all(), "jobs teardown")

    @property
    def events(self) -> JobEvents:
        registrar = self.ctx
        return JobEventsImpl(hub=self._hub, registrar=registrar)

    def start(self, spec: JobSpec) -> JobId:
        owner_id = SessionId(spec.owner) if spec.owner is not None else None
        owner = self._resolve_owner(owner_id)
        if not self._serves_owner(owner):
            raise RuntimeError(
                "background jobs unavailable: no job controller serves this agent "
                "(load deepspace.jobs.tools in its composition)"
            )

        if not spec.kind:
            raise ValueError("invalid job kind: expected non-empty string")
        if not spec.label:
            raise ValueError("invalid job label: expected non-empty string")
        if spec.output_limit_bytes is not None and (
            not isinstance(spec.output_limit_bytes, int) or spec.output_limit_bytes <= 0
        ):
            raise ValueError(f"invalid outputLimitBytes: {spec.output_limit_bytes}")

        if owner is not None:
            self._ensure_owner_cleanup(owner)

        active = self._active_job_count(owner)
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"background job limit reached for this owner (limit: {self._max_concurrent}); "
                f"use job_kill to stop an unneeded job, wait for it to finish, then retry"
            )

        # Generate unique job ID
        job_id: JobId
        while True:
            job_id = JobId(f"{spec.kind}-{uuid.uuid4().hex[:16]}")
            if job_id not in self._store:
                break

        tool_call = spec.tool_call
        ring = OutputRing(retain_bytes=self._retain_bytes)

        state = ProducerState()
        handle = JobHandle(
            job_id=job_id,
            append=lambda text, opts: self._append_ring(state, ring, text, opts, "producer"),
            update_progress=lambda line: self._update_progress(state, line),
        )

        hooks = spec.run(handle)

        settled_future: asyncio.Future[None] = asyncio.Future()
        job = TrackedJob(
            id=job_id,
            tool_call=tool_call,
            kind=spec.kind,
            label=spec.label,
            output_limit_bytes=spec.output_limit_bytes,
            owner=owner,
            cancel=hooks.cancel,
            status="running",
            ring=ring,
            state=state,
            started_at=int(datetime.now(UTC).timestamp() * 1000),
            settled=settled_future,
        )
        state.job = job
        self._store[job_id] = job

        # Record session event
        self._record_session_event(job)

        # Emit registered event
        self._emit({"type": "registered", "job": self._view(job)}, owner)

        # Start pump if pull sources specified
        if spec.output:
            job.pump = start_pump(
                sources=spec.output,
                append=lambda text, opts: self._append_ring(state, ring, text, opts, "pump"),
                spill=lambda idx, path: setattr(
                    job, "spill_paths", job.spill_paths[:idx] + [path] + job.spill_paths[idx + 1 :]
                ),
                poll_ms=self._pump_poll_ms,
                stop_signal=job.settled,
            )

        # Handle producer settlement
        async def handle_settlement() -> None:
            if job.pump:
                await job.pump.done
            try:
                outcome = await hooks.done
                producer_reported = True
            except Exception as e:
                self._self_ctx.logger.warning(f"jobs: job {job.id} producer done rejected: {e}")
                outcome = JobOutcome(status="failed", detail=str(e))
                producer_reported = False
            self._settle(job, outcome, job.settle_cause or "producer", producer_reported)

        asyncio.create_task(handle_settlement())

        return job_id

    def list(self, caller: SessionId | None = None) -> list[JobView]:
        return [
            self._view(job)
            for job in self._store.values()
            if job.owner is None or (caller is not None and job.owner.id == caller)
        ]

    def get(self, id: JobId, caller: SessionId | None = None) -> JobView:
        return self._view(self._expect(id, caller))

    def read(self, id: JobId, caller: SessionId | None = None) -> JobRead:
        return self._read_job(self._expect(id, caller))

    def read_at(
        self, id: JobId, from_offset: int, caller: SessionId | None = None
    ) -> JobOutputRead:
        job = self._expect(id, caller)
        if not isinstance(from_offset, int) or from_offset < 0:
            raise ValueError(f"invalid output read offset: {from_offset}")
        chunks, lossy, next_offset = job.ring.read_from(from_offset)
        return JobOutputRead(chunks=chunks, next=next_offset, lossy=lossy)

    def kill(
        self, id: JobId, caller: SessionId | None = None, reason: str | None = None
    ) -> Literal["requested", "already-finished"]:
        job = self._expect(id, caller)
        return self._kill_job(job, reason)

    async def wait(
        self,
        id: JobId,
        timeout_ms: int,
        caller: SessionId | None = None,
        signal: Any | None = None,
    ) -> JobView:
        job = self._expect(id, caller)
        return await self._wait_job(job, timeout_ms, signal)

    def remove(self, id: JobId, caller: SessionId | None = None) -> None:
        job = self._expect(id, caller)
        if not is_terminal(job.status):
            raise ValueError(
                f"job {id} is still {job.status}; kill it and wait for settlement before removing"
            )
        self._drop([job])

    def attach_controller(self, name: str) -> Callable[[], None]:
        token = object()
        return self._layers.effect(
            self._self_ctx,
            lambda layer: layer.controllers.append(token),
            f"jobs.attachController({name})",
        )

    # --- Internal methods ---

    def _resolve_owner(self, session_id: SessionId | None) -> Any | None:
        if session_id is None:
            return None
        agents = self._self_ctx.get("agents")
        if agents is None:
            raise RuntimeError(
                "background job ownership requires the agent registry (load deepspace.agents)"
            )
        owner = agents.get(session_id)
        if owner is None:
            raise RuntimeError(
                f'session "{session_id}" has no live agent (background job owner must be live)'
            )
        return owner

    def _serves_owner(self, owner: Any | None) -> bool:
        # If no owner, allow (unowned jobs)
        if owner is None:
            return True

        # Check if any layer in the owner's scope chain has controllers
        return any(layer.controllers for layer in self._layers.chain_layers(scope_of(owner.ctx)))

    def _active_job_count(self, owner: Any | None) -> int:
        return sum(
            1
            for job in self._store.values()
            if job.owner == owner and job.status in ("running", "stopping")
        )

    def _ensure_owner_cleanup(self, owner: Any) -> None:
        if owner in self._owner_cleanups:
            return
        detach = owner.ctx.effect(
            lambda: self._dispose_owned(owner), f"jobs.ownerCleanup({owner.id})"
        )
        self._owner_cleanups[owner] = detach

    async def _dispose_owned(self, owner: Any) -> None:
        owned = [j for j in self._store.values() if j.owner == owner]
        self._cancel_for_teardown(owned, "owner disposed")
        await asyncio.gather(*[j.settled for j in owned])
        self._drop(owned)

    def _expect(self, id: JobId, caller: SessionId | None) -> TrackedJob:
        job = self._store.get(id)
        if job is None:
            raise KeyError(f"unknown job {id}")
        if job.owner is not None and (caller is None or job.owner.id != caller):
            raise PermissionError(f"job {id} belongs to another session")
        return job

    def _view(self, job: TrackedJob) -> JobView:
        owner = job.owner.id if job.owner else None
        spill_paths = tuple(p for p in job.spill_paths if p is not None)
        output = (
            JobOutput(
                total=job.ring.total,
                earliest=job.ring.earliest,
                spill_paths=spill_paths,
            )
            if job.ring.total > 0
            else None
        )

        return JobView(
            id=job.id,
            kind=job.kind,
            label=job.label,
            status=job.status,
            owner=owner,
            output_limit_bytes=job.output_limit_bytes,
            progress=job.state.progress,
            detail=job.detail,
            started_at=job.started_at,
            finished_at=job.finished_at,
            output=output,
        )

    def _read_job(self, job: TrackedJob) -> JobRead:
        chunks, lossy = job.ring.read_from_model_cursor()
        result = job.result if is_terminal(job.status) and not job.result_delivered else None
        if result is not None:
            job.result_delivered = True
        if is_terminal(job.status):
            job.ring.trim_to_settled(self._settled_retain_bytes, job.model_cursor)
        return JobRead(
            chunks=chunks,
            lossy=lossy,
            result=result,
            job=self._view(job),
        )

    def _kill_job(
        self, job: TrackedJob, reason: str | None
    ) -> Literal["requested", "already-finished"]:
        if is_terminal(job.status):
            return "already-finished"
        job.cancel(reason)
        job.status = "stopping"
        if reason:
            job.kill_reason = reason
        job.settle_cause = "kill"
        self._emit({"type": "stopping", "job": self._view(job)}, job.owner)
        return "requested"

    async def _wait_job(
        self,
        job: TrackedJob,
        timeout_ms: int,
        signal: Any | None,
    ) -> JobView:
        if timeout_ms <= 0:
            raise ValueError("wait timeout must be positive")
        if not is_terminal(job.status):
            if signal and getattr(signal, "aborted", False):
                raise asyncio.CancelledError("wait aborted")

            # Create abort signal with timeout
            abort_controller = asyncio.Event()

            async def timeout_watcher() -> None:
                await asyncio.sleep(timeout_ms / 1000)
                abort_controller.set()

            async def signal_watcher() -> None:
                if signal and hasattr(signal, "addEventListener"):
                    # Handle AbortSignal
                    signal.addEventListener("abort", lambda: abort_controller.set(), {"once": True})

            async def wait_settled() -> None:
                await job.settled

            timeout_task = asyncio.create_task(timeout_watcher())
            wait_task = asyncio.create_task(abort_controller.wait())
            done_task = asyncio.create_task(wait_settled())

            try:
                await asyncio.wait([wait_task, done_task], return_when=asyncio.FIRST_COMPLETED)
            finally:
                wait_task.cancel()
                done_task.cancel()
                timeout_task.cancel()

        return self._view(job)

    def _append_ring(
        self,
        state: ProducerState,
        ring: OutputRing,
        text: str,
        options: JobAppendOptions | None,
        writer: Literal["producer", "pump"],
    ) -> None:
        job = state.job
        if job and is_terminal(job.status):
            if writer == "producer":
                self._self_ctx.logger.warning(f"jobs: append to settled job {job.id} dropped")
            return
        if ring.append(
            text,
            channel=options.channel if options else None,
            gap_before=options.gap_before if options else False,
            retain_bytes=self._retain_bytes,
        ):
            if job:
                self._emit_output(job)

    def _update_progress(self, state: ProducerState, line: str) -> None:
        job = state.job
        if job and is_terminal(job.status):
            self._self_ctx.logger.warning(f"jobs: progress update on settled job {job.id} dropped")
            return
        state.progress = line
        if job:
            self._emit({"type": "progress", "job": self._view(job)}, job.owner)

    def _emit_output(self, job: TrackedJob) -> None:
        owner = job.owner.id if job.owner else None
        self._hub.emit(
            {"type": "output", "id": job.id, "owner": owner, "total": job.ring.total},
            job.owner,
        )

    def _emit(self, event: dict[str, Any], owner: Any | None) -> None:
        self._hub.emit(event, owner)

    def _record_session_event(
        self,
        job: TrackedJob,
        settlement: dict[str, Any] | None = None,
    ) -> None:
        if job.owner is None:
            return
        session = job.owner.session
        data = JobRegisteredData(
            job_id=job.id,
            owner=job.owner.id,
            kind=job.kind,
            label=job.label,
            started_at=job.started_at,
            tool_call=(
                {
                    "call_id": job.tool_call.call_id,
                    "root_call_id": job.tool_call.root_call_id,
                }
                if job.tool_call
                else None
            ),
        )
        try:
            if settlement is None:
                session.append("job/registered", data)
            else:
                session.append(
                    "job/settled",
                    JobSettledData(
                        job_id=data.job_id,
                        owner=data.owner,
                        kind=data.kind,
                        label=data.label,
                        started_at=data.started_at,
                        tool_call=data.tool_call,
                        finished_at=settlement["finished_at"],
                        status=settlement["status"],
                        cause=settlement["cause"],
                        producer_reported=settlement["producer_reported"],
                        detail=settlement.get("detail"),
                    ),
                )
        except Exception as e:
            self._self_ctx.logger.warning(f"jobs: failed to record lifecycle for {job.id}: {e}")

    def _settle(
        self,
        job: TrackedJob,
        outcome: JobOutcome,
        cause: JobSettleCause,
        producer_reported: bool,
    ) -> None:
        if is_terminal(job.status):
            return

        job.status = outcome.status
        if outcome.status == "killed" and job.kill_reason:
            job.detail = (
                f"{outcome.detail}; {job.kill_reason}" if outcome.detail else job.kill_reason
            )
        elif outcome.detail:
            job.detail = outcome.detail
        job.state.progress = None
        job.result = outcome.result
        job.finished_at = int(datetime.now(UTC).timestamp() * 1000)

        # Record settlement event
        self._record_session_event(
            job,
            {
                "finished_at": job.finished_at,
                "status": outcome.status,
                "cause": cause,
                "producer_reported": producer_reported,
                "detail": job.detail,
            },
        )

        # Trim ring
        job.ring.trim_to_settled(self._settled_retain_bytes, job.ring.model_cursor)

        # Release waiters
        resolvers = list(job.wait_resolvers)
        job.wait_resolvers.clear()
        for resolver in resolvers:
            resolver()

        job.settled.set_result(None)

        # Emit settled event
        self._emit(
            {
                "type": "settled",
                "job": self._view(job),
                "cause": cause,
                "awaited": len(resolvers) > 0,
            },
            job.owner,
        )

        # Emit final output
        self._emit_output(job)

    def _drop(self, jobs: builtins.list[TrackedJob]) -> None:
        for job in jobs:
            self._store.pop(job.id, None)
            self._emit({"type": "removed", "job": self._view(job)}, job.owner)

    def _cancel_for_teardown(self, jobs: builtins.list[TrackedJob], reason: str) -> None:
        for job in jobs:
            if is_terminal(job.status):
                continue
            job.settle_cause = "teardown"
            try:
                job.cancel(reason)
                job.status = "stopping"
                self._emit({"type": "stopping", "job": self._view(job)}, job.owner)
            except Exception as e:
                self._self_ctx.logger.warning(
                    f"jobs: cancel of {job.id} threw during teardown: {e}"
                )
                self._settle(
                    job, JobOutcome(status="failed", detail=f"cancel threw: {e}"), "teardown", False
                )

    async def dispose_all(self) -> None:
        all_jobs = list(self._store.values())
        self._cancel_for_teardown(all_jobs, "jobs service disposed")
        await asyncio.gather(*[j.settled for j in all_jobs])
        self._drop(all_jobs)
        for cleanup in self._owner_cleanups.values():
            try:
                cleanup()
            except Exception:
                pass
        self._owner_cleanups.clear()


class JobEventsImpl(JobEvents):
    def __init__(self, hub: JobEventHub, registrar: Any) -> None:
        self._hub = hub
        self._registrar = registrar

    def subscribe(self, filter: JobEventFilter, listener: JobEventListener) -> Callable[[], None]:
        return self._hub.subscribe(self._registrar, filter, listener)


__all__ = ["LocalJobRegistry"]
