"""TodoCache 与版本化失效的单元验证。"""

from app.todos.cache import TodoCache


async def test_cache_none_redis_degrades_to_none(client):
    cache = TodoCache(None, "owner", 60)
    assert await cache.get_list(None, 10, 0) is None
    assert await cache.get_detail("id") is None
    # 无 Redis 时 set/invalidate 都是空操作，不应抛错。
    await cache.set_list(None, 10, 0, [])
    await cache.set_detail("id", {})
    await cache.invalidate()


async def test_cache_invalidate_bumps_version(client):
    redis = client.app.state.redis.client
    cache = TodoCache(redis, "owner", 60)
    before = redis._store.get("ver:todos:owner")
    assert before is None
    await cache.invalidate()
    assert redis._store.get("ver:todos:owner") == "1"
    await cache.invalidate()
    assert redis._store.get("ver:todos:owner") == "2"


async def test_cache_set_and_get_detail_roundtrip(client):
    redis = client.app.state.redis.client
    cache = TodoCache(redis, "owner", 60)
    await cache.set_detail("todo-1", {"title": "缓存"})
    cached = await cache.get_detail("todo-1")
    assert cached == {"title": "缓存"}
    # 未写入的键返回 None
    assert await cache.get_detail("todo-missing") is None
