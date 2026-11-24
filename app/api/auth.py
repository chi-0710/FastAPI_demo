from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import check_origin, cookie_name, get_auth_service, require_session
from app.auth.schemas import Credentials, RegistrationCredentials, SessionIdentity
from app.auth.service import AuthService
from app.core.errors import AppError

auth = APIRouter(prefix="/api/v1/auth", tags=["auth"])
Auth = Annotated[AuthService, Depends(get_auth_service)]
Identity = Annotated[SessionIdentity, Depends(require_session)]


def session_payload(session, user):
    return {"user": {"id": user.id, "username": user.username}, "csrf_token": session.csrf_token}


async def establish(
    data: Credentials, request: Request, response: Response, service: AuthService, register: bool
):
    check_origin(request)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise AppError(415, "json_required", "请使用 JSON 请求")
    # 不直接信任客户端伪造的 X-Forwarded-For；仅由可信代理配置解析地址。
    address = request.client.host if request.client else "unknown"
    await service.throttle("register" if register else "login", address, data.username)
    async with request.app.state.password_slots:
        user = await service.authenticate(data.username, data.password.get_secret_value(), register)
    token, session = await service.start_session(user, request.cookies.get(cookie_name(request)))
    response.set_cookie(
        cookie_name(request),
        token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.app.state.settings.app_env == "production",
        max_age=request.app.state.settings.session_hours * 3600,
    )
    return session_payload(session, user)


@auth.post("/register", status_code=201)
async def register(
    data: RegistrationCredentials, request: Request, response: Response, service: Auth
):
    return await establish(data, request, response, service, register=True)


@auth.post("/login")
async def login(data: Credentials, request: Request, response: Response, service: Auth):
    return await establish(data, request, response, service, register=False)


@auth.get("/me")
async def me(identity: Identity):
    return session_payload(identity.session, identity.user)


@auth.post("/logout", status_code=204)
async def logout(request: Request, response: Response, service: Auth, identity: Identity):
    await service.logout(identity.session)
    response.delete_cookie(
        cookie_name(request),
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.app_env == "production",
    )
