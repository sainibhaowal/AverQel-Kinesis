"""Abstract background job registry service definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from app.deepspace.services.jobs.types import (
    JobEvents,
    JobId,
    JobOutputRead,
    JobRead,
    JobSpec,
    JobView,
)
from app.deepspace.utils.brand import SessionId


class JobRegistry(ABC):
    """
    Abstract background job registry. Subclass, implement the abstract members,
    and load the subclass as a plugin — it registers as `ctx.jobs` (one
    implementation per context; loading a second throws, which is cordis'
    standard duplicate-service behavior).

    Implementations must honor these semantics:
    - Registrations outlive producer and controller fibers. Owner and
      service disposal cancel live work and await compliant producers; a
      throwing teardown cancel force-fails only the record. Such settlements
      announce `cause: 'teardown'`, because a job whose owner is being destroyed
      has no reader left.
    - Owned-job access is fenced by the owner's session id. Ids are
      predictable, so authorization — not secrecy — is the boundary.
    - Settlement is first-wins: one terminal record, released waiters, then one
      round of contained event delivery, even against a late producer outcome.
      The `settled` event follows every released waiter and reports whether it
      released one (`awaited`), so a completion reporter can skip settlements a
      waiting caller already collected.
    - A settled record stays listed until its owner's disposal, service
      disposal, or an explicit `remove` by a caller that collected the
      terminal state itself and never handed the id out.
    - `start` refuses work while no attached job controller serves the
      spec's owner, so a producer cannot start work that owner cannot collect
      or stop. One registry serves every composition in the process, so this
      question — and event delivery under `{ owners: 'scope' }` — is
      owner-relative rather than process-wide: registrations made from an
      unscoped context serve every owner, and registrations made under an agent
      composition's scope serve exactly the agents composed under it.
    - Every job owns one output ring. Pull sources named by the spec are pumped
      by the registry and drained once more before settlement; pushed appends
      land whole. The model's consuming cursor and observers' absolute offsets
      read the same bytes and never disturb each other.
    - Ring retention is bounded. Appends past the live cap drop the oldest
      retained bytes; a reader below the retained window gets a lossy read,
      never an error. Settlement trims retention to the settled cap and ends
      the stream; the ring has no separate lifecycle.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        if type(self) is JobRegistry:
            raise TypeError(
                "JobRegistry is abstract; load an implementation such as LocalJobRegistry instead"
            )

    @property
    @abstractmethod
    def events(self) -> JobEvents:
        """Lifecycle and output events, filtered per subscription."""

    @abstractmethod
    def start(self, spec: JobSpec) -> JobId:
        """
        Preflight access, validation, owner cleanup, and implementation-owned
        admission before starting and atomically registering work. Any preflight
        rejection leaves no job id or execution resource. A throwing starter
        leaves nothing registered; after it returns, registration cannot fail.

        Args:
            spec: Job identity, owner, output sources, and synchronous starter.

        Returns:
            The registry-issued `<kind>-<short-uuid>` id.
        """

    @abstractmethod
    def list(self, caller: SessionId | None = None) -> list[JobView]:
        """
        List caller-owned and unowned jobs in registration order.

        Args:
            caller: Reading session; omission sees only unowned jobs.

        Returns:
            Fresh projections.
        """

    @abstractmethod
    def get(self, id: JobId, caller: SessionId | None = None) -> JobView:
        """
        Project one job without changing its cursor. Throws for an unknown or
        foreign job.

        Args:
            id: Job to look up.
            caller: Reading session checked against the owner.

        Returns:
            A fresh projection.
        """

    @abstractmethod
    def read(self, id: JobId, caller: SessionId | None = None) -> JobRead:
        """
        Consume the ring from the model cursor and advance it to the current
        total. After settlement the first read also carries the producer's
        result. Throws for an unknown or foreign job.

        Args:
            id: Job to read.
            caller: Reading session checked against the owner.

        Returns:
            The chunks since the cursor, the lossy flag, the result once, and the post-read projection.
        """

    @abstractmethod
    def read_at(
        self, id: JobId, from_offset: int, caller: SessionId | None = None
    ) -> JobOutputRead:
        """
        Read retained ring output without moving the model cursor. Resume with
        a previous read's `next`; an offset inside a retained chunk returns the
        whole chunk (its `at` may precede `from`). Throws for a negative or
        non-integer offset, or an unknown or foreign job.

        Args:
            id: Job to read.
            from_offset: Absolute byte offset to read from (0 for the retained head).
            caller: Reading session checked against the owner.

        Returns:
            Retained chunks overlapping `[from, total)`, the resume offset, and the lossy flag.
        """

    @abstractmethod
    def kill(
        self, id: JobId, caller: SessionId | None = None, reason: str | None = None
    ) -> Literal["requested", "already-finished"]:
        """
        Request cancellation, then mark the job stopping. A producer throw
        propagates without changing job state. A supplied reason is merged into
        terminal `detail` when the job settles `killed`. Throws for an unknown
        or foreign job.

        Args:
            id: Job to cancel.
            caller: Killing session checked against the owner.
            reason: Cancellation reason forwarded verbatim to the producer.

        Returns:
            `requested` for live work, otherwise `already-finished`.
        """

    @abstractmethod
    async def wait(
        self,
        id: JobId,
        timeout_ms: int,
        caller: SessionId | None = None,
        signal: Any | None = None,
    ) -> JobView:
        """
        Wait for settlement or timeout without cancelling the job. Caller abort
        rejects only while the job is live; after settlement the terminal
        projection wins. Rejects for an invalid timeout or an unknown or foreign
        job.

        Args:
            id: Job to wait for.
            timeout_ms: Positive finite wait bound in milliseconds.
            caller: Waiting session checked against the owner.
            signal: Optional cancellation of the wait itself.

        Returns:
            Projection at settlement or timeout.
        """

    @abstractmethod
    def remove(self, id: JobId, caller: SessionId | None = None) -> None:
        """
        Drop one settled job's record from the visible set and announce
        `removed`. For a caller that collected the terminal state through its own
        `wait` and never handed the id to the model, such as a shell tool's
        foreground call. Throws for a job that is still live, unknown, or foreign.

        Args:
            id: Settled job to drop.
            caller: Removing session checked against the owner.
        """

    @abstractmethod
    def attach_controller(self, name: str) -> Callable[[], None]:
        """
        Attach an effect-scoped controller that can read and stop jobs. It serves the
        owners its registering context's scope covers, and `start` refuses an
        owner no attached controller serves.

        Args:
            name: Diagnostic label; duplicate names remain independent.

        Returns:
            Disposer that detaches this controller.
        """


__all__ = ["JobRegistry"]
