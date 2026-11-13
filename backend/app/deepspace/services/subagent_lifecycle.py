"""Subagent lifecycle and disposal for DeepSpace.

Python-native port of the AverQel KineSis subagent lifecycle system.
Provides ActivationObserver, LifecycleEmitter, settlement state machine,
poke/wake mechanism, and disposal cascade.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SettlementState(str, Enum):
    """Settlement observation states."""

    READY = "ready"
    WAIT = "wait"
    RETRY = "retry"
    CLOSED = "closed"


class SubagentStopReason(str, Enum):
    """Stop reasons for subagent runs."""

    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    MAX_ROUNDS = "max_rounds"


@dataclass
class SubagentResult:
    """Result of a subagent run."""

    output: list[Any] = field(default_factory=list)
    stop_reason: str = "completed"
    error: Exception | None = None


@dataclass
class SubagentRunInfo:
    """Observe-only identifying detail for a published subagent run."""

    run_id: str
    provider: str
    id: str
    local: bool


@dataclass
class SubagentRunEndInfo:
    """Observe-only outcome detail for a settled subagent run."""

    run_id: str
    stop_reason: str
    error: str | None = None


class ActivationObserver:
    """Observer for one activation's residency epoch."""

    def __init__(
        self,
        emit: Callable[..., None],
        provider: str,
        child_id: str,
        parent: Any,
        run_id: str | None = None,
    ) -> None:
        self._emit = emit
        self._provider = provider
        self._child_id = child_id
        self._parent = parent
        self._run_id = run_id or str(uuid.uuid4())
        self._started = False
        self._settled = False

    @property
    def run_id(self) -> str:
        return self._run_id

    def start(self, child: Any = None) -> None:
        """Publish the start edge once the epoch is resident."""
        if self._started:
            return
        self._started = True
        info = SubagentRunInfo(
            run_id=self._run_id,
            provider=self._provider,
            id=self._child_id,
            local=child is not None,
        )
        try:
            self._emit("subagent/start", info, self._parent)
        except Exception as exc:
            logger.warning("subagent start listener threw: %s", exc)

    def settle(self, result: SubagentResult) -> None:
        """Publish the terminal edge exactly once."""
        if self._settled:
            return
        self._settled = True
        info = SubagentRunEndInfo(
            run_id=self._run_id,
            stop_reason=result.stop_reason,
            error=str(result.error) if result.error else None,
        )
        try:
            self._emit("subagent/end", info, self._parent)
        except Exception as exc:
            logger.warning("subagent end listener threw: %s", exc)


class LifecycleEmitter:
    """Contained lifecycle emitter for subagent edges."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._listeners: list[Callable[[str, Any, Any], None]] = []

    def register(self, listener: Callable[[str, Any, Any], None]) -> None:
        self._listeners.append(listener)

    def __call__(
        self,
        name: str,
        info: Any,
        parent: Any = None,
    ) -> None:
        for callback in self._listeners:
            try:
                callback(name, info, parent)
            except Exception as exc:
                logger.warning("subagent %s listener threw: %s", name, exc)


def create_lifecycle_emitter(ctx: Any) -> LifecycleEmitter:
    """Create a contained lifecycle emitter."""
    return LifecycleEmitter(ctx)


def create_activation_observer(
    emit: LifecycleEmitter,
    provider: str,
    child_id: str,
    parent: Any,
    run_id: str | None = None,
) -> ActivationObserver:
    """Build the observer for one activation's residency epoch."""
    return ActivationObserver(emit, provider, child_id, parent, run_id=run_id)


