import pytest
from pydantic import ValidationError

from app.core.config import Settings

ORIGIN = "http://127.0.0.1:5173"


async def test_backend_has_no_frontend(client):
    for path in ["/", "/assets/app.js", "/ui-config"]:
        assert (await client.get(path)).status_code == 404
    client.cookies.clear()
    assert (await client.get("/api/v1/capabilities")).json() == {
        "docs_url": "/docs",
        "agent_mode": "disabled",
    }
    assert (await client.get("/api/v1/todos")).status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_allowed_preflight(client, method):
    client.cookies.clear()
    response = await client.options(
        "/api/v1/todos",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "x-csrf-token,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_rejected_origin_and_header(client):
    response = await client.options(
        "/api/v1/todos",
        headers={
            "Origin": "https://untrusted.invalid",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    response = await client.options(
        "/api/v1/todos",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-unapproved",
        },
    )
    assert response.status_code == 400


async def test_cors_success_auth_and_unhandled_error(client):
    response = await client.get("/api/v1/todos", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]
    response = await client.get(
        "/api/v1/todos", headers={"Origin": ORIGIN, "Cookie": "flowspace_session=invalid"}
    )
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ORIGIN

    class BrokenAgent:
        async def run(self, *args):
            raise RuntimeError("secret-test-value")

    client.app.state.agent = BrokenAgent()
    response = await client.post(
        "/api/v1/agent/invoke", headers={"Origin": ORIGIN}, json={"prompt": "test"}
    )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret-test-value" not in response.text


@pytest.mark.parametrize("origin", ["*", "https://example.com/path", "https://u:p@example.com"])
def test_cors_config_rejects_unsafe_origins(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins=[origin])
