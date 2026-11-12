"""The process-wide Typert service and its change stream.

The service owns one :class:`~app.deepspace.typert.registry.TypertRegistry`,
one :class:`~app.deepspace.typert.loader.TypertLoader`, and a change hub the
authenticated API streams to a client.

Startup is additive: the service registers the built-in resource kinds, the
native tool surface, and the Todo wire contract.  A failure to reflect one
optional package is logged and does not stop the application, but a failure
to construct the service itself is a deployment problem and is reported.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any

from app.deepspace.typert.bridge import contribution_from_native_specs
from app.deepspace.typert.codecs import JsonCodec
from app.deepspace.typert.descriptors import (
    RemoteContribution,
    RemoteDescriptor,
    RemoteParameter,
)
from app.deepspace.typert.loader import TypertLoader
from app.deepspace.typert.reflection import (
    TypertDocumentation,
    TypertObjectModel,
    TypertPackageModel,
    TypertServiceModel,
)
from app.deepspace.typert.registry import (
    Disposer,
    LookupProvider,
    RegistryChange,
    TypertRegistry,
)
from app.deepspace.typert.resource_authorization import built_in_resource_authorizers
from app.deepspace.typert.resources import GLOBAL, ResourceKindRegistry

logger = logging.getLogger(__name__)

#: The package name of the Todo wire contract.
TODO_PACKAGE = "deepspace.todo"

#: The native Todo endpoint.
TODO_ENDPOINT = "todo/write"

#: The resource kinds every DeepSpace deployment declares.
_BUILTIN_RESOURCE_KINDS: tuple[tuple[str, str, str], ...] = (
    ("workspace", "A conversation workspace directory.", "workspace_id"),
    ("library", "One Library file.", "file_id"),
    ("conversation", "One DeepSpace conversation.", "conversation_id"),
    ("document", "One ingested document.", "document_id"),
    ("terminal", "One interactive terminal session.", "terminal_id"),
    ("ssh-host", "One configured SSH host.", "host_id"),
    ("plugin", "One installed plugin.", "plugin_id"),
    ("artifact", "One persisted media artifact.", "artifact_id"),
    ("job", "One background job.", "job_id"),
    ("schedule", "One scheduled task.", "schedule_id"),
)


def _attribute_identity(primary: str, fallback: str = "id") -> Any:
    def extract(host: Any) -> Any:
        value = getattr(host, primary, None)
        if value is None:
            value = getattr(host, fallback, None)
        if value is None:
            raise ValueError(f"The host object has no {primary!r} or {fallback!r} identity.")
        return value

    return extract


class TypertSubscription:
    """One client's queue of registry changes."""

    __slots__ = ("_closed", "_queue")

    def __init__(self, maxsize: int = 256) -> None:
        self._queue: queue.Queue[RegistryChange | None] = queue.Queue(maxsize=maxsize)

    @property
    def closed(self) -> bool:
        """Whether the subscription was closed."""
        return self._closed

    def publish(self, change: RegistryChange) -> None:
        """Enqueue one change, dropping the oldest when the queue is full."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(change)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - only a concurrent reader
                pass
            try:
                self._queue.put_nowait(change)
            except queue.Full:  # pragma: no cover - only a concurrent producer
                pass

    def get(self, timeout: float | None = None) -> RegistryChange | None:
        """Return the next change, waiting up to ``timeout`` seconds."""
        try:
            value = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if value is None:
            self._closed = True
        return value

    def close(self) -> None:
        """End the subscription and release a blocked reader."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except (queue.Empty, queue.Full):  # pragma: no cover - concurrent close
                pass


class TypertChangeHub:
    """The process hub that fans registry changes out to subscribers."""

    def __init__(self) -> None:
        self._subscriptions: dict[int, TypertSubscription] = {}
        self._lock = threading.RLock()

    def subscribe(self) -> TypertSubscription:
        """Create one subscription."""
        subscription = TypertSubscription()
        with self._lock:
            self._subscriptions[id(subscription)] = subscription
        return subscription

    def unsubscribe(self, subscription: TypertSubscription) -> None:
        """Remove and close one subscription."""
        with self._lock:
            self._subscriptions.pop(id(subscription), None)
        subscription.close()

    def publish(self, change: RegistryChange) -> None:
        """Publish one change to every live subscriber."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            subscription.publish(change)

    def close(self) -> None:
        """Close every subscription."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.close()

    def count(self) -> int:
        """Return the number of live subscriptions."""
        with self._lock:
            return len(self._subscriptions)


