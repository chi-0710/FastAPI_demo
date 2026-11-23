import os

import httpx
import pytest
from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.main import create_app

PASSWORD = "test-only-long-password-123"


@pytest.fixture
def database_url(tmp_path):
    # TEST_DATABASE_URL 只能指向可销毁的独立测试库：fixture 会删除其中的表。
    url = os.getenv("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    yield url
    command.downgrade(config, "base")


@pytest.fixture
def settings(database_url):
    return Settings(_env_file=None, app_env="test", database_url=database_url)


@pytest.fixture
async def client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            client.app = app
            result = await client.post(
                "/api/v1/auth/register",
                json={
                    "username": "tester",
                    "password": PASSWORD,
                },
            )
            assert result.status_code == 201, result.text
            client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
            client.user_id = result.json()["user"]["id"]
            yield client
