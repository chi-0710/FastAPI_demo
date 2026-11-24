import secrets
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.queue import AgentQueue
from app.auth.repository import AuthRepository
from app.auth.schemas import SessionIdentity
from app.auth.service import AuthService
from app.core.errors import AppError
from app.todos.cache import TodoCache
from app.todos.idempotency import IdempotencyGuard
from app.todos.repository import TodoRepository
from app.todos.service import TodoService


def cookie_name(request: Request) -> str:
    if request.app.state.settings.app_env == "production":
        return "__Host-flowspace_session"
    return "flowspace_session"


def check_origin(request: Request):
    origin = request.headers.get("origin")
    allowed = [*request.app.state.settings.cors_origins, str(request.base_url).rstrip("/")]
    if origin and origin not in allowed:
        raise AppError(403, "untrusted_origin", "请求来源不受信任")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AppError(403, "cross_site_request", "请从同站点的前端访问")


async def get_session(request: Request):
    # 每请求独立 Session；异常或取消时回滚未提交事务并释放连接。
    async with request.app.state.db.sessions() as session:
        yield session


async def get_redis(request: Request):
    # Redis 客户端由应用 lifespan 中的连接池持有，这里只暴露引用。
    return request.app.state.redis.client


def get_auth_service(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    settings = request.app.state.settings
    return AuthService(
        AuthRepository(session),
        settings.session_hours,
        redis=request.app.state.redis.client if settings.redis_enabled else None,
        cache_ttl=(0 if settings.app_env == "test" else settings.session_cache_ttl_seconds),
    )


async def require_session(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> SessionIdentity:
    identity = await auth.resolve(request.cookies.get(cookie_name(request)))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        supplied = x_csrf_token or ""
        if not secrets.compare_digest(supplied.encode(), identity.session.csrf_token.encode()):
            raise AppError(403, "invalid_csrf", "页面会话已变化，请刷新后再试")
    return identity


def get_agent_queue(request: Request) -> AgentQueue:
    return request.app.state.agent_queue


def get_todo_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[SessionIdentity, Depends(require_session)],
):
    settings = request.app.state.settings
    redis = request.app.state.redis.client if settings.redis_enabled else None
    return TodoService(
        TodoRepository(session, identity.user.id),
        cache=TodoCache(redis, identity.user.id, settings.cache_ttl_seconds),
        idempotency=IdempotencyGuard(redis, identity.user.id, settings.idempotency_ttl_seconds),
    )
