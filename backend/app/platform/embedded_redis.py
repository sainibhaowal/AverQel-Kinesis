from __future__ import annotations

import logging
from threading import RLock
from typing import Any

import fakeredis
import fakeredis.aioredis
import redis
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_shared_server: fakeredis.FakeServer | None = None
_limiter_eval_lock = RLock()


def get_fake_server() -> fakeredis.FakeServer:
    global _shared_server
    if _shared_server is None:
        _shared_server = fakeredis.FakeServer()
    return _shared_server


def _limiter_script_kind(script: str, *, numkeys: int, argument_count: int) -> str | None:
    """Identify the conversation-gate scripts before generic emulation rules.

    The embedded Redis compatibility layer normally handles a small set of
    legacy scripts.  Conversation execution uses hashes and sorted sets, so
    it needs explicit emulation when Lupa is not available.  Matching the
    script shape keeps this module independent from the DeepSpace service and
    avoids importing application code during platform initialization.
    """
    if "zscore" in script and "zadd" in script and "zcard" in script:
        return "enqueue_write"
    if "zrem" in script and "hdel" in script and numkeys == 2 and argument_count == 1:
        return "remove_write_ticket"
    if "hgetall" in script and "zrange" in script and "set" in script:
        return "acquire_write"
    if "hgetall" in script and "incr" in script and "zcard" in script:
        return "acquire_read"
    if "decr" in script and "get" in script and numkeys == 1 and argument_count == 0:
        return "release_read"
    if "get" in script and "del" in script and "ARGV[1]" in script:
        return "release_write"
    return None


def _purge_limiter_expiry(client: Any, queue_key: str, expiry_key: str, now: float) -> None:
    for token, expires_at in client.hgetall(expiry_key).items():
        if float(expires_at) <= now:
            client.zrem(queue_key, token)
            client.hdel(expiry_key, token)


def _execute_limiter_sync(client: Any, kind: str, keys: list[Any], argv: list[Any]) -> int:
    if kind == "enqueue_write":
        queue_key, expiry_key, sequence_key = keys
        token, expires_at, now, max_writers, queue_ttl = argv
        _purge_limiter_expiry(client, queue_key, expiry_key, float(now))
        existing = client.zscore(queue_key, token)
        if existing is not None:
            return int(existing)
        if client.zcard(queue_key) >= int(max_writers):
            return -1
        ticket = int(client.incr(sequence_key))
        client.zadd(queue_key, {token: ticket})
        client.hset(expiry_key, token, expires_at)
        client.expire(queue_key, int(queue_ttl))
        client.expire(expiry_key, int(queue_ttl))
        client.expire(sequence_key, int(queue_ttl))
        return ticket

    if kind == "remove_write_ticket":
        queue_key, expiry_key = keys
        (token,) = argv
        client.zrem(queue_key, token)
        client.hdel(expiry_key, token)
        return 1

    if kind == "acquire_read":
        reads_key, write_key, queue_key, expiry_key = keys
        limit, lease_ttl, now = argv
        _purge_limiter_expiry(client, queue_key, expiry_key, float(now))
        if client.exists(write_key) or client.zcard(queue_key) > 0:
            return 0
        count = int(client.incr(reads_key))
        if count <= int(limit):
            client.expire(reads_key, int(lease_ttl))
            return 1
        client.decr(reads_key)
        return 0

    if kind == "acquire_write":
        write_key, reads_key, queue_key, expiry_key = keys
        token, lease_ms, now = argv
        _purge_limiter_expiry(client, queue_key, expiry_key, float(now))
        if client.exists(write_key) or int(client.get(reads_key) or 0) > 0:
            return 0
        first = client.zrange(queue_key, 0, 0)
        if not first or first[0] != token:
            return 0
        if client.set(write_key, token, nx=True, px=int(lease_ms)):
            client.zrem(queue_key, token)
            client.hdel(expiry_key, token)
            return 1
        return 0

    if kind == "release_read":
        (reads_key,) = keys
        count = int(client.get(reads_key) or 0)
        if count > 0:
            return int(client.decr(reads_key))
        return 0

    if kind == "release_write":
        (write_key,) = keys
        (token,) = argv
        if client.get(write_key) == token:
            client.delete(write_key)
            return 1
        return 0

    raise AssertionError(f"Unexpected embedded limiter script kind: {kind}")


