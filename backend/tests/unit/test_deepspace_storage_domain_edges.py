from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.storage import Storage, StorageError
from app.deepspace.services.storage_domain import (
    DomainError,
    DomainFacility,
    DomainGlobalSpec,
    DomainSpec,
    define_domain,
    domain_table,
)

pytestmark = pytest.mark.unit_no_db


class FakeUnit:
    def __init__(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        load_error: Exception | None = None,
        open_error: Exception | None = None,
        write_error: Exception | None = None,
        close_error: Exception | None = None,
        write_started: asyncio.Event | None = None,
        write_release: asyncio.Event | None = None,
    ) -> None:
        self.snapshot = snapshot or {"tables": {}, "global": None}
        self.load_error = load_error
        self.open_error = open_error
        self.write_error = write_error
        self.close_error = close_error
        self.write_started = write_started
        self.write_release = write_release
        self.records: dict[str, dict[str, Any]] = {}
        self.global_value: Any = None
        self.closed = False
        self.write_calls: list[tuple[str, str, Any]] = []

    async def load_all(self) -> dict[str, Any]:
        if self.load_error is not None:
            raise self.load_error
        return self.snapshot

    async def put_record(self, table: str, key: str, value: Any) -> None:
        self.write_calls.append((table, key, value))
        if self.write_started is not None:
            self.write_started.set()
        if self.write_release is not None:
            await self.write_release.wait()
        if self.write_error is not None:
            raise self.write_error
        self.records.setdefault(table, {})[key] = value

    async def delete_record(self, table: str, key: str) -> None:
        self.records.setdefault(table, {}).pop(key, None)

    async def delete_records(self, table: str, keys: list[str]) -> None:
        records = self.records.setdefault(table, {})
        for key in keys:
            records.pop(key, None)

    async def set_global(self, value: Any) -> None:
        self.global_value = value

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _spec(*, global_value: Any = None) -> DomainSpec:
    return define_domain(
        DomainSpec(
            name="test_domain",
            version=1,
            tables={"records": domain_table("json")},
            global_spec=(
                DomainGlobalSpec(schema="json", initial=global_value)
                if global_value is not None
                else None
            ),
        )
    )


def _facility(
    unit: FakeUnit | None = None,
    *,
    with_storage: bool = True,
    backend_has_kv: bool = True,
    backend_name: str | None = "memory",
) -> tuple[DomainFacility, FakeUnit, list[tuple[str, dict[str, Any]]]]:
    selected_unit = unit or FakeUnit()
    events: list[tuple[str, dict[str, Any]]] = []
    storage = Storage()

    async def open_unit(_descriptor: Any) -> FakeUnit:
        if selected_unit.open_error is not None:
            raise selected_unit.open_error
        return selected_unit

    backend = SimpleNamespace(kv=SimpleNamespace(open=open_unit) if backend_has_kv else None)
    storage.backend.register("memory", backend)
    context = SimpleNamespace(
        storage=storage if with_storage else None,
        emit=lambda event, payload: events.append((event, payload)),
    )
    facility = DomainFacility(context, {"backend": backend_name})
    return facility, selected_unit, events


def test_domain_table_reads_writes_global_and_close() -> None:
    async def exercise() -> None:
        facility, unit, events = _facility()
        domain = await facility.open(_spec(global_value={"ready": False}))
        table = domain.table("records")

        assert domain.global_handle.get() == {"ready": False}
        assert table.get("missing") is None
        assert table.entries() == []
        assert table.keys() == []
        assert table.size == 0
        with pytest.raises(ValueError, match="declares no table"):
            domain.table("missing")

        assert await table.delete("missing") is False
        assert await table.delete_many([]) == 0
        await table.put("one", {"value": 1})
        assert table.get("one") == {"value": 1}
        assert table.entries() == [("one", {"value": 1})]
        assert table.keys() == ["one"]
        assert table.size == 1
        updated = await table.update("one", lambda row: {"value": row["value"] + 1})
        assert updated == {"value": 2}
        with pytest.raises(DomainError, match="has no record"):
            await table.update("missing", lambda row: row)
        assert await table.delete_many(["one", "missing", "one"]) == 1
        assert await table.delete("one") is False

        await domain.global_handle.set({"ready": True})
        assert domain.global_handle.get() == {"ready": True}
        assert unit.global_value == {"ready": True}
        assert any(event == "domain/changed" for event, _payload in events)
        assert await table.delete_many(["not-present"]) == 0

        await domain.close()
        await domain.close()
        assert unit.closed is True
        assert facility.get("test_domain") is None
        with pytest.raises(DomainError, match="is closed"):
            table.get("one")

    asyncio.run(exercise())


def test_domain_write_queue_serializes_operations_and_close() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        facility, unit, _events = _facility(FakeUnit(write_started=started, write_release=release))
        domain = await facility.open(_spec())
        table = domain.table("records")

        first_write = asyncio.create_task(table.put("one", 1))
        await started.wait()
        second_write = asyncio.create_task(table.put("two", 2))
        closing = asyncio.create_task(domain.close())
        await asyncio.sleep(0)
        assert unit.write_calls == [("records", "one", 1)]
        with pytest.raises(DomainError, match="is closed"):
            await table.put("three", 3)
        release.set()
        await asyncio.gather(first_write, second_write, closing)

        assert unit.records["records"] == {"one": 1, "two": 2}
        assert unit.closed is True

    asyncio.run(exercise())