@dataclass
class Activation:
    """One managed child execution and its result delivery policy."""

    pool: Any = None
    release_slot: Callable[[], None] = field(default_factory=lambda: lambda: None)
    child_id: str = ""
    parent: Any = None
    delivery: str = "parent"
    result: asyncio.Future[SubagentResult] | None = None
    closing: asyncio.Future[None] | None = None
    ancestry: set[int] = field(default_factory=set)
    owned_children: set[str] = field(default_factory=set)
    observer: ActivationObserver | None = None
    announced: bool = False
    kind: str = "local"
    handle: Any = None
    run: Any = None
    # Replaced on each wake so waiters can distinguish a real state change
    # from a stale observation and retry their settlement check.
    poke: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        if self.result is None:
            loop = asyncio.get_event_loop()
            self.result = loop.create_future()

    @property
    def is_closing(self) -> bool:
        return self.closing is not None


@dataclass
class SubagentActivation:
    """A managed execution; disposal addresses this exact activation."""

    child_id: str
    result: Any = None
    dispose: Callable[[], Any] = field(default_factory=lambda: lambda: None)

    def __post_init__(self) -> None:
        if self.result is None:
            loop = asyncio.get_event_loop()
            self.result = loop.create_future()


class ActivationPool:
    """Pool for limiting concurrent subagent activations."""

    def __init__(self, max_active: int = 8) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least one")
        self._max_active = max_active
        self._active = 0
        self._waiters: list[asyncio.Future[None]] = []

    def reserve(self, max_active: int | None = None) -> Callable[[], None]:
        """Reserve a slot in the pool."""
        limit = self._max_active if max_active is None else min(self._max_active, max_active)
        if limit < 1:
            raise ValueError("max_active must be at least one")
        if self._active >= limit:
            raise RuntimeError(f"subagent activation limit reached ({limit})")
        self._active += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._active = max(0, self._active - 1)
            if self._waiters:
                fut = self._waiters.pop(0)
                if not fut.done():
                    fut.set_result(None)

        return release


