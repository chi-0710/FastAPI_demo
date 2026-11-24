import asyncio
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from app.agent.service import AgentService, MockAgent
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.models import Todo
from app.todos.repository import TodoRepository
from app.todos.schemas import TodoReplace
from app.todos.service import TodoService


async def test_crud_filter_paging_and_conflict(client):
    response = await client.post("/api/v1/todos", json={"title": " 学习工程化 "})
    assert response.status_code == 201
    todo = response.json()
    assert todo["title"] == "学习工程化"
    assert todo["version"] == 1
    path = f"/api/v1/todos/{todo['id']}"
    response = await client.put(path, json={"title": "已学会", "completed": True, "version": 1})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert (await client.put(path, json={"title": "旧修改", "version": 1})).status_code == 409
    page = (await client.get("/api/v1/todos?completed=true&limit=1")).json()
    assert len(page["items"]) == 1
    assert (await client.get("/api/v1/todos?offset=1")).json()["items"] == []
    assert (await client.delete(path + "?version=1")).status_code == 409
    response = await client.delete(path + "?version=2")
    assert response.status_code == 204 and response.content == b""
    assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize(
    "body", [{}, {"title": "   "}, {"title": "a" * 101}, {"title": "ok", "admin": True}]
)
async def test_validation(client, body):
    response = await client.post("/api/v1/todos", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert all("input" not in item for item in response.json()["error"]["details"])


async def test_auth_health_and_errors(client):
    client.cookies.clear()
    assert (await client.get("/api/v1/todos")).status_code == 401
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/missing")).json()["error"]["code"] == "http_error"
    assert (await client.get("/health/live", headers={"Host": "evil.invalid"})).status_code == 400


async def test_invalid_ids_and_limits(client):
    assert (await client.get("/api/v1/todos/abc")).status_code == 422
    assert (await client.get(f"/api/v1/todos/{uuid4()}")).status_code == 404
    assert (await client.get("/api/v1/todos?limit=101")).status_code == 422


async def test_agent_disabled_mock_timeout_and_failure(client):
    assert (await client.post("/api/v1/agent/invoke", json={"prompt": "任务"})).status_code == 503
    client.app.state.agent = AgentService(MockAgent(), 1)
    result = await client.post("/api/v1/agent/invoke", json={"prompt": "任务"})
    assert result.json()["provider"] == "mock"

    class Slow:
        async def run(self, prompt, *, request_id):
            await asyncio.sleep(1)

    client.app.state.agent = AgentService(Slow(), 0.001)
    assert (await client.post("/api/v1/agent/invoke", json={"prompt": "任务"})).status_code == 504

    class Broken:
        async def run(self, prompt, *, request_id):
            raise RuntimeError("secret-provider-token")

    client.app.state.agent = AgentService(Broken(), 1)
    result = await client.post("/api/v1/agent/invoke", json={"prompt": "任务"})
    assert result.status_code == 500
    assert "secret-provider-token" not in result.text
    assert result.headers["X-Request-ID"] == result.json()["request_id"]


async def test_persistence_between_app_instances(client, settings):
    todo = (await client.post("/api/v1/todos", json={"title": "持久化"})).json()
    second_app = create_app(settings)
    async with second_app.router.lifespan_context(second_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://testserver",
            headers=client.headers,
            cookies=client.cookies,
        ) as other:
            assert (await other.get(f"/api/v1/todos/{todo['id']}")).json()["title"] == "持久化"


async def test_concurrent_sessions_and_rollback(client):
    todo = (await client.post("/api/v1/todos", json={"title": "原始记录"})).json()
    async with client.app.state.db.sessions() as a, client.app.state.db.sessions() as b:
        one, two = (
            TodoService(TodoRepository(a, client.user_id)),
            TodoService(TodoRepository(b, client.user_id)),
        )
        # 两个 session 先读取同一版本，随后第二次提交必须被数据库拒绝。
        first = await one.get(todo["id"])
        second = await two.get(todo["id"])
        assert first.version == second.version == 1
        await one.replace(todo["id"], TodoReplace(title="winner", version=1))
        with pytest.raises(AppError) as error:
            await two.replace(todo["id"], TodoReplace(title="loser", version=1))
        assert error.value.status == 409
    async with client.app.state.db.sessions() as session:
        session.add(Todo(title="不提交"))
        await session.flush()
    assert len((await client.get("/api/v1/todos")).json()["items"]) == 1


async def test_readiness_without_migration(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503


def test_production_guards_and_docs():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+asyncpg://u:p@localhost/test",
        redis_url="rediss://:secret@localhost:6379/0",
    )
    assert create_app(settings).openapi_url is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), "agent_backend": "mock"})


def test_migration_roundtrip_and_metadata(database_url):
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.check(config)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)