async def _execute_limiter_async(client: Any, kind: str, keys: list[Any], argv: list[Any]) -> int:
    async def purge_expiry(queue_key: str, expiry_key: str, now: float) -> None:
        for token, expires_at in (await client.hgetall(expiry_key)).items():
            if float(expires_at) <= now:
                await client.zrem(queue_key, token)
                await client.hdel(expiry_key, token)

    if kind == "enqueue_write":
        queue_key, expiry_key, sequence_key = keys
        token, expires_at, now, max_writers, queue_ttl = argv
        await purge_expiry(queue_key, expiry_key, float(now))
        existing = await client.zscore(queue_key, token)
        if existing is not None:
            return int(existing)
        if await client.zcard(queue_key) >= int(max_writers):
            return -1
        ticket = int(await client.incr(sequence_key))
        await client.zadd(queue_key, {token: ticket})
        await client.hset(expiry_key, token, expires_at)
        await client.expire(queue_key, int(queue_ttl))
        await client.expire(expiry_key, int(queue_ttl))
        await client.expire(sequence_key, int(queue_ttl))
        return ticket

    if kind == "remove_write_ticket":
        queue_key, expiry_key = keys
        (token,) = argv
        await client.zrem(queue_key, token)
        await client.hdel(expiry_key, token)
        return 1

    if kind == "acquire_read":
        reads_key, write_key, queue_key, expiry_key = keys
        limit, lease_ttl, now = argv
        await purge_expiry(queue_key, expiry_key, float(now))
        if await client.exists(write_key) or await client.zcard(queue_key) > 0:
            return 0
        count = int(await client.incr(reads_key))
        if count <= int(limit):
            await client.expire(reads_key, int(lease_ttl))
            return 1
        await client.decr(reads_key)
        return 0

    if kind == "acquire_write":
        write_key, reads_key, queue_key, expiry_key = keys
        token, lease_ms, now = argv
        await purge_expiry(queue_key, expiry_key, float(now))
        if await client.exists(write_key) or int(await client.get(reads_key) or 0) > 0:
            return 0
        first = await client.zrange(queue_key, 0, 0)
        if not first or first[0] != token:
            return 0
        if await client.set(write_key, token, nx=True, px=int(lease_ms)):
            await client.zrem(queue_key, token)
            await client.hdel(expiry_key, token)
            return 1
        return 0

    if kind == "release_read":
        (reads_key,) = keys
        count = int(await client.get(reads_key) or 0)
        if count > 0:
            return int(await client.decr(reads_key))
        return 0

    if kind == "release_write":
        (write_key,) = keys
        (token,) = argv
        if await client.get(write_key) == token:
            await client.delete(write_key)
            return 1
        return 0

    raise AssertionError(f"Unexpected embedded limiter script kind: {kind}")


def _execute_fake_eval(
    client: Any, script: str, numkeys: int, *args: Any, is_async: bool = False
) -> Any:
    """Provide in-process Lua emulation for FakeRedis scripts when Lupa is absent."""
    keys = list(args[:numkeys])
    argv = list(args[numkeys:])
    script_str = str(script)

    limiter_kind = _limiter_script_kind(
        script_str,
        numkeys=numkeys,
        argument_count=len(argv),
    )
    if limiter_kind is not None:
        if is_async:
            return _execute_limiter_async(client, limiter_kind, keys, argv)
        with _limiter_eval_lock:
            return _execute_limiter_sync(client, limiter_kind, keys, argv)

    # 1. _CONSUME_TICKET_SCRIPT:
    # "local v=redis.call('get', KEYS[1]); if v then redis.call('del', KEYS[1]); end; return v"
    if "get" in script_str and "del" in script_str and len(keys) >= 1 and not argv:
        key = keys[0]
        if is_async:

            async def _run_async_consume() -> Any:
                v = await client.get(key)
                if v:
                    await client.delete(key)
                return v

            return _run_async_consume()
        v = client.get(key)
        if v:
            client.delete(key)
        return v

    # 2. _REFRESH_OWNER_TTL_SCRIPT:
    # if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end; return 0
    if "expire" in script_str and len(keys) >= 1 and len(argv) >= 2:
        key, owner, ttl = keys[0], argv[0], int(argv[1])
        if is_async:

            async def _run_async_refresh() -> int:
                curr = await client.get(key)
                if curr == owner:
                    await client.expire(key, ttl)
                    return 1
                return 0

            return _run_async_refresh()
        curr = client.get(key)
        if curr == owner:
            client.expire(key, ttl)
            return 1
        return 0

    # 3. _CLAIM_OWNER_SCRIPT:
    # if redis.call('exists', KEYS[1]) == 0 then redis.call('del', KEYS[2]); redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2]); return 1 end; return 0
    if "exists" in script_str and "set" in script_str and len(keys) >= 2 and len(argv) >= 2:
        online_key, req_key, conn_id, ttl = keys[0], keys[1], argv[0], int(argv[1])
        if is_async:

            async def _run_async_claim() -> int:
                if not (await client.exists(online_key)):
                    await client.delete(req_key)
                    await client.set(online_key, conn_id, ex=ttl)
                    return 1
                return 0

            return _run_async_claim()
        if not client.exists(online_key):
            client.delete(req_key)
            client.set(online_key, conn_id, ex=ttl)
            return 1
        return 0

    # 4. _REPLACE_LOCAL_OWNER_SCRIPT:
    # if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end; redis.call('del', KEYS[2]); redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1
    if "~=" in script_str and "set" in script_str and len(keys) >= 2 and len(argv) >= 3:
        online_key, req_key, old_conn, new_conn, ttl = (
            keys[0],
            keys[1],
            argv[0],
            argv[1],
            int(argv[2]),
        )
        if is_async:

            async def _run_async_replace() -> int:
                curr = await client.get(online_key)
                if curr != old_conn:
                    return 0
                await client.delete(req_key)
                await client.set(online_key, new_conn, ex=ttl)
                return 1

            return _run_async_replace()
        curr = client.get(online_key)
        if curr != old_conn:
            return 0
        client.delete(req_key)
        client.set(online_key, new_conn, ex=ttl)
        return 1

    # 5. _DELETE_OWNER_SCRIPT:
    # if redis.call('get', KEYS[1]) == ARGV[1] then redis.call('del', KEYS[1]); redis.call('del', KEYS[2]); return 1 end; return 0
    if "del" in script_str and len(keys) >= 2 and len(argv) >= 1:
        online_key, req_key, conn_id = keys[0], keys[1], argv[0]
        if is_async:

            async def _run_async_delete() -> int:
                curr = await client.get(online_key)
                if curr == conn_id:
                    await client.delete(online_key, req_key)
                    return 1
                return 0

            return _run_async_delete()
        curr = client.get(online_key)
        if curr == conn_id:
            client.delete(online_key, req_key)
            return 1
        return 0

    # Fallback for worker queue scripts
    if "exists" in script_str and len(keys) >= 1:
        if is_async:

            async def _run_async_exists() -> int:
                return 1 if (await client.exists(keys[0])) else 0

            return _run_async_exists()
        return 1 if client.exists(keys[0]) else 0

    if is_async:

        async def _run_async_default() -> int:
            return 0

        return _run_async_default()
    return 0


