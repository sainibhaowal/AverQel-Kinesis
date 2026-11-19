"""Domain-level storage facilities for DeepSpace.

Domains declare their tables and version once, then use the configured KV
backend through a serialized write queue.  The queue keeps the in-memory
snapshot and durable backend in lockstep and preserves an unknown storage
state after a failed write.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from app.deepspace.services.storage import (
    UNIT_NAME_RE,
    UNIT_NAME_RE_COMPILED,
    KvUnitDescriptor,
    StorageError,
)

logger = logging.getLogger(__name__)


@dataclass
class DomainGlobalSpec:
    """Declaration of a domain-wide singleton value."""

    schema: Any
    initial: Any


@dataclass
class DomainTableSpec:
    """Declaration of one domain table."""

    value_schema: Any
    key_type: str = "str"


@dataclass
class DomainSpec:
    """Static declaration of one storage domain."""

    name: str
    version: int
    layout: Literal["single", "per-record"] | None = None
    global_spec: DomainGlobalSpec | None = None
    tables: dict[str, DomainTableSpec] = field(default_factory=dict)
    compatible_versions: list[int] | None = None
    invalid_records: Literal["backup-and-skip"] | None = None


class DomainError(Exception):
    """A domain operation failed with a stable application error code."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: Any = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        self.cause = cause
        super().__init__(message)


@dataclass
class DomainChangedBase:
    domain: str
    table: str
    key: str


@dataclass
class DomainChangedPut(DomainChangedBase):
    operation: str = "put"
    value: Any = None


@dataclass
class DomainChangedDeleted(DomainChangedBase):
    operation: str = "deleted"


DomainChanged = DomainChangedPut | DomainChangedDeleted


class DomainGlobal:
    """Handle for a domain's optional global singleton."""

    def __init__(self, get_fn: Callable[[], Any], set_fn: Callable[[Any], Awaitable[None]]) -> None:
        self._get_fn = get_fn
        self._set_fn = set_fn

    def get(self) -> Any:
        return self._get_fn()

    async def set(self, value: Any) -> None:
        await self._set_fn(value)


class TableHost:
    """Shared callbacks used by table handles."""

    def __init__(
        self,
        domain_name: str,
        unit: Any,
        enqueue_fn: Callable[..., Awaitable[Any]],
        assert_readable_fn: Callable[[], None],
        emit_changed_fn: Callable[[dict[str, Any]], None],
    ) -> None:
        self.domain_name = domain_name
        self.unit = unit
        self.enqueue = enqueue_fn
        self.assert_readable = assert_readable_fn
        self.emit_changed = emit_changed_fn


class KvTable:
    """Read and write handle for one declared table."""

    def __init__(self, host: TableHost, table_name: str, records: dict[str, Any]) -> None:
        self._host = host
        self._table_name = table_name
        self._records = records

    def get(self, key: str) -> Any:
        self._host.assert_readable()
        return self._records.get(key)

    def entries(self) -> list[tuple[str, Any]]:
        self._host.assert_readable()
        return list(self._records.items())

    def keys(self) -> list[str]:
        self._host.assert_readable()
        return list(self._records.keys())

    @property
    def size(self) -> int:
        self._host.assert_readable()
        return len(self._records)

    async def put(self, key: str, value: Any) -> None:
        self._host.assert_readable()
        await self._host.enqueue(self._put, key, value)

    async def _put(self, key: str, value: Any) -> None:
        await self._host.unit.put_record(self._table_name, key, value)
        self._records[key] = value
        self._host.emit_changed(
            {
                "domain": self._host.domain_name,
                "table": self._table_name,
                "key": key,
                "operation": "put",
                "value": value,
            }
        )

    async def delete(self, key: str) -> bool:
        self._host.assert_readable()
        return cast(bool, await self._host.enqueue(self._delete, key))

    async def _delete(self, key: str) -> bool:
        if key not in self._records:
            return False
        await self._host.unit.delete_record(self._table_name, key)
        del self._records[key]
        self._host.emit_changed(
            {
                "domain": self._host.domain_name,
                "table": self._table_name,
                "key": key,
                "operation": "deleted",
            }
        )
        return True

    async def delete_many(self, keys: list[str]) -> int:
        self._host.assert_readable()
        return cast(int, await self._host.enqueue(self._delete_many, keys))

    async def _delete_many(self, keys: list[str]) -> int:
        present = list(dict.fromkeys(key for key in keys if key in self._records))
        if not present:
            return 0
        await self._host.unit.delete_records(self._table_name, present)
        for key in present:
            del self._records[key]
            self._host.emit_changed(
                {
                    "domain": self._host.domain_name,
                    "table": self._table_name,
                    "key": key,
                    "operation": "deleted",
                }
            )
        return len(present)

    async def update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        self._host.assert_readable()
        return await self._host.enqueue(self._update, key, fn)

    async def _update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        if key not in self._records:
            raise DomainError(
                "missing-key",
                f"domain '{self._host.domain_name}' table '{self._table_name}' has no record '{key}' to update",
            )
        next_value = fn(self._records[key])
        if inspect.isawaitable(next_value):
            next_value = await next_value
        await self._host.unit.put_record(self._table_name, key, next_value)
        self._records[key] = next_value
        self._host.emit_changed(
            {
                "domain": self._host.domain_name,
                "table": self._table_name,
                "key": key,
                "operation": "put",
                "value": next_value,
            }
        )
        return next_value