class SubagentManager:
    """Full subagent manager with activation lifecycle.

    Python-native port of the AverQel KineSis SubagentManager.
    """

    def __init__(
        self,
        ctx: Any,
        max_active_subagents: int = 8,
        max_depth: int = 3,
    ) -> None:
        self._ctx = ctx
        self._max_active_subagents = max_active_subagents
        self._max_depth = max_depth
        self._resident: dict[str, Activation] = {}
        self._draining = False
        self._closing_scopes: dict[int, set[int]] = {}
        self._pool = ActivationPool(max_active_subagents)
        self._emit = create_lifecycle_emitter(ctx)

    @property
    def ctx(self) -> Any:
        return self._ctx

    @property
    def draining(self) -> bool:
        return self._draining

    def max_active_subagents(self) -> int:
        return self._max_active_subagents

    def max_depth(self) -> int:
        return self._max_depth

    def assert_admitting(self, agent: Any) -> None:
        """Reject new admission once draining has begun."""
        if self._draining:
            raise RuntimeError("subagents are draining; the operation was not admitted")
        # Check closing scopes
        agent_key = id(agent)
        for members in self._closing_scopes.values():
            if agent_key in members:
                raise RuntimeError(
                    "subagents below parent are draining; the operation was not admitted"
                )

    def live_lineage(self, agent: Any) -> list[Any]:
        """Return the resolvable ancestry from agent upward."""
        lineage = [agent]
        seen = {id(agent)}
        parent_session = getattr(getattr(agent, "session", None), "header", None)
        if parent_session is not None:
            parent_session = getattr(parent_session, "parentSession", None)
        while parent_session is not None:
            parent = self._ctx.agents.get(parent_session) if hasattr(self._ctx, "agents") else None
            if parent is None or id(parent) in seen:
                break
            lineage.append(parent)
            seen.add(id(parent))
            parent_session = getattr(getattr(parent, "session", None), "header", None)
            if parent_session is not None:
                parent_session = getattr(parent_session, "parentSession", None)
        return lineage

    def hold_ownership(self, parent: Any, child_id: str) -> Callable[[], None]:
        """Pre-register childId in parent's owned set to prevent settlement."""
        parent_activation = self._resident.get(getattr(parent, "id", str(parent)))
        if parent_activation is None:
            return lambda: None
        if parent_activation.closing is not None:
            raise RuntimeError("subagent parent is being disposed; the child was not established")
        if child_id in parent_activation.owned_children:
            return lambda: None
        parent_activation.owned_children.add(child_id)
        self._wake(parent_activation)

        def release() -> None:
            live = self._resident.get(child_id)
            if live is not None and live.closing is None:
                return
            if child_id in parent_activation.owned_children:
                parent_activation.owned_children.discard(child_id)
                self._wake(parent_activation)

        return release

    def release_ownership(self, child_id: str) -> None:
        """Remove one child from its live owner's set."""
        for candidate in self._resident.values():
            if child_id in candidate.owned_children:
                candidate.owned_children.discard(child_id)
                self._wake(candidate)

    def notify_inbox_changed(self, child_id: str) -> None:
        """Wake a resident child's settlement watcher after a queued delivery."""
        activation = self._resident.get(child_id)
        if activation is not None:
            self._wake(activation)

    def _wake(self, activation: Activation) -> None:
        """Let a settlement watcher re-check residency."""
        previous = activation.poke
        activation.poke = asyncio.Event()
        previous.set()

    def acquire_ownership(self, parent: Any, child_id: str) -> None:
        """Register the child in parent's owned set."""
        parent_activation = self._resident.get(getattr(parent, "id", str(parent)))
        if parent_activation is None:
            return
        if parent_activation.closing is not None:
            raise RuntimeError("subagent parent is being disposed; the child was not established")
        if child_id not in parent_activation.owned_children:
            parent_activation.owned_children.add(child_id)
            self._wake(parent_activation)

    def settlement_state(
        self,
        activation: Activation,
        observation: Any = None,
    ) -> SettlementState:
        """Check pending input and owned children before closing admission."""
        if activation.closing is not None:
            return SettlementState.CLOSED
        if observation is not None and activation.poke is not observation:
            return SettlementState.RETRY
        if activation.owned_children:
            return SettlementState.WAIT
        # Check inbox
        inbox = getattr(getattr(activation, "handle", None), "agent", None)
        if inbox is not None:
            inbox = getattr(inbox, "inbox", None)
            if inbox is not None:
                if getattr(inbox, "nextTurn", []) or getattr(inbox, "nextStep", []):
                    return SettlementState.WAIT
        return SettlementState.READY

    async def wait_for_settlement(self, activation: Activation) -> SettlementState:
        """Wait for ownership or inbox changes, retrying stale observations."""
        while True:
            observation = activation.poke
            state = self.settlement_state(activation, observation)
            if state in {SettlementState.READY, SettlementState.CLOSED}:
                return state
            if state == SettlementState.RETRY:
                continue
            await observation.wait()

    def assert_child_id_available(self, child_id: str, expected_agent: Any = None) -> None:
        """Reject a child identity already owned by a live Agent or Session."""
        if child_id in self._resident:
            raise RuntimeError(f'subagent "{child_id}" already exists')
        # Check agents registry
        agents = getattr(self._ctx, "agents", None)
        if agents is not None and hasattr(agents, "get"):
            existing = agents.get(child_id)
            if existing is not None and existing is not expected_agent:
                raise RuntimeError(f'subagent "{child_id}" already exists')

    def assert_live_parent(self, parent: Any, child_id: str) -> None:
        """Reject parent authority after the live registry entry changes."""
        agents = getattr(self._ctx, "agents", None)
        if agents is not None and hasattr(agents, "get"):
            if agents.get(getattr(parent, "id", str(parent))) is not parent:
                raise RuntimeError(
                    f'subagent "{child_id}" delivery requires the exact live parent agent'
                )

    async def dispose(self, activation: Activation) -> None:
        """Stop and release one Activation through its memoized close transaction."""
        if activation.closing is not None:
            await activation.closing
            return
        loop = asyncio.get_event_loop()
        completion = loop.create_future()
        activation.closing = completion
        root_agent = (
            getattr(activation.handle, "agent", None) if activation.handle is not None else None
        )
        scope_key = id(root_agent) if root_agent is not None else id(activation)
        scoped_members = {scope_key} | {
            identity
            for live in self._resident.values()
            if live is not activation and scope_key in live.ancestry
            for identity in (id(getattr(live.handle, "agent", None) or live),)
        }
        self._closing_scopes[scope_key] = scoped_members
        try:
            await self.finish_disposal(activation, True)
            completion.set_result(None)
        except Exception as exc:
            completion.set_exception(exc)
            raise
        finally:
            self._closing_scopes.pop(scope_key, None)

    async def finish_disposal(self, activation: Activation, stop: bool) -> None:
        """Full teardown of an activation."""
        child_id = activation.child_id
        result = SubagentResult(stop_reason="completed")

        try:
            if stop and activation.owned_children:
                descendants = [
                    self._resident[owned_id]
                    for owned_id in tuple(activation.owned_children)
                    if owned_id in self._resident
                ]
                child_results = await asyncio.gather(
                    *(self.dispose(descendant) for descendant in descendants),
                    return_exceptions=True,
                )
                child_errors = [item for item in child_results if isinstance(item, Exception)]
                if child_errors:
                    result = SubagentResult(
                        stop_reason="error",
                        error=RuntimeError(
                            f"{len(child_errors)} owned child activation(s) failed to dispose"
                        ),
                    )
            if activation.kind == "external" and activation.run is not None:
                if stop and hasattr(activation.run, "dispose"):
                    disposed = activation.run.dispose()
                    if inspect.isawaitable(disposed):
                        await disposed
                if hasattr(activation.run, "result"):
                    external_result = activation.run.result
                    if inspect.isawaitable(external_result):
                        external_result = await external_result
                    if isinstance(external_result, SubagentResult):
                        result = external_result
                    else:
                        result = SubagentResult(
                            output=(
                                [getattr(external_result, "answer", "")]
                                if getattr(external_result, "answer", "")
                                else []
                            ),
                            stop_reason=str(getattr(external_result, "stop_reason", "completed")),
                            error=(
                                RuntimeError(str(external_result.error))
                                if getattr(external_result, "error", None)
                                else None
                            ),
                        )
            elif activation.kind == "local" and activation.handle is not None:
                agent = getattr(activation.handle, "agent", None)
                if agent is not None and stop:
                    if hasattr(agent, "cancel"):
                        agent.cancel({"kind": "parent"})
                    # Wait for idle
                    if hasattr(agent, "whenIdle"):
                        await agent.whenIdle()
                # Flush final state
                try:
                    if hasattr(self._ctx, "sessions") and hasattr(self._ctx.sessions, "flush"):
                        session = getattr(agent, "session", None)
                        if session is not None:
                            await self._ctx.sessions.flush(session)
                except Exception as exc:
                    logger.warning("final session flush failed: %s", exc)

                if hasattr(activation.handle, "dispose"):
                    disposed = activation.handle.dispose()
                    if inspect.isawaitable(disposed):
                        await disposed

                local_result = self._capture_local_result(activation)
                if stop and local_result.stop_reason == "completed":
                    local_result.stop_reason = "cancelled"
                if result.error is not None and local_result.error is None:
                    local_result.error = result.error
                    local_result.stop_reason = "error"
                result = local_result
        except Exception as exc:
            logger.warning("disposal failed: %s", exc)
            result = SubagentResult(stop_reason="error", error=exc)

        # Cleanup
        self._resident.pop(child_id, None)
        if activation.release_slot is not None:
            activation.release_slot()
        self.release_ownership(child_id)

        # Settle
        if activation.observer is not None:
            activation.observer.settle(result)

        self.notify_settlement(activation, result)

        # Resolve result future
        if activation.result is not None and not activation.result.done():
            activation.result.set_result(result)

    def _capture_local_result(self, activation: Activation) -> SubagentResult:
        """Capture the result from a local activation."""
        agent = getattr(getattr(activation, "handle", None), "agent", None)
        if agent is None:
            return SubagentResult(stop_reason="completed")
        session = getattr(agent, "session", None)
        output = []
        if session is not None:
            # Get last assistant message
            messages = getattr(session, "messages", [])
            if messages:
                last = messages[-1]
                if isinstance(last, dict):
                    output = [last.get("content", "")]
                elif getattr(last, "content", None) is not None:
                    output = [last.content]
        return SubagentResult(output=output, stop_reason="completed")

    def notify_settlement(self, activation: Activation, terminal: SubagentResult) -> None:
        """Deliver the captured execution result to the durable direct parent."""
        if not getattr(activation, "announced", False):
            return
        delivery = getattr(activation, "delivery", "parent")
        if delivery == "caller":
            return
        parent = activation.parent
        agents = getattr(self._ctx, "agents", None)
        if agents is not None and hasattr(agents, "get"):
            if agents.get(getattr(parent, "id", None)) is not parent:
                return
        try:
            message = self._create_settlement_message(activation.child_id, terminal)
            # Deliver to parent
            if hasattr(parent, "inject"):
                parent.inject(message)
            elif hasattr(parent, "followup"):
                parent.followup(message)
        except Exception as exc:
            logger.warning("settlement notice not delivered: %s", exc)

    def _create_settlement_message(self, child_id: str, terminal: SubagentResult) -> Any:
        """Create a settlement message for the parent."""
        return {
            "type": "subagent/settlement",
            "child_id": child_id,
            "stop_reason": terminal.stop_reason,
            "output": terminal.output,
        }

    async def drain_descendants(self, parents: list[Any]) -> None:
        """Stop the managed descendants of exact live host-owned parents."""
        roots: list[Any] = []
        agents = getattr(self._ctx, "agents", None)
        for parent in parents:
            if agents is not None and hasattr(agents, "get"):
                if agents.get(getattr(parent, "id", None)) is parent:
                    roots.append(parent)
        if not roots:
            return

        targets = []
        for activation in list(self._resident.values()):
            ancestry: set[int] = getattr(activation, "ancestry", set())
            owners = [root for root in roots if id(root) in ancestry]
            if owners:
                targets.append(activation)

        await asyncio.gather(*(self.dispose(activation) for activation in targets))

    async def drain_children(self, parent: Any, child_ids: list[str]) -> None:
        """Release selected resident direct children of one exact live parent."""
        agents = getattr(self._ctx, "agents", None)
        if agents is not None and hasattr(agents, "get"):
            if agents.get(getattr(parent, "id", None)) is not parent:
                raise RuntimeError("selected child teardown requires the exact live parent agent")

        targets = []
        for child_id in set(child_ids):
            activation = self._resident.get(child_id)
            if activation is None:
                continue
            if getattr(activation, "parent", None) is not parent:
                raise RuntimeError(f'subagent "{child_id}" is not a direct child')
            targets.append(activation)

        await asyncio.gather(*(self.dispose(activation) for activation in targets))

    async def rollback_unpublished(self, activation: Activation, error: Exception) -> None:
        """Release an Activation whose start edge was not published."""
        loop = asyncio.get_event_loop()
        completion = loop.create_future()
        activation.closing = completion
        try:
            if activation.kind == "local" and activation.handle is not None:
                if hasattr(activation.handle, "dispose"):
                    disposed = activation.handle.dispose()
                    if inspect.isawaitable(disposed):
                        await disposed
            elif activation.kind == "external" and activation.run is not None:
                if hasattr(activation.run, "dispose"):
                    disposed = activation.run.dispose()
                    if inspect.isawaitable(disposed):
                        await disposed
        finally:
            self._resident.pop(activation.child_id, None)
            if activation.release_slot is not None:
                activation.release_slot()
            self.release_ownership(activation.child_id)
            if activation.result is not None and not activation.result.done():
                activation.result.set_exception(error)
            completion.set_result(None)

    async def cold_resume(
        self,
        parent: Any,
        child_id: str,
        content: list[Any],
        options: dict[str, Any] | None = None,
    ) -> Any:
        """Cold-resume a persisted child and submit the waiting turn."""
        resume = getattr(self._ctx, "resume_subagent", None)
        if not callable(resume):
            raise RuntimeError(
                "This lifecycle manager has no resume adapter; durable DeepSpace children "
                "must be continued through the subagent worker."
            )
        try:
            value = resume(parent, child_id, content, options or {})
            return await value if hasattr(value, "__await__") else value
        except Exception as exc:
            raise RuntimeError(f'subagent "{child_id}" is unavailable') from exc

    def start_local(self, spec: dict[str, Any]) -> Any:
        """Start one continuable background child."""
        provider = spec.get("provider", "in-process")
        parent = spec.get("request", {}).get("parent")
        child_id = spec.get("childId") or str(uuid.uuid4())
        handle = spec.get("handle")
        if handle is None:
            raise ValueError("start_local requires a materialized local session handle")
        if parent is not None:
            self.assert_admitting(parent)
        self.assert_child_id_available(child_id, expected_agent=getattr(handle, "agent", None))
        release_slot = self._pool.reserve()

        activation = Activation(
            release_slot=release_slot,
            child_id=child_id,
            parent=parent,
            delivery=spec.get("delivery", "parent"),
            kind="local",
            announced=False,
            ancestry=(
                {id(agent) for agent in self.live_lineage(parent)} if parent is not None else set()
            ),
            handle=handle,
        )
        observer = create_activation_observer(self._emit, provider, child_id, parent)
        activation.observer = observer
        self._resident[child_id] = activation
        try:
            self.acquire_ownership(parent, child_id)
        except Exception:
            self._resident.pop(child_id, None)
            activation.release_slot()
            raise
        observer.start(handle)
        activation.announced = True

        return {
            "childId": child_id,
            "result": activation.result,
            "dispose": lambda: self.dispose(activation),
        }

    def start_external(self, spec: dict[str, Any]) -> Any:
        """Start an external backend under activation ownership."""
        provider = spec.get("provider", "acp")
        parent = spec.get("request", {}).get("parent")
        child_id = str(spec.get("childId") or uuid.uuid4())
        run = spec.get("run")
        if run is None:
            raise ValueError("start_external requires an active external run handle")
        if parent is not None:
            self.assert_admitting(parent)
        self.assert_child_id_available(child_id)
        release_slot = self._pool.reserve()

        activation = Activation(
            release_slot=release_slot,
            child_id=child_id,
            parent=parent,
            delivery=spec.get("delivery", "parent"),
            kind="external",
            announced=False,
            ancestry=(
                {id(agent) for agent in self.live_lineage(parent)} if parent is not None else set()
            ),
            run=run,
        )
        observer = create_activation_observer(self._emit, provider, child_id, parent)
        activation.observer = observer
        self._resident[child_id] = activation
        try:
            self.acquire_ownership(parent, child_id)
        except Exception:
            self._resident.pop(child_id, None)
            activation.release_slot()
            raise
        observer.start()
        activation.announced = True

        return {
            "childId": child_id,
            "result": activation.result,
            "dispose": lambda: self.dispose(activation),
        }

    def send_message(
        self,
        sender: Any,
        target_id: str,
        content: list[Any],
        options: dict[str, Any] | None = None,
    ) -> str:
        """Deliver one model-authored message to a direct continuable child."""
        agents = getattr(self._ctx, "agents", None)
        if agents is not None and hasattr(agents, "get"):
            if agents.get(getattr(sender, "id", None)) is not sender:
                raise RuntimeError("message delivery requires the exact live sender agent")
        self.assert_admitting(sender)

        # Check if sender is a resident child sending to parent
        sender_activation = self._resident.get(getattr(sender, "id", str(sender)))
        if (
            sender_activation is not None
            and sender_activation.kind == "local"
            and getattr(getattr(sender_activation, "handle", None), "agent", None) is sender
            and getattr(sender_activation, "parent", None) is not None
            and getattr(sender_activation.parent, "id", None) == target_id
        ):
            # Send to parent
            parent = self._ctx.agents.get(target_id) if hasattr(self._ctx, "agents") else None
            if parent is not None:
                msg = {
                    "type": "agent_message",
                    "sender": getattr(sender, "id", str(sender)),
                    "content": content,
                }
                if hasattr(parent, "steer"):
                    parent.steer(msg)
                elif hasattr(parent, "followup"):
                    parent.followup(msg)
                return str(uuid.uuid4())
            raise RuntimeError("direct parent is not live")

        raise RuntimeError("agent is not a resident continuable child")

    def queue_prompt(
        self,
        parent: Any,
        child_id: str,
        content: list[Any],
        source: Any,
        signal: Any = None,
    ) -> str:
        """Queue one human-authored prompt as a distinct direct-child turn."""
        return self._deliver_to_child(
            parent,
            child_id,
            content,
            {
                "source": source,
                "signal": signal,
                "delivery": "queue",
            },
        )

    def steer_prompt(
        self,
        parent: Any,
        child_id: str,
        content: list[Any],
        source: Any,
        signal: Any = None,
    ) -> str:
        """Steer one host-authored prompt to a direct continuable child."""
        return self._deliver_to_child(
            parent,
            child_id,
            content,
            {
                "source": source,
                "signal": signal,
                "delivery": "steer",
            },
        )

    def _deliver_to_child(
        self,
        parent: Any,
        child_id: str,
        content: list[Any],
        options: dict[str, Any],
    ) -> str:
        """Route one parent-originated delivery through residency and cold resume."""
        self.assert_admitting(parent)
        activation = self._resident.get(child_id)
        if activation is None:
            raise RuntimeError(f'subagent "{child_id}" is unavailable')
        # Deliver
        msg_id = str(uuid.uuid4())
        message = {"id": msg_id, "content": content}
        if options.get("delivery") == "steer":
            if hasattr(activation, "handle") and hasattr(activation.handle, "agent"):
                activation.handle.agent.steer(message)
        else:
            if hasattr(activation, "handle") and hasattr(activation.handle, "agent"):
                activation.handle.agent.followup(message)
        return msg_id

    def get_resident(self, child_id: str) -> Activation | None:
        """Get a resident activation by child ID."""
        return self._resident.get(child_id)

    def list_resident(self) -> list[str]:
        """List all resident child IDs."""
        return list(self._resident.keys())

    def is_draining(self) -> bool:
        return self._draining

    def start_draining(self) -> None:
        """Begin draining all subagents."""
        self._draining = True

    async def drain_all(self) -> None:
        """Reject admissions and await disposal of every resident activation."""
        self.start_draining()
        await asyncio.gather(
            *(self.dispose(activation) for activation in tuple(self._resident.values())),
            return_exceptions=True,
        )

    def stop_draining(self) -> None:
        """Stop draining."""
        self._draining = False


__all__ = [
    "SettlementState",
    "SubagentStopReason",
    "SubagentResult",
    "SubagentRunInfo",
    "SubagentRunEndInfo",
    "ActivationObserver",
    "LifecycleEmitter",
    "create_lifecycle_emitter",
    "create_activation_observer",
    "Activation",
    "SubagentActivation",
    "ActivationPool",
    "SubagentManager",
]
