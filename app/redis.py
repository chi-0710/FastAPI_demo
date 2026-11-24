"""Redis 连接池封装：生命周期由应用 lifespan 管理，连接由 redis-py 池复用。"""

import redis.asyncio as redis


class RedisPool:
    """只持有客户端与 ping/close；具体命令由各能力模块使用。"""

    def __init__(self, url: str):
        self.client = redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            health_check_interval=30,
            max_connections=50,
        )

    async def ping(self):
        await self.client.ping()

    async def close(self):
        await self.client.aclose()
