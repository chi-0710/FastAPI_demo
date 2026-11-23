import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import get_todo_service, require_session
from app.core.errors import AppError
from app.schemas import AgentRequest, AgentResponse, TodoCreate, TodoPage, TodoRead, TodoReplace
from app.services import TodoService

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
            if revision != "0002":
                raise AppError(503, "not_ready", "数据库版本不匹配")
    except (SQLAlchemyError, TimeoutError) as exc:
        raise AppError(503, "not_ready", "数据库未就绪") from exc
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
async def create_todo(data: TodoCreate, service: Service):
    return await service.create(data)


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
