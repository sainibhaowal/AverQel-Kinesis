"""Redis-backed conversation-wide limits shared by root and child agents."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import redis

from app.core.config import get_settings

# The configured per-tool deadline is capped at one hour. Leave a short grace
# period so a live command cannot outlast its permit and overlap a new writer.
_LEASE_SECONDS = 4_200
_POLL_SECONDS = 0.05
_MAX_QUEUED_WRITERS = 64
_COMPARE_AND_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('del', KEYS[1])
  return 1
end
return 0
"""
_ACQUIRE_READ = """
local expiries = redis.call('hgetall', KEYS[4])
for i = 1, #expiries, 2 do
  if tonumber(expiries[i + 1]) <= tonumber(ARGV[3]) then
    redis.call('zrem', KEYS[3], expiries[i])
    redis.call('hdel', KEYS[4], expiries[i])
  end
end
if redis.call('exists', KEYS[2]) == 1 or redis.call('zcard', KEYS[3]) > 0 then
  return 0
end
local count = redis.call('incr', KEYS[1])
if count <= tonumber(ARGV[1]) then
  redis.call('expire', KEYS[1], ARGV[2])
  return 1
end
redis.call('decr', KEYS[1])
return 0
"""
_ACQUIRE_WRITE = """
local expiries = redis.call('hgetall', KEYS[4])
for i = 1, #expiries, 2 do
  if tonumber(expiries[i + 1]) <= tonumber(ARGV[3]) then
    redis.call('zrem', KEYS[3], expiries[i])
    redis.call('hdel', KEYS[4], expiries[i])
  end
end
if redis.call('exists', KEYS[1]) == 1 then
  return 0
end
if tonumber(redis.call('get', KEYS[2]) or '0') > 0 then
  return 0
end
local first = redis.call('zrange', KEYS[3], 0, 0)[1]
if first ~= ARGV[1] then
  return 0
end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  redis.call('zrem', KEYS[3], ARGV[1])
  redis.call('hdel', KEYS[4], ARGV[1])
  return 1
end
return 0
"""
_ENQUEUE_WRITE = """
local expiries = redis.call('hgetall', KEYS[2])
for i = 1, #expiries, 2 do
  if tonumber(expiries[i + 1]) <= tonumber(ARGV[3]) then
    redis.call('zrem', KEYS[1], expiries[i])
    redis.call('hdel', KEYS[2], expiries[i])
  end
end
local existing = redis.call('zscore', KEYS[1], ARGV[1])
if existing then
  return tonumber(existing)
end
if redis.call('zcard', KEYS[1]) >= tonumber(ARGV[4]) then
  return -1
end
local ticket = redis.call('incr', KEYS[3])
redis.call('zadd', KEYS[1], ticket, ARGV[1])
redis.call('hset', KEYS[2], ARGV[1], ARGV[2])
redis.call('expire', KEYS[1], ARGV[5])
redis.call('expire', KEYS[2], ARGV[5])
redis.call('expire', KEYS[3], ARGV[5])
return ticket
"""
_REMOVE_WRITE_TICKET = """
redis.call('zrem', KEYS[1], ARGV[1])
redis.call('hdel', KEYS[2], ARGV[1])
return 1
"""


@lru_cache(maxsize=2)
def _redis_client(redis_url: str) -> redis.Redis:
    """Use bounded network timeouts so an unavailable broker fails promptly."""
    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


def _limiter_redis() -> redis.Redis:
    return _redis_client(get_settings().redis_url)