@dataclass(slots=True)
class TypertService:
    """The process-wide capability registry and its loader.

    Attributes:
        registry: The live registry.
        loader: The package loader.
        changes: The change hub.
        bootstrap_errors: Problems seen while registering built-ins.
    """

    registry: TypertRegistry
    loader: TypertLoader
    changes: TypertChangeHub
    bootstrap_errors: list[str]
    _builtin_disposers: list[Disposer]
    _registry_subscription: Disposer | None
    _started: bool
    _stopped: bool
    _start_lock: threading.RLock

    @classmethod
    def create(cls) -> TypertService:
        """Build a service with the built-in resource and lookup declarations."""
        resource_kinds = ResourceKindRegistry()
        authorizers = built_in_resource_authorizers()
        for kind, description, attribute in _BUILTIN_RESOURCE_KINDS:
            resource_kinds.register(
                kind,
                extract=_attribute_identity(attribute),
                authorize=authorizers.get(kind),
                description=description,
            )
        registry = TypertRegistry(resource_kinds=resource_kinds)
        for kind, _description, _attribute in _BUILTIN_RESOURCE_KINDS:
            registry.lookups.register(
                kind,
                LookupProvider(
                    parameter=kind.replace("-", "_"),
                    wire="id",
                    host_type_symbol=f"deepspace.{kind}",
                    wire_type_symbol="string",
                    resource=kind,
                ),
            )
        loader = TypertLoader(registry=registry)
        changes = TypertChangeHub()
        service = cls(
            registry=registry,
            loader=loader,
            changes=changes,
            bootstrap_errors=[],
            _builtin_disposers=[],
            _registry_subscription=None,
            _started=False,
            _stopped=False,
            _start_lock=threading.RLock(),
        )
        service._registry_subscription = registry.subscribe(changes.publish)
        service._register_builtins()
        return service

    # -- lifecycle ------------------------------------------------------------

    def _register_builtins(self) -> None:
        try:
            contribution = contribution_from_native_specs()
            disposer = self.registry.register(contribution)
            self._builtin_disposers.append(disposer)
        except Exception as exc:  # noqa: BLE001 - introspection must not stop the app
            message = f"native tools: {exc}"
            self.bootstrap_errors.append(message)
            logger.warning("Typert could not reflect DeepSpace native tools: %s", exc)

        try:
            contribution = _todo_contribution()
            disposer = self.registry.register(contribution)
            self._builtin_disposers.append(disposer)
        except Exception as exc:  # noqa: BLE001 - introspection must not stop the app
            message = f"todo: {exc}"
            self.bootstrap_errors.append(message)
            logger.warning("Typert could not reflect the Todo contract: %s", exc)

    def start(self, settings: Any | None = None) -> None:
        """Load configured and discovered packages once."""
        with self._start_lock:
            if self._started or self._stopped:
                return
            self._started = True
            package_setting = str(getattr(settings, "deepspace_typert_packages", "") or "").strip()
            configured = [item.strip() for item in package_setting.split(",") if item.strip()]
            if configured:
                try:
                    self.loader.load_configured(configured)
                except Exception as exc:  # noqa: BLE001 - report, do not crash startup
                    self.bootstrap_errors.append(str(exc))
                    logger.warning("Typert configured packages failed to load: %s", exc)
            try:
                self.loader.discover()
            except Exception as exc:  # noqa: BLE001 - discovery is best effort
                self.bootstrap_errors.append(f"entry-point discovery: {exc}")
                logger.warning("Typert entry-point discovery failed: %s", exc)
            manifest_dir = str(getattr(settings, "deepspace_typert_manifest_dir", "") or "").strip()
            if manifest_dir:
                self.loader.discover_directory(manifest_dir)

    def stop(self) -> None:
        """Withdraw every mounted package and close the change stream."""
        with self._start_lock:
            if self._stopped:
                return
            self._stopped = True
            self.loader.unmount_all()
            for disposer in reversed(self._builtin_disposers):
                disposer.dispose()
            self._builtin_disposers.clear()
            if self._registry_subscription is not None:
                self._registry_subscription.dispose()
                self._registry_subscription = None
            self.changes.close()

    @property
    def started(self) -> bool:
        """Whether :meth:`start` ran."""
        return self._started

    # -- reads ----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return the full inspectable capability state."""
        snapshot = self.registry.snapshot()
        snapshot["loader"] = self.loader.status()
        snapshot["bootstrap_errors"] = list(self.bootstrap_errors)
        snapshot["started"] = self._started
        snapshot["subscribers"] = self.changes.count()
        snapshot["resource_kinds"] = self.registry.resource_kinds.as_dict()
        snapshot["descriptor_packages"] = self.registry.descriptor_packages()
        return snapshot

    def subscribe_changes(self) -> TypertSubscription:
        """Subscribe to every registry change."""
        return self.changes.subscribe()

    def unsubscribe_changes(self, subscription: TypertSubscription) -> None:
        """Close one subscription."""
        self.changes.unsubscribe(subscription)


