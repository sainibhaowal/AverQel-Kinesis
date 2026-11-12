"""The runtime registry of schemas, reflection, invocations, and providers.

The registry owns five stores:

* the schema and package-reflection store, populated by an atomic package
  contribution;
* the local invocation store, the descriptors this process implements;
* the remote invocation store, consumer-selected descriptors from another
  compiled face;
* the lookup and Context stores, which turn a wire identity into a host
  object;
* the resource store, which declares the default wire field of a resource
  kind.

Every registration returns a :class:`Disposer`.  Disposing it withdraws
exactly the registration that created it, which is how a mounted package's
contribution is removed when the package unmounts.  A contribution is
validated as a whole before anything is published, so a malformed batch
cannot leave half its schemas or half its endpoints behind.

The registry also invokes a local endpoint in process.  That path is the
Python-native equivalent of the Harness's in-process dispatch: it validates
and decodes every argument, resolves lookup parameters through the registered
providers, binds the ambient invocation, encodes the result, and releases
invocation-owned values.

The API in :mod:`app.deepspace.typert.api` exposes only authenticated,
explicitly scoped capability calls. Its HTTP and WebSocket boundaries reject
unscoped endpoints and resource-bound endpoints without a resource
authorization adapter.
"""

from __future__ import annotations

import builtins
import inspect
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.deepspace.typert.descriptors import RemoteContribution, RemoteDescriptor
from app.deepspace.typert.errors import (
    RemoteError,
    RemoteResult,
    remote_error_of,
)
from app.deepspace.typert.invocation import RemoteInvocation, invocation_scope
from app.deepspace.typert.json_value import UNSET
from app.deepspace.typert.principal import Principal
from app.deepspace.typert.resources import ResourceKindRegistry

logger = logging.getLogger(__name__)

#: One registration in the schema store.
SCHEMA_CHANGE = "schema"

#: One registration in the package-reflection store.
PACKAGE_CHANGE = "package"

#: One registration in the local invocation store.
LOCAL_CHANGE = "local"

#: One registration in the remote invocation store.
REMOTE_CHANGE = "remote"

#: One registration in the lookup store.
LOOKUP_CHANGE = "lookup"

#: One registration in the host Context store.
HOST_CONTEXT_CHANGE = "host-context"

#: One registration in the client Context store.
CLIENT_CONTEXT_CHANGE = "client-context"

#: One registration in the resource-kind store.
RESOURCE_CHANGE = "resource"

_WIRE_NAME = re.compile(r"^[A-Za-z0-9_$.\-]+$")


def typert_key(package: str, name: str) -> str:
    """Compose the global key of one generated schema."""
    return f"{package}#{name}"


def typert_package_key(package: str, face: str) -> str:
    """Compose the identity of one package-face model."""
    return f"{package}#{face}"


def typert_endpoint(descriptor: RemoteDescriptor) -> str:
    """Compose the endpoint key of one invocation descriptor."""
    return descriptor.endpoint


@dataclass(frozen=True, slots=True)
class RegistryChange:
    """One change a subscriber can observe.

    Attributes:
        kind: The store that changed.
        key: The schema key, package key, endpoint, lookup key, Context key,
            or resource kind that changed.
    """

    kind: str
    key: str

    def as_dict(self) -> dict[str, str]:
        """Return the JSON-compatible wire form."""
        return {"kind": self.kind, "key": self.key}


RegistryListener = Callable[[RegistryChange], None]


class Disposer:
    """One idempotent withdrawal.

    A disposer is callable, so it can be used directly as a callback, and it
    is a context manager, so a caller can scope a contribution to a block.
    """

    __slots__ = ("_dispose", "_done")

    def __init__(self, dispose: Callable[[], None]) -> None:
        if not callable(dispose):
            raise TypeError("A disposer needs a callable withdrawal.")
        self._dispose = dispose
        self._done = False

    @property
    def active(self) -> bool:
        """Whether the registration is still in place."""
        return not self._done

    def dispose(self) -> None:
        """Withdraw the registration at most once."""
        if self._done:
            return
        self._done = True
        self._dispose()

    def __call__(self) -> None:
        self.dispose()

    def __enter__(self) -> Disposer:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.dispose()