class SharedExecutionLimitError(RuntimeError):
    """A conversation-wide execution slot could not be acquired safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _key(tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    return f"deepspace:execution:{tenant_id}:{user_id}:{conversation_id}"


def _acquire_read_slot(key: str, *, limit: int) -> bool:
    client = _limiter_redis()
    counter_key = f"{key}:reads"
    # The Lua script checks the writer and increments the readers atomically,
    # so a read cannot slip between a writer's check and lease acquisition.
    return bool(
        client.eval(
            _ACQUIRE_READ,
            4,
            counter_key,
            f"{key}:write",
            f"{key}:write_queue",
            f"{key}:write_expiry",
            max(1, min(32, limit)),
            _LEASE_SECONDS,
            time.time(),
        )
    )


def _release_read_slot(key: str) -> None:
    client = _limiter_redis()
    client.eval(
        "local n = tonumber(redis.call('get', KEYS[1]) or '0'); "
        "if n > 0 then return redis.call('decr', KEYS[1]) end; return 0",
        1,
        f"{key}:reads",
    )


def _enqueue_write_slot(key: str, token: str) -> None:
    client = _limiter_redis()
    ticket = int(
        client.eval(
            _ENQUEUE_WRITE,
            3,
            f"{key}:write_queue",
            f"{key}:write_expiry",
            f"{key}:write_sequence",
            token,
            time.time() + _LEASE_SECONDS,
            time.time(),
            _MAX_QUEUED_WRITERS,
            _LEASE_SECONDS,
        )
    )
    if ticket < 0:
        raise SharedExecutionLimitError(
            "SHARED_EXECUTION_BUSY",
            "The conversation already has the maximum number of queued writes.",
        )


def _remove_write_ticket(key: str, token: str) -> None:
    _limiter_redis().eval(
        _REMOVE_WRITE_TICKET,
        2,
        f"{key}:write_queue",
        f"{key}:write_expiry",
        token,
    )


def _acquire_write_slot(key: str, token: str) -> bool:
    client = _limiter_redis()
    return bool(
        client.eval(
            _ACQUIRE_WRITE,
            4,
            f"{key}:write",
            f"{key}:reads",
            f"{key}:write_queue",
            f"{key}:write_expiry",
            token,
            _LEASE_SECONDS * 1_000,
            time.time(),
        )
    )


def _release_write_slot(key: str, token: str) -> None:
    client = _limiter_redis()
    client.eval(_COMPARE_AND_DELETE, 1, f"{key}:write", token)


@asynccontextmanager
async def shared_conversation_execution_slot(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    is_read: bool,
    max_parallel_reads: int,
    timeout_seconds: float,
) -> AsyncIterator[None]:
    """Acquire a shared read permit or serialized write lease for one conversation.

    Root and child turns all call this boundary. Redis makes the read budget and
    write exclusion visible across API and Celery worker processes. The caller
    still applies its local per-round semaphore and write-ordering barrier.
    """
    key = _key(tenant_id, user_id, conversation_id)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    acquire = _acquire_read_slot if is_read else _acquire_write_slot
    release = _release_read_slot if is_read else _release_write_slot
    acquired = False
    try:
        if not is_read:
            try:
                await asyncio.to_thread(_enqueue_write_slot, key, token)
            except SharedExecutionLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SharedExecutionLimitError(
                    "SHARED_EXECUTION_UNAVAILABLE",
                    "DeepSpace could not queue the shared conversation write safely, so the tool was not run.",
                ) from exc
        while True:
            try:
                acquired = await asyncio.to_thread(
                    acquire,
                    key,
                    **(
                        {"limit": max(1, min(32, max_parallel_reads))}
                        if is_read
                        else {"token": token}
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - never bypass a failed shared gate
                raise SharedExecutionLimitError(
                    "SHARED_EXECUTION_UNAVAILABLE",
                    "DeepSpace could not verify the shared conversation execution limit, so the tool was not run.",
                ) from exc
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SharedExecutionLimitError(
                    "SHARED_EXECUTION_BUSY",
                    "The conversation reached its shared execution limit. Retry after active work finishes.",
                )
            await asyncio.sleep(min(_POLL_SECONDS, remaining))
        yield
    finally:
        try:
            if not acquired and not is_read:
                await asyncio.to_thread(_remove_write_ticket, key, token)
            elif acquired and is_read:
                await asyncio.to_thread(release, key)
            elif acquired:
                await asyncio.to_thread(release, key, token)
        except Exception:  # noqa: BLE001 - the lease expires if release fails
            pass


@asynccontextmanager
async def conversation_execution_gate(
    local_gate: Any,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    is_read: bool,
    max_parallel_reads: int,
    timeout_seconds: float,
    shared: bool = True,
) -> AsyncIterator[SharedExecutionLimitError | None]:
    """Combine the existing round gate with the shared conversation lease.

    A bounded wait for another agent is coordination, not tool execution. It
    must not reserve a slot that the child needs in order to finish.
    """
    async with local_gate:
        if not shared:
            yield None
            return
        lease = shared_conversation_execution_slot(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            is_read=is_read,
            max_parallel_reads=max_parallel_reads,
            timeout_seconds=timeout_seconds,
        )
        try:
            await lease.__aenter__()
        except SharedExecutionLimitError as exc:
            yield exc
            return
        try:
            yield None
        finally:
            await lease.__aexit__(None, None, None)


__all__ = [
    "SharedExecutionLimitError",
    "conversation_execution_gate",
    "shared_conversation_execution_slot",
]
