"""Redis 接入验证：缓存、限流、幂等、会话缓存、长任务队列。"""

import asyncio

import httpx
import pytest
from sqlalchemy import select, update

from app.agent.queue import AgentQueue
from app.auth.rate_limiter import AuthRateLimiter
from app.auth.repository import AuthRepository
from app.auth.security import token_digest
from app.auth.service import AuthService
from app.core.config import Settings
from app.core.errors import AppError
from app.models import AgentRun, Todo, User

PASSWORD = "test-only-long-password-123"


async def test_todo_cache_hit_and_invalidation(client):
    """首次回源写缓存，二次命中；更新后版本号递增使旧 key 失效。"""
    redis = client.app.state.redis.client
    r = await client.post("/api/v1/todos", json={"title": "缓存测试"})
    todo_id = r.json()["id"]

    # 第一次 GET 回填缓存
    assert (await client.get(f"/api/v1/todos/{todo_id}")).status_code == 200
    detail_keys = [k for k in redis._store if k.startswith("td:")]
    assert detail_keys, "详情缓存未写入"

    # 删除底层记录后仍能读到（证明命中缓存而非回源）
    async with client.app.state.db.sessions() as db:
        await db.execute(update(Todo).where(Todo.id == todo_id).values(title="数据库被改"))
        await db.commit()
    assert (await client.get(f"/api/v1/todos/{todo_id}")).json()["title"] == "缓存测试"

    # 更新后失效
    r = await client.put(f"/api/v1/todos/{todo_id}", json={"title": "已更新", "version": 1})
    assert r.status_code == 200
    assert (await client.get(f"/api/v1/todos/{todo_id}")).json()["title"] == "已更新"


async def test_cache_owner_isolation(client):
    """不同用户的缓存 key 不串数据。"""
    redis = client.app.state.redis.client
    mine = (await client.post("/api/v1/todos", json={"title": "我的"})).json()
    await client.get(f"/api/v1/todos/{mine['id']}")  # 触发详情缓存写入

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    ) as other:
        r = await other.post(
            "/api/v1/auth/register", json={"username": "other_user", "password": PASSWORD}
        )
        other.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        assert (await other.get("/api/v1/todos")).json()["items"] == []
        assert (await other.get(f"/api/v1/todos/{mine['id']}")).status_code == 404
    assert any(k.startswith(f"td:{client.user_id}:") for k in redis._store)


async def test_session_cache_written_and_cleared_on_logout(client):
    """会话缓存写入；登出后清除（测试环境禁用读取缓存，但仍检查写/删路径）。"""
    redis = client.app.state.redis.client
    async with client.app.state.db.sessions() as db:
        service = AuthService(AuthRepository(db), 8, redis=redis, cache_ttl=300)
        user = await db.scalar(select(User).where(User.id == client.user_id))
        token, session = await service.start_session(user, None)
        key = f"sess:{token_digest(token)}"
        assert await redis.get(key) is not None
        # 缓存命中能解析出身份
        identity = await service.resolve(token)
        assert identity.session.csrf_token == session.csrf_token
        await service.logout(session)
        assert await redis.get(key) is None


async def test_redis_rate_limit_atomic_and_429(client):
    """Lua 限流：N 次内通过，超限 429。"""
    redis = client.app.state.redis.client
    limiter = AuthRateLimiter(redis)
    for _ in range(5):
        assert await limiter.consume("rl:test:ip:1.2.3.4", 5)
    with pytest.raises(AppError) as err:
        await limiter.consume("rl:test:ip:1.2.3.4", 5)
    assert err.value.status == 429


async def test_idempotency_create_only_once(client):
    """同一 Idempotency-Key 重复 POST 只创建一条，返回首次结果。"""
    key = "order-123"
    first = await client.post(
        "/api/v1/todos", json={"title": "幂等"}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/todos", json={"title": "幂等"}, headers={"Idempotency-Key": key}
    )
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    items = (await client.get("/api/v1/todos")).json()["items"]
    assert len([i for i in items if i["title"] == "幂等"]) == 1

    # 不同用户同名 key 互不干扰
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    ) as other:
        r = await other.post(
            "/api/v1/auth/register", json={"username": "idem_user", "password": PASSWORD}
        )
        other.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        result = await other.post(
            "/api/v1/todos", json={"title": "别人的幂等"}, headers={"Idempotency-Key": key}
        )
        assert result.status_code == 201
        assert result.json()["id"] != first.json()["id"]


async def test_idempotency_requires_redis(client):
    """未提供 Redis 且带幂等键时 fail-closed。"""
    from app.todos.idempotency import IdempotencyGuard

    guard = IdempotencyGuard(None, "owner", 86400)
    with pytest.raises(AppError) as err:
        await guard.begin("k")
    assert err.value.status == 503


async def test_agent_run_queue_and_state(client):
    """提交任务 → 队列有消息 → worker 处理 → 状态与输出落库。"""
    # 测试环境未启动后台 worker，这里手动驱动一次。
    from app.agent.queue import run_worker
    from app.agent.service import AgentService, MockAgent

    client.app.state.agent = AgentService(MockAgent(), 5)

    r = await client.post("/api/v1/agent/runs", json={"prompt": "分析项目"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    queue: AgentQueue = client.app.state.agent_queue
    state = await queue.get_state(run_id)
    assert state["status"] == "queued"

    # 手动消费一条
    task = asyncio.create_task(run_worker(client.app))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    r = await client.get(f"/api/v1/agent/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert "分析项目" in r.json()["output"]

    async with client.app.state.db.sessions() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "succeeded" and run.output


async def test_agent_run_owner_isolation(client):
    """他人不能读取我的任务。"""
    r = await client.post("/api/v1/agent/runs", json={"prompt": "私密任务"})
    run_id = r.json()["run_id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    ) as other:
        reg = await other.post(
            "/api/v1/auth/register", json={"username": "voyeur", "password": PASSWORD}
        )
        other.headers["X-CSRF-Token"] = reg.json()["csrf_token"]
        assert (await other.get(f"/api/v1/agent/runs/{run_id}")).status_code == 404


def test_redis_production_config_guard():
    """生产必须使用 rediss:// 且带密码。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://u:p@localhost/test",
            redis_url="redis://localhost:6379/0",
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://u:p@localhost/test",
            redis_url="rediss://localhost:6379/0",
            redis_enabled=True,
        )