class DomainImpl:
    """Opened domain backed by one KV unit."""

    def __init__(
        self,
        ctx: Any,
        spec: DomainSpec,
        unit: Any,
        records: dict[str, dict[str, Any]],
        global_value: Any,
        on_closed: Callable[[], None],
    ) -> None:
        self.name = spec.name
        self.ctx = ctx
        self.spec = spec
        self.unit = unit
        self._records = records
        self._global_value = global_value
        self._on_closed = on_closed
        self.tables: dict[str, KvTable] = {}
        self._chain: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._chain.set_result(None)
        self._disposing = False
        self._failure: BaseException | None = None
        self._closed = False
        self._disposal: asyncio.Future[None] | None = None

        host = TableHost(
            domain_name=spec.name,
            unit=unit,
            enqueue_fn=self._enqueue,
            assert_readable_fn=self._assert_readable,
            emit_changed_fn=self._emit_changed,
        )
        for table_name, records_map in records.items():
            self.tables[table_name] = KvTable(host, table_name, records_map)
        self._global_handle: DomainGlobal | None = (
            DomainGlobal(self._global_get, self._global_set)
            if spec.global_spec is not None
            else None
        )

    @property
    def global_handle(self) -> DomainGlobal | None:
        return self._global_handle

    def table(self, name: str) -> KvTable:
        if name not in self.tables:
            raise ValueError(f"domain '{self.name}' declares no table '{name}'")
        return self.tables[name]

    async def close(self) -> None:
        if self._disposal is not None:
            await self._disposal
            return
        self._disposing = True
        self._disposal = asyncio.get_running_loop().create_future()
        try:
            await asyncio.shield(self._chain)
            await self.unit.close()
            self._closed = True
            self._on_closed()
            self._disposal.set_result(None)
        except BaseException as exc:
            self._disposal.set_exception(exc)
            raise

    def _global_get(self) -> Any:
        self._assert_readable()
        return self._global_value

    async def _global_set(self, value: Any) -> None:
        await self._enqueue(self._global_set_job, value)

    async def _global_set_job(self, value: Any) -> None:
        await self.unit.set_global(value)
        self._global_value = value
        self._emit_changed(
            {"domain": self.name, "table": "", "key": "", "operation": "put", "value": value}
        )

    async def _enqueue(self, job: Callable[..., Any] | Coroutine[Any, Any, Any], *args: Any) -> Any:
        if self._disposing:
            raise DomainError("closed", f"domain '{self.name}' is closed")
        previous = self._chain
        current: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        result_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def consume_exception(future: asyncio.Future[Any]) -> None:
            if not future.cancelled():
                future.exception()

        result_future.add_done_callback(consume_exception)

        async def runner() -> None:
            try:
                await previous
                if self._failure is not None:
                    raise self._failure
                result = job(*args) if callable(job) else job
                if inspect.isawaitable(result):
                    result = await result
                if not result_future.done():
                    result_future.set_result(result)
            except BaseException as exc:
                if isinstance(exc, StorageError) and exc.code == "unknown-state":
                    self._failure = exc
                if not result_future.done():
                    result_future.set_exception(exc)
            finally:
                if not current.done():
                    current.set_result(None)

        asyncio.create_task(runner())
        self._chain = current
        return await asyncio.shield(result_future)

    def _assert_readable(self) -> None:
        if self._closed or self._disposing:
            raise DomainError("closed", f"domain '{self.name}' is closed")
        if self._failure is not None:
            raise self._failure

    def _emit_changed(self, change: dict[str, Any]) -> None:
        try:
            if self.ctx is not None and hasattr(self.ctx, "emit"):
                self.ctx.emit("domain/changed", change)
        except Exception as exc:
            logger.warning("domain '%s': domain/changed listener failed: %s", self.name, exc)


