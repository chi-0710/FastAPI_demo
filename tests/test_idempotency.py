"""IdempotencyGuard 的占位、回放与失败关闭验证。"""

import pytest

from app.core.errors import AppError
from app.todos.idempotency import IdempotencyGuard


async def test_begin_acquire_then_replay(client):
    redis = client.app.state.redis.client
    guard = IdempotencyGuard(redis, "owner", 60)

    acquired, replay = await guard.begin("key-1")
    assert acquired is True and replay is None

    # commit 后再次 begin 回放结果
    result_json = (
        '{"id":"11111111-1111-1111-1111-111111111111","title":"t",'
        '"completed":false,"version":1,"created_at":"2024-01-01T00:00:00Z"}'
    )
    await guard.commit("key-1", result_json)
    acquired, replay = await guard.begin("key-1")
    assert acquired is False
    assert str(replay.id) == "11111111-1111-1111-1111-111111111111"


async def test_begin_conflict_when_processing(client):
    redis = client.app.state.redis.client
    guard = IdempotencyGuard(redis, "owner", 60)
    await guard.begin("key-2")
    with pytest.raises(AppError) as err:
        await guard.begin("key-2")
    assert err.value.status == 409


async def test_begin_unavailable_without_redis(client):
    guard = IdempotencyGuard(None, "owner", 60)
    with pytest.raises(AppError) as err:
        await guard.begin("key-3")
    assert err.value.status == 503
