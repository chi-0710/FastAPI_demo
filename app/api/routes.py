import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.queue import AgentQueue, create_run, get_run
from app.agent.schemas import AgentRequest, AgentResponse
from app.api.dependencies import (
    get_agent_queue,
    get_redis,
    get_session,
    get_todo_service,
    require_session,
)
from app.auth.schemas import SessionIdentity
from app.core.errors import AppError
from app.todos.schemas import TodoCreate, TodoPage, TodoRead, TodoReplace
from app.todos.service import TodoService

health = APIRouter(tags=["health"])
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_session)])
Service = Annotated[TodoService, Depends(get_todo_service)]


@health.get("/health/live")
async def live():
    return {"status": "alive"}


@health.get("/health/ready")
async def ready(request: Request):
    try:
        async with asyncio.timeout(3), request.app.state.db.sessions() as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            await session.execute(text("SELECT id FROM todos LIMIT 1"))
            await session.execute(text("SELECT token_hash FROM login_sessions LIMIT 1"))
            await session.execute(text("SELECT id FROM agent_runs LIMIT 1"))
            if revision != "0003":
                raise AppError(503, "not_ready", "数据库版本不匹配")
    except (SQLAlchemyError, TimeoutError) as exc:
        raise AppError(503, "not_ready", "数据库未就绪") from exc
    # Redis 为可选依赖；启用时必须可用，否则就绪检查失败。
    if request.app.state.settings.redis_enabled:
        try:
            async with asyncio.timeout(2):
                await request.app.state.redis.ping()
        except Exception as exc:  # noqa: BLE001
            raise AppError(503, "not_ready", "Redis 未就绪") from exc
    return {"status": "ready"}


@api.get("/todos", response_model=TodoPage, tags=["todos"])
async def list_todos(
    service: Service,
    completed: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    items = await service.list(completed, limit, offset)
    return {"items": items, "limit": limit, "offset": offset}


@api.post("/todos", response_model=TodoRead, status_code=201, tags=["todos"])
async def create_todo(
    data: TodoCreate,
    service: Service,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return await service.create(data, idempotency_key=idempotency_key)


@api.get("/todos/{todo_id}", response_model=TodoRead, tags=["todos"])
async def get_todo(todo_id: UUID, service: Service):
    return await service.get(str(todo_id))


@api.put("/todos/{todo_id}", response_model=TodoRead, tags=["todos"])
async def replace_todo(todo_id: UUID, data: TodoReplace, service: Service):
    return await service.replace(str(todo_id), data)


@api.delete("/todos/{todo_id}", status_code=204, tags=["todos"])
async def delete_todo(todo_id: UUID, service: Service, version: Annotated[int, Query(ge=1)]):
    await service.delete(str(todo_id), version)
    return Response(status_code=204)


@api.post("/agent/invoke", response_model=AgentResponse, tags=["agent"])
async def invoke_agent(data: AgentRequest, request: Request):
    return await request.app.state.agent.run(data.prompt, request.state.request_id)


@api.post("/agent/runs", status_code=202, tags=["agent"])
async def submit_agent_run(
    data: AgentRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    queue: Annotated[AgentQueue, Depends(get_agent_queue)],
    identity: Annotated[SessionIdentity, Depends(require_session)],
):
    """提交长任务，返回 202 + run_id；状态由 GET 查询或 SSE 订阅。"""
    if not request.app.state.settings.redis_enabled:
        raise AppError(503, "redis_disabled", "长任务需要 Redis")
    run = await create_run(session, queue, identity.user.id, data.prompt, None)
    return {"run_id": run.id, "status": "queued"}


@api.get("/agent/runs/{run_id}", tags=["agent"])
async def read_agent_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    queue: Annotated[AgentQueue, Depends(get_agent_queue)],
    identity: Annotated[SessionIdentity, Depends(require_session)],
):
    run, cached = await get_run(session, queue, run_id, identity.user.id)
    return {
        "run_id": run.id,
        "status": cached.get("status", run.status) if cached else run.status,
        "version": int(cached["version"]) if cached and "version" in cached else run.version,
        "output": run.output,
        "error_code": run.error_code,
    }


@api.get("/agent/runs/{run_id}/events", tags=["agent"])
async def agent_run_events(
    run_id: str,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    session: Annotated[AsyncSession, Depends(get_session)],
    queue: Annotated[AgentQueue, Depends(get_agent_queue)],
    identity: Annotated[SessionIdentity, Depends(require_session)],
):
    """SSE 事件流：断线可用 GET /agent/runs/{id} 补状态。"""
    await get_run(session, queue, run_id, identity.user.id)  # 权限校验

    async def stream():
        pubsub = redis.pubsub()
        channel = f"run:{run_id}:events"
        await pubsub.subscribe(channel)
        try:
            # 首帧发送当前状态，避免订阅与事件之间出现空窗。
            cached = await queue.get_state(run_id)
            frame = json.dumps(
                {
                    "event": "status",
                    "run_id": run_id,
                    "status": cached.get("status", "queued"),
                }
            )
            yield f"data: {frame}\n\n"
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    yield f"data: {message['data']}\n\n"
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream")