class ChangeSource:
    """A small, thread-safe, failure-contained observer set."""

    def __init__(self) -> None:
        self._listeners: dict[int, RegistryListener] = {}
        self._lock = threading.RLock()

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Add one observer and return its withdrawal."""
        if not callable(listener):
            raise TypeError("A registry listener must be callable.")
        token = id(listener)
        with self._lock:
            self._listeners[token] = listener
        return Disposer(lambda: self._remove(token))

    def _remove(self, token: int) -> None:
        with self._lock:
            self._listeners.pop(token, None)

    def emit(self, change: RegistryChange) -> None:
        """Notify every observer; one failing observer cannot stop the rest."""
        with self._lock:
            listeners = list(self._listeners.values())
        for listener in listeners:
            try:
                listener(change)
            except Exception:  # noqa: BLE001 - observer isolation is the contract
                logger.warning(
                    "typert registry observer for %s %r failed",
                    change.kind,
                    change.key,
                    exc_info=True,
                )

    def clear(self) -> None:
        """Remove every observer, used when a deployment shuts down."""
        with self._lock:
            self._listeners.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._listeners)


def _validate_segment(subject: str, value: str) -> None:
    if not isinstance(value, str) or not value or "#" in value:
        raise RemoteError(
            "gateway/internal",
            f"Invalid {subject} {value!r}: it must be non-empty and must not contain '#'.",
        )


def _validate_wire_name(subject: str, value: str) -> None:
    if value in {".", ".."} or not _WIRE_NAME.match(value):
        raise RemoteError(
            "gateway/internal",
            f"Invalid {subject} {value!r}: it may only use endpoint characters.",
        )


def _validate_nonempty(subject: str, value: str) -> None:
    if not value:
        raise RemoteError("gateway/internal", f"Invalid {subject}: it must be non-empty.")


def validate_invocation(descriptor: RemoteDescriptor) -> None:
    """Validate one invocation descriptor for the registry.

    This is stricter than :meth:`RemoteDescriptor.validate`: it also checks
    the wire fields, duplicate wire fields, the Context receiver, and the
    scope projection.

    Raises:
        RemoteError: With ``gateway/internal`` for a declaration bug.
    """
    descriptor.validate()
    _validate_nonempty("invocation id", descriptor.id)
    _validate_segment("invocation service key", descriptor.service)
    _validate_wire_name("invocation namespace", descriptor.namespace)
    _validate_wire_name("invocation method", descriptor.method)
    wires: set[str] = set()
    for parameter in descriptor.parameters:
        wire = parameter.wire or parameter.name
        _validate_wire_name("parameter wire field", wire)
        if wire in wires:
            raise RemoteError(
                "gateway/internal",
                f"Invocation {descriptor.id!r} repeats wire field {wire!r}.",
            )
        wires.add(wire)
        if parameter.source == "lookup" and parameter.lookup is not None:
            _validate_segment("lookup key", parameter.lookup)
    if descriptor.receiver.kind == "context":
        assert descriptor.receiver.wire is not None
        if descriptor.receiver.wire in wires:
            raise RemoteError(
                "gateway/internal",
                f"Invocation {descriptor.id!r} repeats wire field {descriptor.receiver.wire!r}.",
            )
        wires.add(descriptor.receiver.wire)


@dataclass(frozen=True, slots=True)
class _DescriptorEntry:
    descriptor: RemoteDescriptor
    owner: object


class DescriptorStore:
    """One endpoint-addressed invocation store."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._entries: dict[str, _DescriptorEntry] = {}
        self._ids: dict[str, _DescriptorEntry] = {}
        self._history: set[str] = set()
        self._changes = ChangeSource()
        self._lock = threading.RLock()

    def validate(self, descriptors: Iterable[RemoteDescriptor]) -> None:
        """Reject a batch that repeats an endpoint or an id."""
        endpoints: set[str] = set()
        ids: set[str] = set()
        with self._lock:
            for descriptor in descriptors:
                validate_invocation(descriptor)
                endpoint = typert_endpoint(descriptor)
                if endpoint in endpoints or endpoint in self._entries:
                    raise RemoteError(
                        "gateway/conflict",
                        f"The {self.kind} endpoint {endpoint!r} is already registered.",
                    )
                if descriptor.id in ids or descriptor.id in self._ids:
                    raise RemoteError(
                        "gateway/conflict",
                        f"The {self.kind} invocation id {descriptor.id!r} is already registered.",
                    )
                endpoints.add(endpoint)
                ids.add(descriptor.id)

    def commit(self, owner: object, descriptors: Iterable[RemoteDescriptor]) -> None:
        """Publish a validated batch for one owner."""
        batch = tuple(descriptors)
        with self._lock:
            for descriptor in batch:
                entry = _DescriptorEntry(descriptor=descriptor, owner=owner)
                self._entries[typert_endpoint(descriptor)] = entry
                self._ids[descriptor.id] = entry
                self._history.add(typert_endpoint(descriptor))
                self._changes.emit(RegistryChange(self.kind, typert_endpoint(descriptor)))

    def withdraw(self, owner: object, descriptors: Iterable[RemoteDescriptor]) -> None:
        """Remove exactly the batch still owned by ``owner``."""
        removed: list[str] = []
        with self._lock:
            for descriptor in descriptors:
                endpoint = typert_endpoint(descriptor)
                entry = self._entries.get(endpoint)
                if entry is None or entry.owner is not owner:
                    continue
                del self._entries[endpoint]
                if self._ids.get(descriptor.id) is entry:
                    del self._ids[descriptor.id]
                removed.append(endpoint)
        for endpoint in removed:
            self._changes.emit(RegistryChange(self.kind, endpoint))

    def get(self, endpoint: str) -> RemoteDescriptor | None:
        """Return one descriptor by endpoint."""
        with self._lock:
            entry = self._entries.get(endpoint)
            return entry.descriptor if entry is not None else None

    def has_seen(self, endpoint: str) -> bool:
        """Whether an endpoint ever existed in this registry lifetime."""
        with self._lock:
            return endpoint in self._history

    def list(self) -> list[RemoteDescriptor]:
        """Return the live descriptors in registration order."""
        with self._lock:
            return [entry.descriptor for entry in self._entries.values()]

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe endpoint changes."""
        return self._changes.subscribe(listener)

    def notify(self, change: RegistryChange) -> None:
        """Emit one change that belongs to this store's kind."""
        self._changes.emit(change)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def as_dict(self) -> builtins.list[dict[str, Any]]:
        """Return the inspectable descriptor list."""
        return [descriptor.as_dict() for descriptor in self.list()]