def _todo_contribution() -> RemoteContribution:
    """Describe the Todo wire contract as one readable package."""
    from app.deepspace.typert.schema import TypertSchemaFactory

    def arguments_model() -> Any:
        from app.deepspace.services.tool_registry import HarnessTodoWriteArguments

        return HarnessTodoWriteArguments

    schema = TypertSchemaFactory(
        key=f"{TODO_PACKAGE}#HarnessTodoWriteArguments",
        package=TODO_PACKAGE,
        name="HarnessTodoWriteArguments",
        factory=arguments_model,
        type_symbol="app.deepspace.services.tool_registry.HarnessTodoWriteArguments",
    )
    descriptor = RemoteDescriptor(
        id=TODO_ENDPOINT,
        service="todo",
        namespace="todo",
        method="write",
        implementation=None,
        parameters=(
            RemoteParameter(
                name="todos",
                codec=JsonCodec(type_symbol="TodoInput[]"),
                source="json",
                accepts_undefined=False,
            ),
        ),
        result_codec=JsonCodec(type_symbol="{todos,counts}"),
        description=(
            "Replace the conversation's visible task plan with the supplied "
            "ordered list and return the normalized plan plus its counts."
        ),
        resources=(GLOBAL,),
        source=TODO_PACKAGE,
        invocable=False,
    )
    model = TypertPackageModel(
        package=TODO_PACKAGE,
        face="host",
        services=(
            TypertServiceModel(
                name="todo",
                documentation=TypertDocumentation(
                    summary="The Harness-compatible task-plan contract."
                ),
            ),
        ),
        objects=(
            TypertObjectModel(
                name="HarnessTodoWriteArguments",
                documentation=TypertDocumentation(
                    summary="The {todos: [{content, status}]} input shape."
                ),
            ),
        ),
    )
    return RemoteContribution(
        package=TODO_PACKAGE,
        face="host",
        schemas=(schema,),
        descriptors=(descriptor,),
        package_model=model,
    )


_SERVICE: TypertService | None = None
_SERVICE_LOCK = threading.Lock()


def get_typert_service() -> TypertService:
    """Return the process-wide service, creating it on first use."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = TypertService.create()
        return _SERVICE


def reset_typert_service() -> None:
    """Dispose and drop the process-wide service.

    This exists for tests and for an orderly embedded-runtime restart; a
    normal request never calls it.
    """
    global _SERVICE
    with _SERVICE_LOCK:
        service = _SERVICE
        _SERVICE = None
    if service is not None:
        service.stop()


__all__ = [
    "TODO_ENDPOINT",
    "TODO_PACKAGE",
    "TypertChangeHub",
    "TypertService",
    "TypertSubscription",
    "get_typert_service",
    "reset_typert_service",
]
