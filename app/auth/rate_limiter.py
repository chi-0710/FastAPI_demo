"""基于 Redis 的原子限流计数（Lua 保证读+增+过期原子性）。

限流是安全边界：Redis 故障时 fail-closed（拒绝请求），不静默放行。
保留旧的数据库限流实现作为 fallback（RATE_LIMIT_BACKEND=db）。
"""

import logging

from redis.asyncio import Redis

from app.core.errors import AppError

logger = logging.getLogger("demo")

# INCR + 首次设置 EXPIRE + 超限判断，全部在 Redis 内原子执行。
LUA_RATE_LIMIT = """
local current = tonumber(redis.call('INCR', KEYS[1]))
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if current > tonumber(ARGV[1]) then
  return 0
end
return 1
"""


class AuthRateLimiter:
    def __init__(self, redis: Redis, window_seconds: int = 900):
        self.redis = redis
        self.window = window_seconds
        self._script = self.redis.register_script(LUA_RATE_LIMIT)

    async def consume(self, bucket: str, limit: int) -> bool:
        try:
            allowed = await self._script(keys=[bucket], args=[limit, self.window])
        except Exception:  # noqa: BLE001 - 限流不可用时拒绝，避免被绕过
            logger.warning("rate_limit_redis_failed")
            raise AppError(503, "rate_limiter_unavailable", "限流服务暂不可用") from None
        if not allowed:
            raise AppError(429, "auth_rate_limited", "尝试次数过多，请在 15 分钟后重试")
        return True