class RemoteStore:
    """Consumer-selected descriptors owned by a named remote package."""

    def __init__(self) -> None:
        self._descriptors = DescriptorStore("remote")
        self._packages: dict[str, object] = {}
        self._lock = threading.RLock()

    def register(self, package: str, descriptors: Iterable[RemoteDescriptor]) -> Disposer:
        """Register one remote package's descriptors atomically."""
        _validate_segment("Remote package name", package)
        batch = tuple(descriptors)
        with self._lock:
            if package in self._packages:
                raise RemoteError(
                    "gateway/conflict",
                    f"Remote package {package!r} is already registered.",
                )
            self._descriptors.validate(batch)
            owner = object()
            self._packages[package] = owner
        self._descriptors.commit(owner, batch)

        def withdraw() -> None:
            with self._lock:
                if self._packages.get(package) is owner:
                    del self._packages[package]
            self._descriptors.withdraw(owner, batch)

        return Disposer(withdraw)

    def get(self, endpoint: str) -> RemoteDescriptor | None:
        """Return one remote descriptor by endpoint."""
        return self._descriptors.get(endpoint)

    def list(self) -> list[RemoteDescriptor]:
        """Return every remote descriptor in registration order."""
        return self._descriptors.list()

    def has_seen(self, endpoint: str) -> bool:
        """Whether a remote endpoint ever existed."""
        return self._descriptors.has_seen(endpoint)

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe remote endpoint changes."""
        return self._descriptors.subscribe(listener)

    def packages(self) -> builtins.list[str]:
        """Return the registered remote package names."""
        with self._lock:
            return builtins.list(self._packages)

    def as_dict(self) -> builtins.list[dict[str, Any]]:
        """Return the inspectable remote descriptor list."""
        return self._descriptors.as_dict()


@dataclass(frozen=True, slots=True)
class LookupProvider:
    """How one host object kind is found from a wire identity.

    Attributes:
        parameter: The implementation parameter this provider feeds.
        wire: The wire field that carries the identity.
        host_type_symbol: The host object's portable type name.
        wire_type_symbol: The wire identity's portable type name.
        resource: The declared resource kind the identity names, if any.
        resolve: The resolver that turns an identity into a host object.
    """

    parameter: str
    wire: str
    host_type_symbol: str
    wire_type_symbol: str
    resource: str | None = None
    resolve: Callable[[Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class LookupDefinition:
    """The stable wire declaration of one lookup key."""

    key: str
    parameter: str
    wire: str
    host_type_symbol: str
    wire_type_symbol: str

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible form."""
        return {
            "key": self.key,
            "parameter": self.parameter,
            "wire": self.wire,
            "host_type_symbol": self.host_type_symbol,
            "wire_type_symbol": self.wire_type_symbol,
        }


