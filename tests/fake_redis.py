"""进程内最小 Redis 替身，仅覆盖应用用到的命令，供测试零外部依赖运行。

支持：get/set(ex,nx)/delete/incr/ping/register_script(Lua 限流)/aclose。
不支持持久化与并发原子性之外的完整 Redis 语义，测试用途足够。
"""

import asyncio
import time


class _FakePubSub:
    async def subscribe(self, channel):
        pass

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(timeout or 0.01)
        return None

    async def unsubscribe(self, channel):
        pass

    async def aclose(self):
        pass


class _Script:
    def __init__(self, client, script):
        self.client = client
        self.script = script

    async def __call__(self, keys=None, args=None):
        # 仅识别限流 Lua：INCR + 首次 EXPIRE + 超限判断。
        key = keys[0]
        limit, ttl = int(args[0]), int(args[1])
        async with self.client._lock:
            self.client._expire_if_needed(key)
            value = int(self.client._store.get(key, 0)) + 1
            self.client._store[key] = str(value)
            if value == 1:
                self.client._expires[key] = time.monotonic() + ttl
        return 1 if value <= limit else 0


class FakeRedis:
    def __init__(self):
        self._store = {}
        self._expires = {}
        self._lock = asyncio.Lock()

    def _expire_if_needed(self, key):
        exp = self._expires.get(key)
        if exp is not None and time.monotonic() > exp:
            self._store.pop(key, None)
            self._expires.pop(key, None)

    async def ping(self):
        return True

    async def get(self, key):
        async with self._lock:
            self._expire_if_needed(key)
            return self._store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        async with self._lock:
            self._expire_if_needed(key)
            if nx and key in self._store:
                return None
            self._store[key] = str(value)
            if ex is not None:
                self._expires[key] = time.monotonic() + ex
            else:
                self._expires.pop(key, None)
            return True

    async def delete(self, *keys):
        async with self._lock:
            removed = 0
            for key in keys:
                if key in self._store:
                    removed += 1
                self._store.pop(key, None)
                self._expires.pop(key, None)
            return removed

    async def incr(self, key):
        async with self._lock:
            self._expire_if_needed(key)
            value = int(self._store.get(key, 0)) + 1
            self._store[key] = str(value)
            return value

    def register_script(self, script):
        return _Script(self, script)

    # --- Streams（简化：全局列表 + 消费组游标）---
    async def xgroup_create(self, name, group, id="0", mkstream=False):
        key = f"__group__{name}:{group}"
        async with self._lock:
            if key in self._store:
                raise Exception("BUSYGROUP Consumer Group name already exists")
            self._store[key] = "0"
        return True

    async def xadd(self, name, fields):
        key = f"__stream__{name}"
        async with self._lock:
            msgs = self._store.setdefault(key, [])
            mid = f"{len(msgs) + 1}-0"
            msgs.append((mid, dict(fields)))
            return mid

    async def xreadgroup(self, group, consumer, streams, count=1, block=2000):
        name = next(iter(streams))
        key = f"__stream__{name}"
        out = []
        async with self._lock:
            msgs = self._store.get(key, [])
            # 简单实现：每次返回尚未交付给任何消费者的消息。
            delivered = set(self._store.get(f"__delivered__{name}", []))
            picked = []
            for mid, fields in msgs:
                if mid not in delivered:
                    picked.append((mid, fields))
                    if len(picked) >= count:
                        break
            if picked:
                self._store[f"__delivered__{name}"] = list(delivered) + [m[0] for m in picked]
                out = [(name, picked)]
        if not out:
            await asyncio.sleep(block / 1000)
        return out

    async def xack(self, name, group, message_id):
        return 1

    # --- Hash ---
    async def hset(self, key, mapping=None, values=None):
        async with self._lock:
            store = self._store.setdefault(f"__hash__{key}", {})
            if mapping:
                for k, v in mapping.items():
                    store[k] = str(v)
            if values:
                for i in range(0, len(values), 2):
                    store[values[i]] = str(values[i + 1])
        return 1

    async def hgetall(self, key):
        async with self._lock:
            return dict(self._store.get(f"__hash__{key}", {}))

    # --- Pub/Sub（简化：发布即丢弃，SSE 测试用首帧状态）---
    async def publish(self, channel, message):
        return 0

    def pubsub(self):
        return _FakePubSub()

    async def aclose(self):
        pass

    # 测试辅助：模拟过期/故障
    def fast_forward(self, seconds):
        for key in list(self._expires):
            self._expires[key] -= seconds
