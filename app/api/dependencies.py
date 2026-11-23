import secrets
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repository import AuthRepository
from app.auth.service import AuthService
from app.core.errors import AppError
from app.repositories import TodoRepository
from app.services import TodoService


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


def get_auth_service(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    return AuthService(AuthRepository(session), request.app.state.settings.session_hours)


async def require_session(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    x_csrf_token: Annotated[str | None, Header()] = None,
):
    session, user = await auth.resolve(request.cookies.get(cookie_name(request)))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        supplied = x_csrf_token or ""
        if not secrets.compare_digest(supplied.encode(), session.csrf_token.encode()):
            raise AppError(403, "invalid_csrf", "页面会话已变化，请刷新后再试")
    return session, user


def get_todo_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[tuple, Depends(require_session)],
):
    return TodoService(TodoRepository(session, identity[1].id))