class LookupStore:
    """Host object lookup providers and their optional resolvers."""

    def __init__(self) -> None:
        self._providers: dict[str, LookupProvider] = {}
        self._resolvers: dict[str, Callable[[Any], Any]] = {}
        self._definitions: dict[str, LookupDefinition] = {}
        self._changes = ChangeSource()
        self._lock = threading.RLock()

    def register(self, key: str, provider: LookupProvider) -> Disposer:
        """Declare one lookup provider.

        Raises:
            RemoteError: When the key or declaration is invalid, already
                registered, or silently changed from an earlier declaration.
        """
        _validate_segment("lookup key", key)
        _validate_segment("lookup parameter", provider.parameter)
        _validate_wire_name("lookup wire field", provider.wire)
        _validate_nonempty("lookup Host type symbol", provider.host_type_symbol)
        _validate_nonempty("lookup wire type symbol", provider.wire_type_symbol)
        if provider.resource == "global":
            raise RemoteError(
                "gateway/internal",
                "The global resource has no lookup identity.",
            )
        definition = LookupDefinition(
            key=key,
            parameter=provider.parameter,
            wire=provider.wire,
            host_type_symbol=provider.host_type_symbol,
            wire_type_symbol=provider.wire_type_symbol,
        )
        with self._lock:
            if key in self._providers:
                raise RemoteError(
                    "gateway/conflict",
                    f"Lookup {key!r} is already registered.",
                )
            known = self._definitions.get(key)
            if known is not None and known != definition:
                raise RemoteError(
                    "gateway/conflict",
                    f"Lookup {key!r} changed its wire declaration during this registry lifetime.",
                )
            self._definitions[key] = definition
            self._providers[key] = provider
        self._changes.emit(RegistryChange(LOOKUP_CHANGE, key))

        def withdraw() -> None:
            with self._lock:
                if self._providers.get(key) is provider:
                    del self._providers[key]
            self._changes.emit(RegistryChange(LOOKUP_CHANGE, key))

        return Disposer(withdraw)

    def configure(self, key: str, resolver: Callable[[Any], Any]) -> Disposer:
        """Install the resolver for one declared lookup provider."""
        _validate_segment("lookup key", key)
        if not callable(resolver):
            raise TypeError("A lookup resolver must be callable.")
        with self._lock:
            if key in self._resolvers:
                raise RemoteError(
                    "gateway/conflict",
                    f"Lookup {key!r} resolver is already configured.",
                )
            self._resolvers[key] = resolver
        self._changes.emit(RegistryChange(LOOKUP_CHANGE, key))

        def withdraw() -> None:
            with self._lock:
                if self._resolvers.get(key) is resolver:
                    del self._resolvers[key]
            self._changes.emit(RegistryChange(LOOKUP_CHANGE, key))

        return Disposer(withdraw)

    def get(self, key: str) -> LookupProvider | None:
        """Return one provider with its resolver merged in, when configured."""
        with self._lock:
            provider = self._providers.get(key)
            if provider is None:
                return None
            resolver = self._resolvers.get(key)
        if resolver is None or provider.resolve is resolver:
            return provider
        return LookupProvider(
            parameter=provider.parameter,
            wire=provider.wire,
            host_type_symbol=provider.host_type_symbol,
            wire_type_symbol=provider.wire_type_symbol,
            resource=provider.resource,
            resolve=resolver,
        )

    def definitions(self) -> list[LookupDefinition]:
        """Return every lookup declaration in declaration order."""
        with self._lock:
            return list(self._definitions.values())

    def keys(self) -> builtins.list[str]:
        """Return the live lookup keys."""
        with self._lock:
            return list(self._providers)

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe lookup changes."""
        return self._changes.subscribe(listener)

    def as_dict(self) -> list[dict[str, Any]]:
        """Return the inspectable lookup declarations."""
        return [definition.as_dict() for definition in self.definitions()]


@dataclass(frozen=True, slots=True)
class HostContextAdapter:
    """How one host Context is carried over the wire.

    Attributes:
        wire: The wire field that carries the Context identity.
        wire_type_symbol: The identity's portable type name.
        resource: The declared resource kind, if any.
        resolve: A resolver from the identity to the host Context.
    """

    wire: str
    wire_type_symbol: str
    resource: str | None = None
    resolve: Callable[[Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class ClientContextAdapter:
    """How one client Context is described on the wire."""

    wire: str
    wire_type_symbol: str
    resource: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ContextStore:
    """Host and client Context adapters."""

    def __init__(self) -> None:
        self._hosts: dict[str, HostContextAdapter] = {}
        self._host_resolvers: dict[str, Callable[[Any], Any]] = {}
        self._clients: dict[str, ClientContextAdapter] = {}
        self._changes = ChangeSource()
        self._lock = threading.RLock()

    def register_host(self, key: str, adapter: HostContextAdapter) -> Disposer:
        """Declare one host Context adapter."""
        _validate_segment("Context key", key)
        _validate_wire_name("Context wire field", adapter.wire)
        _validate_nonempty("Context wire type symbol", adapter.wire_type_symbol)
        if adapter.resource == "global":
            raise RemoteError(
                "gateway/internal",
                "The global resource has no Context identity.",
            )
        with self._lock:
            if key in self._hosts:
                raise RemoteError(
                    "gateway/conflict",
                    f"Host Context {key!r} is already registered.",
                )
            self._hosts[key] = adapter
        self._changes.emit(RegistryChange(HOST_CONTEXT_CHANGE, key))
        return Disposer(lambda: self._withdraw_host(key, adapter))

    def _withdraw_host(self, key: str, adapter: HostContextAdapter) -> None:
        with self._lock:
            if self._hosts.get(key) is adapter:
                del self._hosts[key]
        self._changes.emit(RegistryChange(HOST_CONTEXT_CHANGE, key))

    def configure_host(self, key: str, resolver: Callable[[Any], Any]) -> Disposer:
        """Install the resolver for one host Context."""
        _validate_segment("Context key", key)
        if not callable(resolver):
            raise TypeError("A Context resolver must be callable.")
        with self._lock:
            if key in self._host_resolvers:
                raise RemoteError(
                    "gateway/conflict",
                    f"Host Context {key!r} resolver is already configured.",
                )
            self._host_resolvers[key] = resolver
        self._changes.emit(RegistryChange(HOST_CONTEXT_CHANGE, key))

        def withdraw() -> None:
            with self._lock:
                if self._host_resolvers.get(key) is resolver:
                    del self._host_resolvers[key]
            self._changes.emit(RegistryChange(HOST_CONTEXT_CHANGE, key))

        return Disposer(withdraw)

    def register_client(self, key: str, adapter: ClientContextAdapter) -> Disposer:
        """Declare one client Context adapter."""
        _validate_segment("Context key", key)
        _validate_wire_name("Context wire field", adapter.wire)
        _validate_nonempty("Context wire type symbol", adapter.wire_type_symbol)
        with self._lock:
            if key in self._clients:
                raise RemoteError(
                    "gateway/conflict",
                    f"Client Context {key!r} is already registered.",
                )
            self._clients[key] = adapter
        self._changes.emit(RegistryChange(CLIENT_CONTEXT_CHANGE, key))

        def withdraw() -> None:
            with self._lock:
                if self._clients.get(key) is adapter:
                    del self._clients[key]
            self._changes.emit(RegistryChange(CLIENT_CONTEXT_CHANGE, key))

        return Disposer(withdraw)

    def get_host(self, key: str) -> HostContextAdapter | None:
        """Return one host adapter with its resolver merged in."""
        with self._lock:
            adapter = self._hosts.get(key)
            if adapter is None:
                return None
            resolver = self._host_resolvers.get(key)
        if resolver is None or adapter.resolve is resolver:
            return adapter
        return HostContextAdapter(
            wire=adapter.wire,
            wire_type_symbol=adapter.wire_type_symbol,
            resource=adapter.resource,
            resolve=resolver,
        )

    def get_client(self, key: str) -> ClientContextAdapter | None:
        """Return one client adapter."""
        with self._lock:
            return self._clients.get(key)

    def keys(self) -> dict[str, list[str]]:
        """Return the registered host and client keys."""
        with self._lock:
            return {"host": list(self._hosts), "client": list(self._clients)}

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe Context changes."""
        return self._changes.subscribe(listener)

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable Context declarations."""
        with self._lock:
            hosts = {
                key: {
                    "wire": adapter.wire,
                    "wire_type_symbol": adapter.wire_type_symbol,
                    "resource": adapter.resource,
                    "has_resolver": key in self._host_resolvers,
                }
                for key, adapter in self._hosts.items()
            }
            clients = {
                key: {
                    "wire": adapter.wire,
                    "wire_type_symbol": adapter.wire_type_symbol,
                    "resource": adapter.resource,
                    "metadata": dict(adapter.metadata),
                }
                for key, adapter in self._clients.items()
            }
        return {"host": hosts, "client": clients}


@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    """The default wire rule of one resource kind.

    Exactly one of ``field`` or ``resolve`` is declared.  ``field`` names the
    wire field that carries the identity; ``resolve`` reads it from a host
    object.
    """

    field: str | None = None
    resolve: Callable[[Any], Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable declaration."""
        return {
            "field": self.field,
            "has_resolver": self.resolve is not None,
        }


