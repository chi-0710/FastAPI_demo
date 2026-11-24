import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent.queue import AgentQueue, run_worker
from app.agent.service import AgentService, MockAgent
from app.api.auth import auth
from app.api.routes import api, health
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db import Database
from app.redis import RedisPool

logger = logging.getLogger("demo")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        app.state.db = Database(settings.database_url.get_secret_value())
        app.state.password_slots = asyncio.Semaphore(4)
        runner = MockAgent() if settings.agent_backend == "mock" else None
        app.state.agent = AgentService(runner, settings.agent_timeout_seconds)
        # Redis 可选：关闭时各能力模块回源数据库；ready 检查会报告未就绪。
        app.state.redis = RedisPool(settings.redis_url.get_secret_value())
        if settings.redis_enabled:
            await app.state.redis.ping()
        app.state.agent_queue = AgentQueue(app.state.redis.client)
        # 后台 worker 消费 Agent 长任务；测试环境由测试自行驱动，避免后台线程干扰。
        worker_task = None
        if settings.redis_enabled and settings.app_env != "test":
            worker_task = asyncio.create_task(
                run_worker(
                    app,
                    reconcile_interval=settings.agent_reconcile_interval_seconds,
                    stale_seconds=settings.agent_reconcile_stale_seconds,
                )
            )
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            await app.state.redis.close()
            await app.state.db.close()

    is_dev = settings.app_env != "production"
    app = FastAPI(
        title="Production-oriented FastAPI Demo",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if is_dev else None,
    )
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    def error_response(request, status, code, message, details=None):
        request_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=status,
            content={
                "error": {"code": code, "message": message, "details": details},
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            # 在 CORS 内部转成响应，让跨域前端也能读取脱敏的 500 错误。
            response = await unexpected_error(request, exc)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logger.info(
            "http_request",
            extra={
                "fields": {
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                }
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # 不返回 input/context，避免原始敏感输入被错误响应带出。
        details = [
            {"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]} for e in exc.errors()
        ]
        return error_response(request, 422, "validation_error", "请求参数不合法", details)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        response = error_response(request, exc.status_code, "http_error", str(exc.detail))
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # 原始 DB/SDK 异常可能含 SQL 参数或密钥，因此只记录异常类型。
        logger.error(
            "unhandled_error",
            extra={
                "fields": {
                    "request_id": getattr(request.state, "request_id", "unknown"),
                    "exception_type": type(exc).__name__,
                }
            },
        )
        return error_response(request, 500, "internal_error", "服务器内部错误")

    app.include_router(health)
    app.include_router(api)
    app.include_router(auth)

    @app.get("/api/v1/capabilities", tags=["capabilities"])
    async def capabilities():
        # 只公开前端能力标记，不序列化 Settings。
        return {"docs_url": app.docs_url, "agent_mode": settings.agent_backend}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
        expose_headers=["X-Request-ID"],
    )
    return app
