from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.auth.security import token_digest
from app.main import create_app
from app.models import AuthRateLimit, LoginSession, Todo, User

PASSWORD = "test-only-long-password-123"


async def test_production_cookie_flags(settings):
    # 只覆盖环境标志以测试 Cookie，仍连接 fixture 的可销毁数据库。
    app = create_app(settings.model_copy(update={"app_env": "production"}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as browser,
    ):
        response = await browser.post(
            "/api/v1/auth/register",
            json={
                "username": "secure_user",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-flowspace_session=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
        assert "Domain=" not in cookie
        assert (await browser.get("/api/v1/auth/me")).status_code == 200
        browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        result = await browser.post("/api/v1/auth/logout")
        assert result.status_code == 204 and "Secure" in result.headers["set-cookie"]
        assert (await browser.get("/api/v1/auth/me")).status_code == 401


async def test_login_http_limit_and_same_origin_docs(client):
    # HTTP 走 Redis 原子限流；同一窗口内先耗尽 ip 桶。
    redis = client.app.state.redis.client
    for _ in range(30):
        from app.auth.rate_limiter import AuthRateLimiter

        await AuthRateLimiter(redis).consume("rl:login:ip:127.0.0.1", 30)
    result = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "tester",
            "password": PASSWORD,
        },
    )
    assert result.status_code == 429
    # 文档页与 API 同 origin，无需添加到跨域白名单。
    response = await client.post(
        "/api/v1/todos", headers={"Origin": "http://testserver"}, json={"title": "from docs"}
    )
    assert response.status_code == 201


def anonymous(client):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    )


async def test_signup_login_rotation_and_logout(client):
    original = client.cookies.get("flowspace_session")
    async with anonymous(client) as browser:
        result = await browser.post(
            "/api/v1/auth/register",
            json={
                "username": " Second_User ",
                "password": PASSWORD,
            },
        )
        assert result.status_code == 201
        assert result.json()["user"]["username"] == "second_user"
        cookie = result.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=28800" in cookie
        assert "password" not in result.text
        first_token = browser.cookies.get("flowspace_session")
        login = await browser.post(
            "/api/v1/auth/login",
            json={
                "username": "SECOND_USER",
                "password": PASSWORD,
            },
        )
        assert login.status_code == 200
        assert browser.cookies.get("flowspace_session") != first_token
        browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await browser.get("/api/v1/auth/me")).status_code == 200
        last_token = browser.cookies.get("flowspace_session")
        assert (await browser.post("/api/v1/auth/logout")).status_code == 204
        assert not browser.cookies
        for token in (first_token, last_token):
            assert (
                await browser.get(
                    "/api/v1/auth/me",
                    headers={
                        "Cookie": f"flowspace_session={token}",
                    },
                )
            ).status_code == 401
    async with client.app.state.db.sessions() as db:
        user = await db.scalar(select(User).where(User.id == client.user_id))
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("scrypt$")
        assert await db.get(LoginSession, token_digest(original)) is not None
        assert await db.get(LoginSession, original) is None


async def test_credentials_validation_and_csrf(client):
    async with anonymous(client) as browser:
        for username in ("tester", "missing"):
            result = await browser.post(
                "/api/v1/auth/login",
                json={
                    "username": username,
                    "password": "incorrect-password-value",
                },
            )
            assert result.status_code == 401
            assert result.json()["error"]["message"] == "用户名或密码不正确"
        duplicate = await browser.post(
            "/api/v1/auth/register",
            json={
                "username": "TESTER",
                "password": PASSWORD,
            },
        )
        assert duplicate.status_code == 409
        invalid = await browser.post(
            "/api/v1/auth/register",
            json={
                "username": "new_user",
                "password": "abc12",
            },
        )
        assert invalid.status_code == 422 and "abc12" not in invalid.text
        for endpoint in ("login", "register"):
            result = await browser.post(
                f"/api/v1/auth/{endpoint}",
                headers={
                    "Origin": "https://evil.invalid",
                },
                json={"username": "tester", "password": PASSWORD},
            )
            assert result.status_code == 403
    for headers in (
        {"X-CSRF-Token": ""},
        {"X-CSRF-Token": "wrong"},
        {"Origin": "https://evil.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        result = await client.post("/api/v1/todos", headers=headers, json={"title": "forged"})
        assert result.status_code == 403
    assert (
        await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": ""})
    ).status_code == 403
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_user_isolation_and_legacy_tasks(client):
    task = (await client.post("/api/v1/todos", json={"title": "private"})).json()
    async with client.app.state.db.sessions() as db:
        legacy = Todo(title="old shared task")
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id
    async with anonymous(client) as other:
        result = await other.post(
            "/api/v1/auth/register",
            json={
                "username": "another",
                "password": PASSWORD,
            },
        )
        other.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        assert (await other.get("/api/v1/todos")).json()["items"] == []
        for task_id in (task["id"], legacy_id):
            path = f"/api/v1/todos/{task_id}"
            assert (await other.get(path)).status_code == 404
            assert (
                await other.put(path, json={"title": "stolen", "version": 1})
            ).status_code == 404
            assert (await other.delete(path + "?version=1")).status_code == 404
        forged = await other.post("/api/v1/todos", json={"title": "x", "owner_id": client.user_id})
        assert forged.status_code == 422
        assert (await other.post("/api/v1/todos", json={"title": "my own"})).status_code == 201
    assert len((await client.get("/api/v1/todos")).json()["items"]) == 1
    assert (await client.get(f"/api/v1/todos/{legacy_id}")).status_code == 404


async def test_session_expiry(client):
    async with client.app.state.db.sessions() as db:
        await db.execute(
            update(LoginSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.post("/api/v1/todos", json={"title": "expired"})).status_code == 401


@pytest.mark.parametrize("action,limit", [("login", 10), ("register", 10)])
async def test_database_rate_limit(client, action, limit):
    from app.auth.repository import AuthRepository
    from app.auth.service import AuthService
    from app.core.errors import AppError

    for _ in range(limit):
        async with client.app.state.db.sessions() as db:
            await AuthService(AuthRepository(db), 8).throttle(action, "different-ip", "unknown")
    async with client.app.state.db.sessions() as db:
        with pytest.raises(AppError) as error:
            await AuthService(AuthRepository(db), 8).throttle(action, "different-ip", "unknown")
        assert error.value.status == 429
        await db.execute(
            update(AuthRateLimit).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
        await AuthService(AuthRepository(db), 8).throttle(action, "different-ip", "unknown")


@pytest.mark.parametrize("password", ["abc12", "123456", "密码测试中文", "a" * 129])
async def test_registration_rejects_invalid_password(client, password):
    async with anonymous(client) as browser:
        result = await browser.post(
            "/api/v1/auth/register", json={"username": "policy_user", "password": password}
        )
        assert result.status_code == 422
        assert password not in result.text


@pytest.mark.parametrize("password", ["abc123", "ABC123", "abcdef"])
async def test_registration_accepts_six_characters_with_letter(client, password):
    async with anonymous(client) as browser:
        payload = {"username": "policy_user", "password": password}
        assert (await browser.post("/api/v1/auth/register", json=payload)).status_code == 201
        assert (await browser.post("/api/v1/auth/login", json=payload)).status_code == 200