class ResourceStore:
    """Default wire rules for declared resource kinds."""

    #: The identity-less resource kind, reserved by the protocol.
    GLOBAL_KIND = "global"

    def __init__(self) -> None:
        self._declarations: dict[str, ResourceDeclaration] = {}
        self._changes = ChangeSource()
        self._lock = threading.RLock()

    def register(self, kind: str, declaration: ResourceDeclaration) -> Disposer:
        """Declare one resource kind's default wire rule."""
        _validate_segment("resource kind", kind)
        if kind == self.GLOBAL_KIND:
            raise RemoteError(
                "gateway/internal",
                f"Resource kind {self.GLOBAL_KIND!r} is reserved for the built-in global resource.",
            )
        if (declaration.field is None) == (declaration.resolve is None):
            raise RemoteError(
                "gateway/internal",
                f"Resource kind {kind!r} must declare exactly one of field or resolve.",
            )
        if declaration.field is not None:
            _validate_wire_name("resource field", declaration.field)
        if declaration.resolve is not None and not callable(declaration.resolve):
            raise RemoteError(
                "gateway/internal",
                f"Resource kind {kind!r} has a non-callable resolver.",
            )
        with self._lock:
            if kind in self._declarations:
                raise RemoteError(
                    "gateway/conflict",
                    f"Resource kind {kind!r} is already registered.",
                )
            self._declarations[kind] = declaration
        self._changes.emit(RegistryChange(RESOURCE_CHANGE, kind))

        def withdraw() -> None:
            with self._lock:
                if self._declarations.get(kind) is declaration:
                    del self._declarations[kind]
            self._changes.emit(RegistryChange(RESOURCE_CHANGE, kind))

        return Disposer(withdraw)

    def get(self, kind: str) -> ResourceDeclaration | None:
        """Return one declaration, if it exists."""
        with self._lock:
            return self._declarations.get(kind)

    def entries(self) -> list[dict[str, Any]]:
        """Return every declaration in declaration order."""
        with self._lock:
            return [
                {"kind": kind, **declaration.as_dict()}
                for kind, declaration in self._declarations.items()
            ]

    def keys(self) -> builtins.list[str]:
        """Return the declared kinds."""
        with self._lock:
            return list(self._declarations)

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe resource-kind changes."""
        return self._changes.subscribe(listener)


@dataclass(frozen=True, slots=True)
class TypertSchemaRecord:
    """A live schema plus the identity of the contribution that owns it."""

    name: str
    schema: Any
    package: str
    face: str
    key: str

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable description without the schema body."""
        return {
            "name": self.name,
            "package": self.package,
            "face": self.face,
            "key": self.key,
            "schema_type": type(self.schema).__name__,
        }


