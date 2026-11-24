"""幂等守卫：SET NX 占位 + 结果回放；Redis 不可用时 fail-closed。"""

import hashlib

from redis.asyncio import Redis

from app.core.errors import AppError
from app.todos.schemas import TodoRead


class IdempotencyGuard:
    def __init__(self, redis: Redis | None, owner_id: str, ttl: int):
        self.redis = redis
        self.owner_id = owner_id
        self.ttl = ttl

    def key_for(self, raw: str) -> str:
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"idem:{self.owner_id}:{digest}"

    async def begin(self, raw: str) -> tuple[bool, TodoRead | None]:
        """尝试占位。返回 (是否首次获得执行权, 已提交的回放结果)。"""
        if self.redis is None:
            raise AppError(503, "idempotency_unavailable", "幂等依赖不可用")
        key = self.key_for(raw)
        try:
            acquired = await self.redis.set(key, "processing", nx=True, ex=self.ttl)
        except Exception:  # noqa: BLE001 - 无法保证幂等时拒绝写入
            raise AppError(503, "idempotency_unavailable", "幂等依赖不可用") from None
        if acquired:
            return True, None
        # 已提交：回放首次结果；处理中：拒绝并发重试。
        stored = await self.redis.get(key)
        if stored and stored != "processing":
            return False, TodoRead.model_validate_json(stored)
        raise AppError(409, "idempotency_conflict", "重复请求正在处理中")

    async def commit(self, raw: str, result_json: str):
        await self.redis.set(self.key_for(raw), result_json, ex=self.ttl)

    async def abort(self, raw: str):
        try:
            await self.redis.delete(self.key_for(raw))
        except Exception:  # noqa: BLE001 - 清理失败不影响主流程
            pass
