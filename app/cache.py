"""Redis 缓存辅助：统一 JSON 序列化与失败降级（fail-open）。

原则：缓存故障不使业务失败。所有写入都带 TTL，配合 volatile-lru 防止内存打满。
领域专属的键构造与版本化失效位于各领域包（如 app/todos/cache.py）。
"""

import json
import logging

from redis.asyncio import Redis

logger = logging.getLogger("demo")


async def cache_get_json(client: Redis, key: str):
    try:
        raw = await client.get(key)
    except Exception:  # noqa: BLE001
        logger.warning("cache_get_failed", extra={"fields": {"key_prefix": key.split(":")[0]}})
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def cache_set_json(client: Redis, key: str, value, ttl: int):
    try:
        await client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception:  # noqa: BLE001
        logger.warning("cache_set_failed", extra={"fields": {"key_prefix": key.split(":")[0]}})


async def cache_delete(client: Redis, *keys: str):
    if not keys:
        return
    try:
        await client.delete(*keys)
    except Exception:  # noqa: BLE001
        logger.warning(
            "cache_delete_failed", extra={"fields": {"key_prefix": keys[0].split(":")[0]}}
        )