@dataclass(frozen=True, slots=True)
class TypertPackageRecord:
    """A live package-reflection model plus its stable identity."""

    package: str
    face: str
    key: str
    model: Any

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible reflection."""
        model = self.model
        if hasattr(model, "as_dict"):
            return model.as_dict()  # type: ignore[no-any-return]
        if isinstance(model, Mapping):
            return dict(model)
        return {"package": self.package, "face": self.face, "model": str(model)}


@dataclass(slots=True)
class _SchemaFactoryRecord:
    name: str
    create: Callable[[], Any]
    package: str
    face: str
    key: str
    value: Any = None

    def materialize(self) -> TypertSchemaRecord:
        if self.value is None:
            self.value = self.create()
        return TypertSchemaRecord(
            name=self.name,
            schema=self.value,
            package=self.package,
            face=self.face,
            key=self.key,
        )


def _schema_parts(value: Any) -> tuple[str, Callable[[], Any]]:
    """Read a name and factory from one contributed schema."""
    name = getattr(value, "name", None)
    create = getattr(value, "create", None)
    if isinstance(name, str) and callable(create):
        return name, create
    if isinstance(value, tuple) and len(value) == 2:
        tuple_name, tuple_create = value
        if isinstance(tuple_name, str) and callable(tuple_create):
            return tuple_name, tuple_create
    raise RemoteError(
        "gateway/internal",
        "A contributed schema needs a name and a create() factory.",
    )


class TypertRegistry:
    """Schemas, reflection, invocations, and dependency providers."""

    def __init__(self, *, resource_kinds: ResourceKindRegistry | None = None) -> None:
        self._schemas: dict[str, _SchemaFactoryRecord] = {}
        self._packages: dict[str, TypertPackageRecord] = {}
        self._descriptor_packages: dict[str, str] = {}
        self._local = DescriptorStore("local")
        self._remotes = RemoteStore()
        self._lookups = LookupStore()
        self._contexts = ContextStore()
        self._resources = ResourceStore()
        self._resource_kinds = resource_kinds or ResourceKindRegistry()
        self._lock = threading.RLock()

    @property
    def local(self) -> DescriptorStore:
        """The invocation descriptors this process implements."""
        return self._local

    @property
    def remotes(self) -> RemoteStore:
        """Consumer-selected descriptors from another compiled face."""
        return self._remotes

    @property
    def lookups(self) -> LookupStore:
        """Host object lookup providers."""
        return self._lookups

    @property
    def contexts(self) -> ContextStore:
        """Host and client Context adapters."""
        return self._contexts

    @property
    def resources(self) -> ResourceStore:
        """Default wire rules for resource kinds."""
        return self._resources

    @property
    def resource_kinds(self) -> ResourceKindRegistry:
        """The declared resource kinds used by descriptors."""
        return self._resource_kinds

    def register(self, contribution: RemoteContribution) -> Disposer:
        """Register one package contribution atomically.

        The whole batch is validated before anything becomes visible.  The
        returned disposer removes exactly this contribution.

        Raises:
            RemoteError: When the contribution is malformed or conflicts
                with a live registration.
        """
        contribution.validate()
        _validate_segment("package name", contribution.package)
        if contribution.face not in {"host", "client"}:
            raise RemoteError(
                "gateway/internal",
                f"Invalid face {contribution.face!r}: expected 'host' or 'client'.",
            )
        package_key = typert_package_key(contribution.package, contribution.face)
        with self._lock:
            if package_key in self._packages:
                raise RemoteError(
                    "gateway/conflict",
                    f"Package face {package_key!r} is already registered.",
                )
            records: list[_SchemaFactoryRecord] = []
            batch_keys: set[str] = set()
            for raw in contribution.schemas:
                name, create = _schema_parts(raw)
                _validate_segment("schema name", name)
                key = typert_key(contribution.package, name)
                if key in batch_keys or key in self._schemas:
                    raise RemoteError(
                        "gateway/conflict",
                        f"Schema {key!r} is already registered.",
                    )
                batch_keys.add(key)
                records.append(
                    _SchemaFactoryRecord(
                        name=name,
                        create=create,
                        package=contribution.package,
                        face=contribution.face,
                        key=key,
                    )
                )
            self._local.validate(contribution.descriptors)

            package_record = TypertPackageRecord(
                package=contribution.package,
                face=contribution.face,
                key=package_key,
                model=contribution.package_model,
            )
            with self._lock:
                self._packages[package_key] = package_record
                for record in records:
                    self._schemas[record.key] = record
                for descriptor in contribution.descriptors:
                    self._descriptor_packages[descriptor.id] = contribution.package
            owner = object()
            self._local.commit(owner, contribution.descriptors)
        for record in records:
            self._local.notify(RegistryChange(SCHEMA_CHANGE, record.key))
        self._local.notify(RegistryChange(PACKAGE_CHANGE, package_key))

        def withdraw() -> None:
            with self._lock:
                if self._packages.get(package_key) is package_record:
                    del self._packages[package_key]
                for record in records:
                    if self._schemas.get(record.key) is record:
                        del self._schemas[record.key]
                for descriptor in contribution.descriptors:
                    if self._descriptor_packages.get(descriptor.id) == contribution.package:
                        del self._descriptor_packages[descriptor.id]
            self._local.withdraw(owner, contribution.descriptors)
            for record in records:
                self._local.notify(RegistryChange(SCHEMA_CHANGE, record.key))
            self._local.notify(RegistryChange(PACKAGE_CHANGE, package_key))

        return Disposer(withdraw)

    def get(self, key: str) -> TypertSchemaRecord | None:
        """Look up and materialize one schema by ``<package>#<name>``."""
        with self._lock:
            record = self._schemas.get(key)
        return record.materialize() if record is not None else None

    def resolve(self, key: str) -> TypertSchemaRecord:
        """Resolve one required schema.

        Raises:
            RemoteError: With ``gateway/not-found`` when the key is
                malformed, the package face is absent, or the schema was not
                contributed.
        """
        with self._lock:
            record = self._schemas.get(key)
            if record is not None:
                return record.materialize()
            hash_index = key.find("#")
            if hash_index <= 0 or hash_index == len(key) - 1:
                raise RemoteError(
                    "gateway/not-found",
                    f"Invalid schema key {key!r}: expected '<package>#<name>'.",
                )
            package = key[:hash_index]
            contributed = any(candidate.package == package for candidate in self._packages.values())
        if contributed:
            raise RemoteError(
                "gateway/not-found",
                f"Cannot resolve {key!r}: package {package!r} registers no schema "
                f"named {key[hash_index + 1:]!r}.",
            )
        raise RemoteError(
            "gateway/not-found",
            f"Cannot resolve {key!r}: package {package!r} has no registered contribution.",
        )

    def list(
        self, *, package: str | None = None, face: str | None = None
    ) -> list[TypertSchemaRecord]:
        """Enumerate live schemas in registration order."""
        with self._lock:
            records = [
                record
                for record in self._schemas.values()
                if (package is None or record.package == package)
                and (face is None or record.face == face)
            ]
        return [record.materialize() for record in records]

    def keys(self) -> builtins.list[str]:
        """Return every live schema key."""
        with self._lock:
            return builtins.list(self._schemas)

    def get_package(self, package: str, face: str = "host") -> TypertPackageRecord | None:
        """Look up one package face's reflection."""
        with self._lock:
            return self._packages.get(typert_package_key(package, face))

    def list_packages(
        self,
        *,
        package: str | None = None,
        face: str | None = None,
    ) -> builtins.list[TypertPackageRecord]:
        """Enumerate package reflection in registration order."""
        with self._lock:
            return [
                record
                for record in self._packages.values()
                if (package is None or record.package == package)
                and (face is None or record.face == face)
            ]

    def descriptors_for_package(self, package: str) -> builtins.list[RemoteDescriptor]:
        """Return the local descriptors one package contributed."""
        with self._lock:
            ids = {
                endpoint
                for endpoint, owner in self._descriptor_packages.items()
                if owner == package
            }
        return builtins.list(
            descriptor for descriptor in self._local.list() if descriptor.id in ids
        )

    def descriptor_packages(self) -> dict[str, str]:
        """Return the package that owns each local endpoint."""
        with self._lock:
            return dict(self._descriptor_packages)

    def to_json_schema(self, key: str) -> dict[str, Any]:
        """Project a live schema to the tool JSON Schema subset."""
        from app.deepspace.typert.schema import json_schema_for

        return json_schema_for(self.resolve(key).schema)

    def subscribe(self, listener: RegistryListener) -> Disposer:
        """Observe every store through one listener."""
        disposers = [
            self._local.subscribe(listener),
            self._remotes.subscribe(listener),
            self._lookups.subscribe(listener),
            self._contexts.subscribe(listener),
            self._resources.subscribe(listener),
        ]

        def dispose_all() -> None:
            for disposer in disposers:
                disposer.dispose()

        return Disposer(dispose_all)

    def snapshot(self) -> dict[str, Any]:
        """Return the inspectable registry state for the introspection API."""
        with self._lock:
            schemas = [
                {
                    "name": record.name,
                    "package": record.package,
                    "face": record.face,
                    "key": record.key,
                    "materialized": record.value is not None,
                }
                for record in self._schemas.values()
            ]
            packages = [
                {
                    "package": record.package,
                    "face": record.face,
                    "key": record.key,
                    "model": record.as_dict(),
                }
                for record in self._packages.values()
            ]
        return {
            "schemas": schemas,
            "packages": packages,
            "local": self._local.as_dict(),
            "remote": self._remotes.as_dict(),
            "remote_packages": self._remotes.packages(),
            "lookups": self._lookups.as_dict(),
            "contexts": self._contexts.as_dict(),
            "resources": self._resources.entries(),
        }

    # -- in-process invocation ------------------------------------------------

    def invoke(
        self,
        endpoint: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        invocation: RemoteInvocation | None = None,
        principal: Principal | None = None,
    ) -> Any:
        """Call one local endpoint in process and return its encoded result.

        Args:
            endpoint: The descriptor endpoint, ``<namespace>/<method>``.
            arguments: Wire arguments by wire field.
            invocation: An existing invocation context; a new owner
                invocation is created when omitted.
            principal: The caller for a newly created invocation.

        Returns:
            The result after the result codec encodes it.

        Raises:
            RemoteError: When the endpoint is unknown, an argument is
                invalid, a lookup cannot resolve, or the implementation
                raises.
        """
        descriptor = self._local.get(endpoint)
        if descriptor is None:
            raise RemoteError(
                "gateway/not-found",
                f"Local endpoint {endpoint!r} is not registered.",
            )
        active = invocation or RemoteInvocation(
            principal=principal or Principal(kind="owner", id="deepspace"),
        )
        return self._invoke(descriptor, arguments, active, is_async=False)

    def try_invoke(
        self,
        endpoint: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        invocation: RemoteInvocation | None = None,
        principal: Principal | None = None,
    ) -> RemoteResult[Any]:
        """Call one local endpoint and return a typed success or failure."""
        try:
            value = self.invoke(
                endpoint,
                arguments,
                invocation=invocation,
                principal=principal,
            )
        except RemoteError as exc:
            return RemoteResult.failure(exc)
        except Exception as exc:  # noqa: BLE001 - normalize into the vocabulary
            return RemoteResult.failure(remote_error_of(exc))
        return RemoteResult.success(value)

    async def ainvoke(
        self,
        endpoint: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        invocation: RemoteInvocation | None = None,
        principal: Principal | None = None,
    ) -> Any:
        """Call one local endpoint, awaiting a coroutine implementation."""
        descriptor = self._local.get(endpoint)
        if descriptor is None:
            raise RemoteError(
                "gateway/not-found",
                f"Local endpoint {endpoint!r} is not registered.",
            )
        active = invocation or RemoteInvocation(
            principal=principal or Principal(kind="owner", id="deepspace"),
        )
        return await self._invoke_async(descriptor, arguments, active)

    def _bind_arguments(
        self,
        descriptor: RemoteDescriptor,
        arguments: Mapping[str, Any] | None,
        *,
        is_async: bool,
    ) -> tuple[dict[str, Any], builtins.list[tuple[str, Any]]]:
        supplied = dict(arguments or {})
        kwargs: dict[str, Any] = {}
        pending_awaitables: list[tuple[str, Any]] = []
        for parameter in descriptor.parameters:
            wire = parameter.wire or parameter.name
            if parameter.source == "json":
                if wire in supplied:
                    raw = supplied[wire]
                    if raw is UNSET:
                        if not parameter.accepts_undefined:
                            raise RemoteError(
                                "gateway/bad-request",
                                f"Parameter {parameter.name!r} does not accept an undefined value.",
                            )
                        continue
                    kwargs[parameter.name] = parameter.codec.parse(raw)
                elif parameter.accepts_undefined:
                    continue
                else:
                    raise RemoteError(
                        "gateway/bad-request",
                        f"Missing required parameter {parameter.name!r} (wire {wire!r}).",
                    )
                continue
            if wire not in supplied or supplied[wire] is UNSET:
                raise RemoteError(
                    "gateway/bad-request",
                    f"Missing lookup identity {wire!r} for parameter {parameter.name!r}.",
                )
            identity = supplied[wire]
            provider = self._lookups.get(parameter.lookup or "")
            if provider is None or provider.resolve is None:
                raise RemoteError(
                    "gateway/unavailable",
                    f"Lookup {parameter.lookup!r} has no registered resolver.",
                )
            resolved = provider.resolve(identity)
            if inspect.isawaitable(resolved):
                if not is_async:
                    raise RemoteError(
                        "gateway/internal",
                        f"Lookup {parameter.lookup!r} is asynchronous; use ainvoke().",
                    )
                pending_awaitables.append((parameter.name, resolved))
            else:
                if resolved is None:
                    raise RemoteError(
                        "gateway/not-found",
                        f"Lookup {parameter.lookup!r} found no host object for {identity!r}.",
                    )
                kwargs[parameter.name] = resolved
        if descriptor.receiver.kind == "context":
            wire = descriptor.receiver.wire or ""
            if wire not in supplied or supplied[wire] is UNSET:
                raise RemoteError(
                    "gateway/bad-request",
                    f"Missing receiver identity {wire!r} for endpoint {descriptor.id!r}.",
                )
            adapter = self._contexts.get_host(descriptor.receiver.context or "")
            if adapter is None or adapter.resolve is None:
                raise RemoteError(
                    "gateway/unavailable",
                    f"Context {descriptor.receiver.context!r} has no registered resolver.",
                )
            identity = supplied[wire]
            if descriptor.receiver.codec is not None:
                identity = descriptor.receiver.codec.parse(identity)
            resolved_context = adapter.resolve(identity)
            if inspect.isawaitable(resolved_context):
                if not is_async:
                    raise RemoteError(
                        "gateway/internal",
                        f"Context {descriptor.receiver.context!r} is asynchronous; use ainvoke().",
                    )
                pending_awaitables.append(("context", resolved_context))
            else:
                if resolved_context is None:
                    raise RemoteError(
                        "gateway/not-found",
                        f"Context {descriptor.receiver.context!r} found no host object.",
                    )
                kwargs["context"] = resolved_context
        return kwargs, pending_awaitables

    def _invoke(
        self,
        descriptor: RemoteDescriptor,
        arguments: Mapping[str, Any] | None,
        invocation: RemoteInvocation,
        *,
        is_async: bool,
    ) -> Any:
        assert descriptor.implementation is not None
        invocation.check_active()
        try:
            kwargs, _pending = self._bind_arguments(descriptor, arguments, is_async=is_async)
            if descriptor.cancellation_parameter == "signal":
                signature = inspect.signature(descriptor.implementation)
                if "signal" in signature.parameters:
                    kwargs["signal"] = invocation.cancellation
            with invocation_scope(invocation):
                result = descriptor.implementation(**kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise RemoteError(
                    "gateway/internal",
                    f"Endpoint {descriptor.id!r} is asynchronous; use ainvoke().",
                )
            return descriptor.result_codec.encode(result)
        except RemoteError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize into the vocabulary
            raise remote_error_of(exc) from exc
        finally:
            invocation.dispose()

    async def _invoke_async(
        self,
        descriptor: RemoteDescriptor,
        arguments: Mapping[str, Any] | None,
        invocation: RemoteInvocation,
    ) -> Any:
        assert descriptor.implementation is not None
        invocation.check_active()
        dispose_invocation = True
        try:
            kwargs, pending = self._bind_arguments(descriptor, arguments, is_async=True)
            for name, awaitable in pending:
                resolved = await awaitable
                if resolved is None:
                    raise RemoteError(
                        "gateway/not-found",
                        f"Lookup for parameter {name!r} found no host object.",
                    )
                kwargs[name] = resolved
            if descriptor.cancellation_parameter == "signal":
                signature = inspect.signature(descriptor.implementation)
                if "signal" in signature.parameters:
                    kwargs["signal"] = invocation.cancellation
            with invocation_scope(invocation):
                result = descriptor.implementation(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if descriptor.mode == "stream":
                from app.deepspace.typert.streams import RemoteStream

                if not isinstance(result, RemoteStream):
                    raise RemoteError(
                        "gateway/internal",
                        f"Streaming endpoint {descriptor.id!r} did not return a RemoteStream.",
                    )

                def release_stream_invocation() -> None:
                    invocation.cancellation.cancel("The stream was disposed by its caller.")
                    invocation.dispose()

                result.add_close_callback(release_stream_invocation)
                dispose_invocation = False
                return result
            return descriptor.result_codec.encode(result)
        except RemoteError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize into the vocabulary
            raise remote_error_of(exc) from exc
        finally:
            if dispose_invocation:
                invocation.dispose()


__all__ = [
    "CLIENT_CONTEXT_CHANGE",
    "ChangeSource",
    "ClientContextAdapter",
    "ContextStore",
    "DescriptorStore",
    "Disposer",
    "HOST_CONTEXT_CHANGE",
    "HostContextAdapter",
    "LOCAL_CHANGE",
    "LOOKUP_CHANGE",
    "LookupDefinition",
    "LookupProvider",
    "LookupStore",
    "PACKAGE_CHANGE",
    "REMOTE_CHANGE",
    "RESOURCE_CHANGE",
    "RegistryChange",
    "RegistryListener",
    "RemoteStore",
    "ResourceDeclaration",
    "ResourceStore",
    "SCHEMA_CHANGE",
    "TypertPackageRecord",
    "TypertRegistry",
    "TypertSchemaRecord",
    "typert_endpoint",
    "typert_key",
    "typert_package_key",
    "validate_invocation",
]
