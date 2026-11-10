from __future__ import annotations

import time
import uuid

import fakeredis
import pytest

from app.platform import embedded_redis

pytestmark = pytest.mark.unit_no_db


_READ_SCRIPT = """
local expiries = redis.call('hgetall', KEYS[4])
local count = redis.call('incr', KEYS[1])
if redis.call('zcard', KEYS[3]) > 0 then return 0 end
return count
"""
_RELEASE_READ_SCRIPT = """
local n = tonumber(redis.call('get', KEYS[1]) or '0')
if n > 0 then return redis.call('decr', KEYS[1]) end
return 0
"""
_ENQUEUE_WRITE_SCRIPT = """
local expiries = redis.call('hgetall', KEYS[2])
if redis.call('zscore', KEYS[1], ARGV[1]) then return 1 end
if redis.call('zcard', KEYS[1]) >= tonumber(ARGV[4]) then return -1 end
local ticket = redis.call('incr', KEYS[3])
redis.call('zadd', KEYS[1], ticket, ARGV[1])
return ticket
"""
_ACQUIRE_WRITE_SCRIPT = """
local expiries = redis.call('hgetall', KEYS[4])
local first = redis.call('zrange', KEYS[3], 0, 0)[1]
if first ~= ARGV[1] then return 0 end
return redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) and 1 or 0
"""
_RELEASE_WRITE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('del', KEYS[1])
  return 1
end
return 0
"""


def test_embedded_redis_runs_the_shared_conversation_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedded_redis, "_shared_server", fakeredis.FakeServer())
    client = embedded_redis.get_fake_redis_sync()
    prefix = f"deepspace:execution:{uuid.uuid4()}"
    reads_key = f"{prefix}:reads"
    write_key = f"{prefix}:write"
    queue_key = f"{prefix}:write_queue"
    expiry_key = f"{prefix}:write_expiry"
    sequence_key = f"{prefix}:write_sequence"

    assert (
        client.eval(
            _READ_SCRIPT,
            4,
            reads_key,
            write_key,
            queue_key,
            expiry_key,
            1,
            10,
            time.time(),
        )
        == 1
    )
    assert (
        client.eval(
            _READ_SCRIPT,
            4,
            reads_key,
            write_key,
            queue_key,
            expiry_key,
            1,
            10,
            time.time(),
        )
        == 0
    )
    assert client.eval(_RELEASE_READ_SCRIPT, 1, reads_key) == 0
    assert (
        client.eval(
            _READ_SCRIPT,
            4,
            reads_key,
            write_key,
            queue_key,
            expiry_key,
            1,
            10,
            time.time(),
        )
        == 1
    )

    token = "writer-token"
    assert (
        client.eval(
            _ENQUEUE_WRITE_SCRIPT,
            3,
            queue_key,
            expiry_key,
            sequence_key,
            token,
            time.time() + 10,
            time.time(),
            64,
            10,
        )
        == 1
    )
    client.eval(_RELEASE_READ_SCRIPT, 1, reads_key)
    assert (
        client.eval(
            _ACQUIRE_WRITE_SCRIPT,
            4,
            write_key,
            reads_key,
            queue_key,
            expiry_key,
            token,
            10_000,
            time.time(),
        )
        == 1
    )
    assert client.eval(_RELEASE_WRITE_SCRIPT, 1, write_key, token) == 1
