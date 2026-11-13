from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest

from app.deepspace.services import conversation_execution_limiter as limiter
from app.deepspace.services.conversation_execution_limiter import (
    SharedExecutionLimitError,
    conversation_execution_gate,
    shared_conversation_execution_slot,
)

pytestmark = pytest.mark.unit_no_db


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.write_queues: dict[str, dict[str, int]] = {}
        self.write_expiries: dict[str, dict[str, float]] = {}

    def set(self, key: str, value: object, *, nx: bool = False, **_kwargs: object) -> bool:
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def incr(self, key: str) -> int:
        with self.lock:
            self.values[key] = int(self.values.get(key, 0)) + 1
            return int(self.values[key])

    def decr(self, key: str) -> int:
        with self.lock:
            self.values[key] = int(self.values.get(key, 0)) - 1
            return int(self.values[key])

    def expire(self, _key: str, _seconds: int) -> bool:
        return True

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        keys = args[:numkeys]
        values = args[numkeys:]
        with self.lock:
            if script == limiter._ENQUEUE_WRITE:
                queue_key, expiry_key, sequence_key = keys
                token, expires_at, now, max_writers, _queue_ttl = values
                queue = self.write_queues.setdefault(queue_key, {})
                expiry = self.write_expiries.setdefault(expiry_key, {})
                for stale in [key for key, value in expiry.items() if value <= float(now)]:
                    queue.pop(stale, None)
                    expiry.pop(stale, None)
                if str(token) in queue:
                    return queue[str(token)]
                if len(queue) >= int(max_writers):
                    return -1
                ticket = int(self.values.get(sequence_key, 0)) + 1
                self.values[sequence_key] = ticket
                queue[str(token)] = ticket
                expiry[str(token)] = float(expires_at)
                return ticket
            if script == limiter._REMOVE_WRITE_TICKET:
                queue_key, expiry_key = keys
                (token,) = values
                self.write_queues.setdefault(queue_key, {}).pop(str(token), None)
                self.write_expiries.setdefault(expiry_key, {}).pop(str(token), None)
                return 1
            if script == limiter._ACQUIRE_READ:
                reads_key, write_key, queue_key, expiry_key = keys
                limit, _ttl, now = values
                queue = self.write_queues.setdefault(queue_key, {})
                expiry = self.write_expiries.setdefault(expiry_key, {})
                for stale in [key for key, value in expiry.items() if value <= float(now)]:
                    queue.pop(stale, None)
                    expiry.pop(stale, None)
                if write_key in self.values or queue:
                    return 0
                count = int(self.values.get(reads_key, 0)) + 1
                if count > int(limit):
                    return 0
                self.values[reads_key] = count
                return 1
            if script == limiter._ACQUIRE_WRITE:
                write_key, reads_key, queue_key, expiry_key = keys
                token, _lease_ms, now = values
                queue = self.write_queues.setdefault(queue_key, {})
                expiry = self.write_expiries.setdefault(expiry_key, {})
                for stale in [key for key, value in expiry.items() if value <= float(now)]:
                    queue.pop(stale, None)
                    expiry.pop(stale, None)
                first = min(queue, key=queue.get) if queue else None
                if (
                    write_key in self.values
                    or int(self.values.get(reads_key, 0)) > 0
                    or first != token
                ):
                    return 0
                self.values[write_key] = token
                queue.pop(str(token), None)
                expiry.pop(str(token), None)
                return 1
            if script == limiter._COMPARE_AND_DELETE:
                (write_key,) = keys
                (token,) = values
                if self.values.get(write_key) == token:
                    del self.values[write_key]
                    return 1
                return 0
            if "redis.call('decr', KEYS[1])" in script:
                (reads_key,) = keys
                count = int(self.values.get(reads_key, 0))
                if count > 0:
                    self.values[reads_key] = count - 1
                    return count - 1
                return 0
            raise AssertionError(f"Unexpected Redis script: {script}")


def _scope() -> dict[str, uuid.UUID]:
    return {
        "tenant_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
    }