def _fake_eval(self: Any, script: str, numkeys: int, *args: Any) -> Any:
    return _execute_fake_eval(self, script, numkeys, *args, is_async=False)


async def _fake_eval_async(self: Any, script: str, numkeys: int, *args: Any) -> Any:
    return await _execute_fake_eval(self, script, numkeys, *args, is_async=True)


def get_fake_redis_sync(*args: Any, **kwargs: Any) -> fakeredis.FakeRedis:
    kw = dict(kwargs)
    if "decode_responses" not in kw:
        kw["decode_responses"] = True
    client = fakeredis.FakeRedis(*args, server=get_fake_server(), **kw)
    if not hasattr(client, "eval") or getattr(client.eval, "__name__", "") != "_fake_eval":
        method_name = "eval"
        setattr(client, method_name, _fake_eval.__get__(client, type(client)))
    return client


def get_fake_redis_async(*args: Any, **kwargs: Any) -> fakeredis.aioredis.FakeRedis:
    kw = dict(kwargs)
    if "decode_responses" not in kw:
        kw["decode_responses"] = True
    client = fakeredis.aioredis.FakeRedis(*args, server=get_fake_server(), **kw)
    if not hasattr(client, "eval") or getattr(client.eval, "__name__", "") != "_fake_eval_async":
        method_name = "eval"
        setattr(client, method_name, _fake_eval_async.__get__(client, type(client)))
    return client


_orig_redis_from_url = redis.Redis.from_url
_orig_aioredis_from_url = aioredis.Redis.from_url
_patched = False


def setup_embedded_redis() -> None:
    """Patch redis and aioredis from_url to fallback to in-memory fakeredis when running local-first."""
    global _patched
    if _patched:
        return

    def patched_sync_from_url(url: str, *args: Any, **kwargs: Any) -> Any:
        if not url or url.startswith("memory://"):
            return get_fake_redis_sync(*args, **kwargs)
        try:
            client = _orig_redis_from_url(url, *args, socket_connect_timeout=0.2, **kwargs)
            client.ping()
            return client
        except Exception:  # noqa: BLE001
            logger.debug("Local Redis daemon not reachable; falling back to in-memory FakeRedis.")
            return get_fake_redis_sync(*args, **kwargs)

    def patched_async_from_url(url: str, *args: Any, **kwargs: Any) -> Any:
        if not url or url.startswith("memory://"):
            return get_fake_redis_async(*args, **kwargs)
        try:
            if "localhost" in url or "127.0.0.1" in url:
                try:
                    test_c = _orig_redis_from_url(url, socket_connect_timeout=0.2)
                    test_c.ping()
                except Exception:
                    logger.debug(
                        "Local Redis daemon not reachable; using in-memory async FakeRedis."
                    )
                    return get_fake_redis_async(*args, **kwargs)
            return _orig_aioredis_from_url(url, *args, **kwargs)
        except Exception:  # noqa: BLE001
            return get_fake_redis_async(*args, **kwargs)

    factory_name = "from_url"
    setattr(redis, factory_name, patched_sync_from_url)
    setattr(redis.Redis, factory_name, patched_sync_from_url)
    setattr(aioredis, factory_name, patched_async_from_url)
    setattr(aioredis.Redis, factory_name, patched_async_from_url)
    _patched = True
    logger.info("Embedded Redis compatibility layer initialized with shared FakeServer.")
