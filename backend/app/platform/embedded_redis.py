from __future__ import annotations

import logging
from typing import Any

import fakeredis
import fakeredis.aioredis
import redis
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_shared_server: fakeredis.FakeServer | None = None


def get_fake_server() -> fakeredis.FakeServer:
    global _shared_server
    if _shared_server is None:
        _shared_server = fakeredis.FakeServer()
    return _shared_server


def _execute_fake_eval(
    client: Any, script: str, numkeys: int, *args: Any, is_async: bool = False
) -> Any:
    """Provide in-process Lua emulation for FakeRedis scripts when Lupa is absent."""
    keys = list(args[:numkeys])
    argv = list(args[numkeys:])
    script_str = str(script)

    # 1. _CONSUME_TICKET_SCRIPT:
    # "local v=redis.call('get', KEYS[1]); if v then redis.call('del', KEYS[1]); end; return v"
    if "get" in script_str and "del" in script_str and len(keys) >= 1 and not argv:
        key = keys[0]
        if is_async:

            async def _run_async_consume():
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

            async def _run_async_refresh():
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

            async def _run_async_claim():
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

            async def _run_async_replace():
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

            async def _run_async_delete():
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

            async def _run_async_exists():
                return 1 if (await client.exists(keys[0])) else 0

            return _run_async_exists()
        return 1 if client.exists(keys[0]) else 0

    if is_async:

        async def _run_async_default():
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
    client = fakeredis.FakeRedis(server=get_fake_server(), *args, **kw)
    if not hasattr(client, "eval") or getattr(client.eval, "__name__", "") != "_fake_eval":
        client.eval = _fake_eval.__get__(client, type(client))  # type: ignore[assignment]
    return client


def get_fake_redis_async(*args: Any, **kwargs: Any) -> fakeredis.aioredis.FakeRedis:
    kw = dict(kwargs)
    if "decode_responses" not in kw:
        kw["decode_responses"] = True
    client = fakeredis.aioredis.FakeRedis(server=get_fake_server(), *args, **kw)
    if not hasattr(client, "eval") or getattr(client.eval, "__name__", "") != "_fake_eval_async":
        client.eval = _fake_eval_async.__get__(client, type(client))  # type: ignore[assignment]
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
            client = _orig_redis_from_url(url, socket_connect_timeout=0.2, *args, **kwargs)
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

    redis.from_url = patched_sync_from_url  # type: ignore[assignment]
    redis.Redis.from_url = patched_sync_from_url  # type: ignore[assignment]
    aioredis.from_url = patched_async_from_url  # type: ignore[assignment]
    aioredis.Redis.from_url = patched_async_from_url  # type: ignore[assignment]
    _patched = True
    logger.info("Embedded Redis compatibility layer initialized with shared FakeServer.")