@pytest.mark.asyncio
async def test_coordination_wait_does_not_acquire_a_shared_execution_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_shared_slot(**_kwargs: Any):
        pytest.fail("a child wait must not occupy a shared execution slot")

    monkeypatch.setattr(limiter, "shared_conversation_execution_slot", unexpected_shared_slot)
    scope = _scope()

    async with conversation_execution_gate(
        asyncio.Lock(),
        **scope,
        is_read=True,
        max_parallel_reads=2,
        timeout_seconds=1,
        shared=False,
    ) as capacity_error:
        assert capacity_error is None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    client = _FakeRedis()
    monkeypatch.setattr(limiter, "_limiter_redis", lambda: client)
    return client


@pytest.mark.asyncio
async def test_read_permits_are_shared_across_parallel_agent_tasks(fake_redis: _FakeRedis) -> None:
    del fake_redis
    scope = _scope()
    release = asyncio.Event()
    entered = 0

    async def hold_read() -> None:
        nonlocal entered
        async with shared_conversation_execution_slot(
            **scope,
            is_read=True,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            entered += 1
            await release.wait()

    first = asyncio.create_task(hold_read())
    second = asyncio.create_task(hold_read())
    for _ in range(100):
        if entered == 2:
            break
        await asyncio.sleep(0.01)
    assert entered == 2

    with pytest.raises(SharedExecutionLimitError) as caught:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=True,
            max_parallel_reads=2,
            timeout_seconds=0.05,
        ):
            pytest.fail("the shared read budget must hold")
    assert caught.value.code == "SHARED_EXECUTION_BUSY"

    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_write_leases_serialize_agents_and_release_after_completion(
    fake_redis: _FakeRedis,
) -> None:
    del fake_redis
    scope = _scope()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_writer() -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=False,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            first_entered.set()
            await release_first.wait()

    async def second_writer() -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=False,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            second_entered.set()

    first = asyncio.create_task(first_writer())
    second = asyncio.create_task(second_writer())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_shared_writes_follow_their_conversation_queue_order(fake_redis: _FakeRedis) -> None:
    del fake_redis
    scope = _scope()
    order: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def writer(label: str, *, hold: bool = False) -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=False,
            max_parallel_reads=2,
            timeout_seconds=2,
        ):
            order.append(label)
            if hold:
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(writer("first", hold=True))
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(writer("second"))
    third = asyncio.create_task(writer("third"))
    await asyncio.sleep(0.05)
    assert order == ["first"]
    release_first.set()
    await asyncio.gather(first, second, third)

    assert order == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_shared_write_waits_for_reads_and_blocks_new_reads(fake_redis: _FakeRedis) -> None:
    del fake_redis
    scope = _scope()
    read_entered = asyncio.Event()
    release_read = asyncio.Event()
    write_entered = asyncio.Event()
    release_write = asyncio.Event()
    read_after_write_entered = asyncio.Event()

    async def hold_read() -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=True,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            read_entered.set()
            await release_read.wait()

    async def hold_write() -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=False,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            write_entered.set()
            await release_write.wait()

    async def read_while_writer_holds_lease() -> None:
        async with shared_conversation_execution_slot(
            **scope,
            is_read=True,
            max_parallel_reads=2,
            timeout_seconds=1,
        ):
            read_after_write_entered.set()

    read_task = asyncio.create_task(hold_read())
    await asyncio.wait_for(read_entered.wait(), timeout=1)
    write_task = asyncio.create_task(hold_write())
    await asyncio.sleep(0.05)
    assert not write_entered.is_set()

    release_read.set()
    await asyncio.wait_for(write_entered.wait(), timeout=1)
    read_task_2 = asyncio.create_task(read_while_writer_holds_lease())
    await asyncio.sleep(0.05)
    assert not read_after_write_entered.is_set()

    release_write.set()
    await asyncio.gather(read_task, write_task, read_task_2)
    assert read_after_write_entered.is_set()
