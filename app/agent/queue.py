"""Agent 长任务：Redis Streams 队列 + 状态 Hash + Pub/Sub 事件 + 崩溃对账。

事实源是 PostgreSQL 的 agent_runs 表（见 0003 迁移）；Redis 承担队列与状态缓存。
Redis 故障时 API fail-closed（返回 503），不静默丢失任务。

对账（reconcile）处理两类崩溃窗口：
- commit 落库后、投递前崩溃 → run 停在 queued，重新投递；
- worker 处理中崩溃 → run 停在 running，标记 failed。
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.service import AgentService
from app.core.errors import AppError
from app.models import AgentRun

logger = logging.getLogger("demo")

STREAM_KEY = "agent:runs"
GROUP = "agent-workers"
STATE_KEY = "run:{run_id}"
EVENTS_CHANNEL = "run:{run_id}:events"


class AgentQueue:
    def __init__(self, redis: Redis):
        self.redis = redis
        self._group_ready = False

    async def _ensure_group(self):
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(STREAM_KEY, GROUP, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP 表示已存在
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def enqueue(self, run_id: str):
        await self._ensure_group()
        await self.redis.xadd(STREAM_KEY, {"run_id": run_id})

    async def set_state(self, run_id: str, status: str, version: int, **extra):
        payload = {
            "status": status,
            "version": str(version),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        payload.update({k: str(v) for k, v in extra.items()})
        await self.redis.hset(STATE_KEY.format(run_id=run_id), mapping=payload)

    async def get_state(self, run_id: str):
        return await self.redis.hgetall(STATE_KEY.format(run_id=run_id))

    async def publish(self, run_id: str, event: dict):
        await self.redis.publish(EVENTS_CHANNEL.format(run_id=run_id), json.dumps(event))

    async def consume(self, consumer: str, handle):
        """持续消费 Streams；handle(run_id) 由 worker 提供。"""
        await self._ensure_group()
        while True:
            try:
                entries = await self.redis.xreadgroup(
                    GROUP, consumer, {STREAM_KEY: ">"}, count=1, block=2000
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 短暂故障退避后重试
                await asyncio.sleep(0.5)
                continue
            if not entries:
                continue
            for _stream, messages in entries:
                for message_id, fields in messages:
                    run_id = fields.get("run_id") if isinstance(fields, dict) else None
                    try:
                        await handle(run_id)
                        await self.redis.xack(STREAM_KEY, GROUP, message_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - 失败留待下次重投（至少一次）
                        logger.warning("agent_run_retry", extra={"fields": {"run_id": run_id}})


def new_run_id() -> str:
    return str(uuid4())


async def create_run(
    session: AsyncSession,
    queue: AgentQueue,
    owner_id: str,
    prompt: str,
    idempotency_key: str | None,
):
    """事务内落 run 记录，提交后投递队列（简化版 outbox，由对账兜底）。"""
    run_id = new_run_id()
    run = AgentRun(id=run_id, owner_id=owner_id, prompt=prompt, status="queued", version=1)
    session.add(run)
    await session.commit()
    await queue.set_state(run_id, "queued", 1)
    await queue.enqueue(run_id)
    return run


async def reconcile_stale_runs(db, queue: AgentQueue, stale_seconds: int):
    """兜底：重新投递卡在 queued 的 run，把卡在 running 的 run 标记失败。"""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_seconds)
    async with db.sessions() as session:
        stale_queued = (
            await session.scalars(
                select(AgentRun).where(AgentRun.status == "queued", AgentRun.updated_at < cutoff)
            )
        ).all()
        for run in stale_queued:
            await queue.enqueue(run.id)
            logger.warning("agent_run_requeued", extra={"fields": {"run_id": run.id}})

        stale_running = (
            await session.scalars(
                select(AgentRun).where(AgentRun.status == "running", AgentRun.updated_at < cutoff)
            )
        ).all()
        for run in stale_running:
            run.status = "failed"
            run.error_code = "agent_reconcile_timeout"
            run.version += 1
            run.updated_at = datetime.now(UTC)
            await queue.set_state(run.id, "failed", run.version, error_code=run.error_code)
            await queue.publish(
                run.id,
                {"event": "status", "run_id": run.id, "status": "failed", "version": run.version},
            )
            logger.warning("agent_run_reconcile_failed", extra={"fields": {"run_id": run.id}})
        await session.commit()


async def run_worker(app, reconcile_interval: int = 30, stale_seconds: int = 120):
    """从 app.state 取依赖，消费队列中的 run，并周期性对账。"""
    queue: AgentQueue = app.state.agent_queue
    agent: AgentService = app.state.agent
    db = app.state.db

    async def handle(run_id: str):
        async with db.sessions() as session:  # 每个任务独立 Session
            run = await session.get(AgentRun, run_id)
            if run is None or run.status != "queued":
                return
            if run.owner_id is None:
                return
            run.status = "running"
            run.version += 1
            run.updated_at = datetime.now(UTC)
            await session.commit()
            await queue.set_state(run_id, "running", run.version)
            try:
                result = await agent.run(run.prompt, request_id=run_id)
                run.status = "succeeded"
                run.output = result.output
                run.version += 1
            except AppError as exc:
                run.status = "failed"
                run.error_code = exc.code
                run.version += 1
            except Exception as exc:  # noqa: BLE001 - 只记录类型，不写日志原文
                run.status = "failed"
                run.error_code = type(exc).__name__
                run.version += 1
            run.updated_at = datetime.now(UTC)
            await session.commit()
            await queue.set_state(run_id, run.status, run.version, error_code=run.error_code or "")
            await queue.publish(
                run_id,
                {"event": "status", "run_id": run_id, "status": run.status, "version": run.version},
            )

    async def reconcile_loop():
        while True:
            await asyncio.sleep(reconcile_interval)
            try:
                await reconcile_stale_runs(db, queue, stale_seconds)
            except Exception:  # noqa: BLE001 - 对账失败退避后重试
                logger.warning("agent_reconcile_failed")
                await asyncio.sleep(0.5)

    consume_task = asyncio.create_task(queue.consume(f"worker-{uuid4()}", handle))
    reconcile_task = asyncio.create_task(reconcile_loop())
    try:
        await asyncio.gather(consume_task, reconcile_task)
    finally:
        consume_task.cancel()
        reconcile_task.cancel()


async def get_run(session: AsyncSession, queue: AgentQueue, run_id: str, owner_id: str):
    try:
        UUID(run_id)
    except ValueError:
        raise AppError(422, "invalid_run_id", "任务 ID 不合法") from None
    run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
    if run is None or run.owner_id != owner_id:
        raise AppError(404, "run_not_found", "任务不存在")
    cached = await queue.get_state(run_id)
    return run, cached
