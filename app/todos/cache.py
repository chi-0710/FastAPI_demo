"""Todo 缓存：版本化键 + Cache-aside，写后失效。

缓存故障不使业务失败（fail-open）；所有键绑定 owner，避免跨用户串数据；
所有写入带 TTL，配合 volatile-lru 防止内存打满。
"""

import logging

from redis.asyncio import Redis

from app.cache import cache_get_json, cache_set_json

logger = logging.getLogger("demo")

OWNER_VERSION_KEY = "ver:todos:{owner_id}"  # 版本化键：写操作 INCR 后旧列表键自然失效


async def owner_version(client: Redis, owner_id: str) -> int:
    """读取当前版本号；缓存不可用时返回 0（退化为固定 key，仍可用）。"""
    try:
        value = await client.get(OWNER_VERSION_KEY.format(owner_id=owner_id))
        return int(value) if value is not None else 0
    except Exception:  # noqa: BLE001
        return 0


async def bump_owner_version(client: Redis, owner_id: str):
    try:
        await client.incr(OWNER_VERSION_KEY.format(owner_id=owner_id))
    except Exception:  # noqa: BLE001
        logger.warning("cache_bump_failed")


class TodoCache:
    """缓存读写的唯一入口；redis 为 None 时退化为直读数据库。"""

    def __init__(self, redis: Redis | None, owner_id: str, ttl: int):
        self.redis = redis
        self.owner_id = owner_id
        self.ttl = ttl

    def _list_key(self, version: int, completed: bool | None, limit: int, offset: int) -> str:
        return f"tl:{self.owner_id}:{version}:{completed}:{limit}:{offset}"

    def _detail_key(self, version: int, todo_id: str) -> str:
        return f"td:{self.owner_id}:{version}:{todo_id}"

    async def _version(self) -> int:
        if self.redis is None:
            return 0
        return await owner_version(self.redis, self.owner_id)

    async def get_list(self, completed: bool | None, limit: int, offset: int):
        if self.redis is None:
            return None
        version = await self._version()
        return await cache_get_json(self.redis, self._list_key(version, completed, limit, offset))

    async def set_list(self, completed: bool | None, limit: int, offset: int, payload):
        if self.redis is None:
            return
        version = await self._version()
        await cache_set_json(
            self.redis, self._list_key(version, completed, limit, offset), payload, self.ttl
        )

    async def get_detail(self, todo_id: str):
        if self.redis is None:
            return None
        version = await self._version()
        return await cache_get_json(self.redis, self._detail_key(version, todo_id))

    async def set_detail(self, todo_id: str, data):
        if self.redis is None:
            return
        version = await self._version()
        await cache_set_json(self.redis, self._detail_key(version, todo_id), data, self.ttl)

    async def invalidate(self):
        if self.redis is not None:
            await bump_owner_version(self.redis, self.owner_id)