def test_domain_queue_accepts_callable_coroutine_and_coroutine_object() -> None:
    async def exercise() -> None:
        facility, _unit, _events = _facility()
        domain = await facility.open(_spec())
        assert await domain._enqueue(lambda: "callable") == "callable"
        assert await domain._enqueue(lambda: asyncio.sleep(0, result="returned coroutine")) == (
            "returned coroutine"
        )
        assert await domain._enqueue(asyncio.sleep(0, result="coroutine object")) == (
            "coroutine object"
        )
        await domain.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("fails", [False, True])
def test_cancelling_a_waiter_does_not_break_the_write_queue(fails: bool) -> None:
    async def exercise() -> None:
        facility, _unit, _events = _facility()
        domain = await facility.open(_spec())
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> str:
            started.set()
            await release.wait()
            if fails:
                raise RuntimeError("late operation failure")
            return "finished"

        waiting = asyncio.create_task(domain._enqueue(work))
        await started.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        await domain._chain
        await domain.close()

    asyncio.run(exercise())


def test_unknown_storage_state_poisons_domain_and_write_failure_is_reported() -> None:
    async def exercise() -> None:
        failure = StorageError("unknown-state", "storage state is incompatible")
        facility, _unit, _events = _facility(FakeUnit(write_error=failure))
        domain = await facility.open(_spec())
        table = domain.table("records")

        with pytest.raises(StorageError, match="incompatible"):
            await table.put("one", 1)
        with pytest.raises(StorageError, match="incompatible"):
            await table.put("two", 2)
        with pytest.raises(StorageError, match="incompatible"):
            await domain._enqueue(lambda: None)
        with pytest.raises(StorageError, match="incompatible"):
            table.get("one")
        await domain.close()

    asyncio.run(exercise())


def test_domain_listener_failure_is_logged_and_does_not_fail_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        facility, _unit, _events = _facility()
        domain = await facility.open(_spec())

        def fail_listener(_event: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("listener failed")

        domain.ctx.emit = fail_listener
        await domain.table("records").put("one", 1)
        assert "domain/changed listener failed" in caplog.text
        await domain.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("context", "config", "backend_has_kv", "expected"),
    [
        (SimpleNamespace(), {"backend": "memory"}, True, "storage hub not available"),
        (None, {"backend": None}, True, "no backend configured"),
        (None, {"backend": "memory"}, False, "has no kv facet"),
    ],
)
def test_domain_open_releases_reservation_after_configuration_errors(
    context: Any,
    config: dict[str, Any],
    backend_has_kv: bool,
    expected: str,
) -> None:
    async def exercise() -> None:
        if context is None:
            storage = Storage()
            storage.backend.register(
                "memory",
                SimpleNamespace(
                    kv=SimpleNamespace(open=lambda _descriptor: None) if backend_has_kv else None
                ),
            )
            context_value = SimpleNamespace(storage=storage)
        elif expected == "storage hub not available":
            context_value = context
        else:
            context_value = SimpleNamespace(storage=None)

        facility = DomainFacility(context_value, config)
        with pytest.raises(DomainError, match=expected):
            await facility.open(_spec())
        assert "test_domain" not in facility._reserved

    asyncio.run(exercise())


def test_domain_open_releases_reservation_and_closes_unit_after_load_error() -> None:
    async def exercise() -> None:
        unit = FakeUnit(load_error=RuntimeError("load failed"))
        facility, unit, _events = _facility(unit)
        with pytest.raises(RuntimeError, match="load failed"):
            await facility.open(_spec())
        assert unit.closed is True
        assert "test_domain" not in facility._reserved

    asyncio.run(exercise())


def test_domain_open_reports_duplicate_and_preserves_persisted_global() -> None:
    async def exercise() -> None:
        unit = FakeUnit(snapshot={"tables": {"records": {"existing": 3}}, "global": 9})
        facility, _unit, _events = _facility(unit)
        domain = await facility.open(_spec(global_value=0))
        assert domain.global_handle.get() == 9
        assert domain.table("records").get("existing") == 3
        with pytest.raises(DomainError, match="already open"):
            await facility.open(_spec(global_value=0))
        await facility.close_all()
        assert facility.get("test_domain") is None

    asyncio.run(exercise())


def test_domain_open_clears_reservation_when_backend_open_fails() -> None:
    async def exercise() -> None:
        unit = FakeUnit(open_error=RuntimeError("open failed"))
        facility, _unit, _events = _facility(unit)
        with pytest.raises(RuntimeError, match="open failed"):
            await facility.open(_spec())
        assert "test_domain" not in facility._reserved

    asyncio.run(exercise())


def test_domain_close_caches_and_replays_backend_close_failure() -> None:
    async def exercise() -> None:
        facility, unit, _events = _facility(FakeUnit(close_error=RuntimeError("close failed")))
        domain = await facility.open(_spec())

        with pytest.raises(RuntimeError, match="close failed"):
            await domain.close()
        with pytest.raises(RuntimeError, match="close failed"):
            await domain.close()
        assert unit.closed is True
        assert facility.get("test_domain") is domain

    asyncio.run(exercise())


def test_domain_table_name_validation() -> None:
    with pytest.raises(ValueError, match="table name"):
        define_domain(
            DomainSpec(name="valid_domain", version=1, tables={"Invalid": domain_table("json")})
        )