class DomainFacility:
    """Open and track configured storage domains."""

    def __init__(self, ctx: Any, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.config = config
        self._domains: dict[str, DomainImpl] = {}
        self._reserved: set[str] = set()

    async def open(self, spec: DomainSpec) -> DomainImpl:
        if spec.name in self._reserved:
            raise DomainError("already-open", f"domain '{spec.name}' is already open")
        self._reserved.add(spec.name)
        try:
            routes = self.config.get("routes", {})
            backend_name = routes.get(spec.name, self.config.get("backend"))
            if backend_name is None:
                raise DomainError(
                    "backend-not-found", f"no backend configured for domain '{spec.name}'"
                )
            storage = getattr(self.ctx, "storage", None)
            if storage is None:
                raise DomainError("storage-not-available", "storage hub not available")
            backend = storage.backend.get(backend_name)
            facet = getattr(backend, "kv", None)
            if facet is None:
                raise DomainError(
                    "facet-unsupported",
                    f"backend '{backend_name}' routed for domain '{spec.name}' has no kv facet",
                )
            unit = await facet.open(descriptor_of(spec))
            try:
                snapshot = await unit.load_all()
                raw_tables = snapshot.get("tables", {})
                records = {
                    table_name: dict(raw_tables.get(table_name, {})) for table_name in spec.tables
                }
                persisted_global = snapshot.get("global")
                global_value = (
                    spec.global_spec.initial
                    if persisted_global is None and spec.global_spec is not None
                    else persisted_global
                )
                domain = DomainImpl(
                    ctx=self.ctx,
                    spec=spec,
                    unit=unit,
                    records=records,
                    global_value=global_value,
                    on_closed=lambda: self._on_closed(spec.name),
                )
                self._domains[spec.name] = domain
                return domain
            except BaseException:
                await unit.close()
                raise
        except BaseException:
            self._reserved.discard(spec.name)
            raise

    def _on_closed(self, name: str) -> None:
        self._domains.pop(name, None)
        self._reserved.discard(name)

    def get(self, name: str) -> DomainImpl | None:
        return self._domains.get(name)

    async def close_all(self) -> None:
        await asyncio.gather(*(domain.close() for domain in list(self._domains.values())))


def define_domain(spec: DomainSpec) -> DomainSpec:
    if not UNIT_NAME_RE_COMPILED.fullmatch(spec.name):
        raise ValueError(f"domain name '{spec.name}' must match {UNIT_NAME_RE}")
    if not isinstance(spec.version, int) or isinstance(spec.version, bool) or spec.version < 0:
        raise ValueError(f"domain '{spec.name}' version must be a non-negative integer")
    for table_name in spec.tables:
        if not UNIT_NAME_RE_COMPILED.fullmatch(table_name):
            raise ValueError(
                f"domain '{spec.name}' table name '{table_name}' must match {UNIT_NAME_RE}"
            )
    return spec


def domain_table(value_schema: Any, key_type: str = "str") -> DomainTableSpec:
    return DomainTableSpec(value_schema=value_schema, key_type=key_type)


def descriptor_of(spec: DomainSpec) -> KvUnitDescriptor:
    return KvUnitDescriptor(
        name=spec.name,
        version=spec.version,
        tables=list(spec.tables),
        has_global=spec.global_spec is not None,
        layout=spec.layout,
        compatible_versions=spec.compatible_versions,
    )


__all__ = [
    "DomainGlobalSpec",
    "DomainTableSpec",
    "DomainSpec",
    "DomainGlobal",
    "KvTable",
    "DomainError",
    "DomainChanged",
    "DomainChangedBase",
    "DomainChangedPut",
    "DomainChangedDeleted",
    "TableHost",
    "DomainImpl",
    "DomainFacility",
    "define_domain",
    "domain_table",
    "descriptor_of",
]
