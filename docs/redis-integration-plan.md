# Redis 接入方案（已实现）

> 目标：在不破坏现有「分层单体 + 异步 SQLAlchemy + PostgreSQL 持久化」边界的前提下，引入 Redis 解决三个真实痛点：① 认证/限流的高频读写压在数据库上；② 多 worker 下幂等与限流需要跨进程共享状态；③ Agent 长任务需要持久化队列与状态缓存。Redis **不取代数据库**，所有业务事实仍以 PostgreSQL 为准。
>
> **实施状态**：P0（缓存 / 会话缓存 / Lua 限流）与 P1（幂等键 / Agent Streams 队列 + SSE）均已落地；P2（全局网关限流 / Agent 响应缓存）与 P3（向量库）保留为后续演进。测试用进程内内存替身（`tests/fake_redis.py`），全量 47 项测试通过，`ruff check` 与 `ruff format --check` 均通过。

## 0. 设计原则（务必先读）

1. **数据库仍是唯一事实源（source of truth）**：用户、会话、Todo、任务状态都先在 PostgreSQL 落库；Redis 只做加速、限流计数、幂等标记和队列。
2. **Redis 是"可丢失"的加速层**：任何 Redis 数据丢失都不能导致数据错乱或安全问题——最多退化为较慢路径（回源 DB / 拒绝服务 / 重放 outbox）。
3. **不跨任务共享 async 客户端**：沿用现有 `app/db.py` + `get_session` 的模式，一个请求一个逻辑客户端的获取入口，lifespan 管理连接池，shutdown 关闭。
4. **密钥不进日志**：沿用 `SecretStr` 与 `JsonFormatter` 脱敏约定；Redis URL 中的密码不得出现在日志或错误响应。
5. **可降级（fail-open / fail-closed 按场景区分）**：缓存不可用时回源数据库；限流不可用时fail-closed（拒绝请求）避免被绕过；幂等不可用时fail-closed。
6. **测试环境零外部依赖**：沿用 `Settings` 注入 + lifespan fixture，测试用 fakeredis 或内存 stub，CI 不依赖真实 Redis（可选加一行 Redis service 做集成测试）。

---

## 1. 功能清单（按优先级）

| 优先级 | 功能 | 解决的痛点 | Redis 数据结构 | 降级策略 |
| --- | --- | --- | --- | --- |
| P0 | API 响应缓存（Todo 列表/详情） | 列表查询重复打库、热点数据 | `String` (JSON) + `ZSet`(失效索引) | fail-open 回源 DB |
| P0 | 认证会话缓存 | 每个请求 `resolve()` 都查 `login_sessions` 表 | `String` (session JSON, TTL) | fail-open 回源 DB |
| P0 | 认证限流计数 | 当前 `auth_rate_limits` 用 DB 原子自增，写压力大 | `String` + `INCR`/`EXPIRE`（Lua 原子） | fail-closed 拒绝 |
| P1 | 幂等键（POST /todos） | 网络超时重试会创建重复 Todo（架构文档明确指出的缺口） | `String` (`SET NX EX`) | fail-closed 拒绝 |
| P1 | Agent 长任务队列与状态 | 架构文档阶段三的持久队列、租约、心跳、SSE | `List`/`Stream` + `Hash` + `Pub/Sub` | fail-closed 返回 503 |
| P2 | 全局网关限流（滑动窗口） | 架构文档"尚未实现"的全站限流 | `ZSet` 滑动窗口 / Lua | fail-closed |
| P2 | Agent 响应 / 模型结果缓存 | 相同 prompt 重复调用模型烧钱 | `String` (TTL) | fail-open 回源模型 |
| P3 | 向量/记忆（RAG） | 阶段四规划 | Redis Stack / RediSearch（可选） | 不在本期实现 |

> 面试讲解重点：P0 三项直接替换/减轻现有数据库路径，P1 两项恰好填上架构文档自己承认的两个"尚未实现"缺口（幂等键、持久队列），形成"设计文档 → 落地实现"的闭环。

---

## 2. 总体架构与改动全景

```
请求 → TrustedHost → request_context(middleware)
     → [新增] rate_limit_middleware (Lua 滑动窗口, fail-closed)
     → require_session依赖
          → AuthService.resolve()
               → [新增] RedisSessionCache.get() 命中直接返回
               → 未命中回源 AuthRepository.resolve(DB) 再回填
     → TodoService
          → [新增] TodoCache（service 层，写后主动失效）
          → TodoRepository (DB, 不变)
     → POST /todos
          → [新增] 幂等装饰器 SET key NX EX（service 层）
     → POST /agent/runs (新增)
          → DB 落 run 记录 → outbox → [新增] Redis Streams 投递
          → worker 消费 → 状态写 DB + Hash 缓存 → SSE 推送(Pub/Sub)
lifespan: Database + RedisPool + AgentService + [新增] AgentWorker
health/ready: 增加 Redis PING 检查
```

新增文件（建议）：

```
app/redis.py                # RedisPool 封装 + get_redis 依赖（对齐 db.py）
app/core/cache.py           # 通用缓存辅助：序列化、TTL、失效索引、键构造
app/auth/session_cache.py   # AuthService 会话缓存读写
app/auth/rate_limiter.py    # 认证/全局限流 Lua 脚本封装
app/cache/todos.py          # Todo 列表/详情缓存（service 层调用）  ——或放 app/core/cache.py 内
app/idempotency.py          # 幂等键抽象（SET NX EX）
app/agent/queue.py          # Streams 队列 + 状态 Hash + Pub/Sub
app/agent/worker.py         # 后台消费循环（lifespan 启停）
tests/test_redis_*.py       # 各功能测试（fakeredis）
```

修改文件：

```
app/core/config.py          # 新增 redis_* 配置 + 生产校验
app/main.py                 # lifespan 创建/关闭 Redis、worker；health 注入
app/api/dependencies.py     # get_redis 依赖、缓存/限流注入
app/services.py             # create/replace/delete 加缓存失效与幂等
app/auth/service.py         # resolve 走缓存；throttle 走 Redis（保留 DB 兜底或迁移）
app/api/routes.py           # ready 检查 Redis；新增 /agent/runs 路由；POST 幂等头
requirements*.txt / compose.yaml / .env.example / docs/architecture.md
```

---

## 3. 配置项设计（app/core/config.py）

沿用 `SecretStr` + `model_validator(mode="after")` 的写法：

```python
# 新增字段
redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
redis_enabled: bool = True  # 测试可整体关闭，走纯 DB 路径
cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
session_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)  # <= session_hours
rate_limit_per_minute: int = Field(default=60, ge=1)
idempotency_ttl_seconds: int = Field(default=86400, ge=60)
agent_stream_maxlen: int = Field(default=10000, ge=100)
```

新增校验（放在 `validate_production`）：

- URL 必须以 `redis://` 或 `rediss://`（TLS）开头；production 必须用 `rediss://` 且带密码，禁止无认证公网 Redis。
- `redis_enabled=False` 仅允许 development/test；production 强制为 True（安全相关路径不能无人托管降级）。
- `session_cache_ttl_seconds <= session_hours * 3600`，避免缓存有效期超过会话本身。

`.env.example` 增加：

```
REDIS_URL=redis://:change-me@localhost:6379/0
REDIS_ENABLED=true
RATE_LIMIT_PER_MINUTE=60
```

`compose.yaml` 增加 `redis` 服务（`redis:7-alpine`，`requirepass`，`appendonly yes`，healthcheck `redis-cli ping`），`api`/`migrate` 的 `x-app` 环境里注入 `REDIS_URL`，`api` 增加 `depends_on: redis: service_healthy`。

> 生产建议：有认证 + TLS（rediss://）+ `appendonly yes` + 内存上限与淘汰策略（`maxmemory-policy volatile-lru`，仅淘汰带 TTL 的键）。

---

## 4. 客户端生命周期（app/redis.py，对齐 app/db.py）

```python
import redis.asyncio as redis


class RedisPool:
    def __init__(self, url: str, *, decode_responses=True):
        self.client = redis.from_url(
            url,
            decode_responses=decode_responses,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            health_check_interval=30,
            max_connections=50,
        )

    async def ping(self):
        await self.client.ping()

    async def close(self):
        await self.client.aclose()  # 关闭连接池，等同 dispose
```

- `app/main.py` lifespan：`app.state.redis = RedisPool(...)`；`await app.state.redis.ping()` 启动自检；`finally` 里 `await app.state.redis.close()`（与 `app.state.db.close()` 并列）。
- `app/api/dependencies.py` 增加：

```python
async def get_redis(request: Request):
    return request.app.state.redis.client  # 连接由池管理，非每请求新建
```

- **不**把 Redis 客户端塞进 SQLAlchemy Session；两者职责分离，依赖注入分别获取。

---

## 5. 功能详细设计

### 5.1 Todo 列表/详情缓存（P0，fail-open）

- **Key 规范**（统一前缀，便于扫描与清理）：
  - 列表：`tl:{owner_id}:{completed}:{limit}:{offset}` → JSON 数组
  - 详情：`td:{owner_id}:{todo_id}` → JSON 对象
  - 失效索引：`inv:owner:{owner_id}` → `ZSet`，member = 业务 key，score = 过期时间戳
- **读路径**（`TodoService.list/get`）：先 `GET` 缓存；命中反序列化返回；未命中查库 → `SET key value EX cache_ttl` → 同时 `ZADD inv:owner:...` 记录，供失效用。
- **写路径**（`create/replace/delete`）：沿用现有 service `commit()` 成功后调用 `invalidate_owner(owner_id)`：
  - `ZRANGEBYSCORE inv:owner:{owner} 0 now` 取到期/全部 member → `DEL` → `ZREM`。
  - 更稳妥方案：用版本号 key `ver:{owner}`（`INCR`），缓存 key 带版本号 `tl:{owner}:{ver}:...`，写后 `INCR ver` 使旧 key 自然失效（避免扫库，面试可讲"版本化缓存键"思想）。
- **一致性**：先提交 DB 再失效缓存（cache-aside + write-invalidate）；窗口期内可能返回旧值，但 TTL 短（60s）可接受。
- **归属隔离**：key 必带 `owner_id`，与 `TodoRepository(owner_id)` 一致，杜绝跨用户缓存串数据。
- **防击穿**：单飞（singleflight）用按 key 的 `asyncio.Lock` 字典，或简单方案：缓存空值也写入（短 TTL）；本 demo 建议用 singleflight 讲解。
- **序列化**：Pydantic `TodoRead.model_dump_json()`，读出用 `TodoRead.model_validate_json()`；与 schema 契约复用，不手写 dict。

### 5.2 认证会话缓存（P0，fail-open）

- 现状：`AuthService.resolve()` 每请求用 `token_digest(token)` 查 `login_sessions` 表。
- 方案：**只缓存 digest → (user_id, csrf_token, expires_at)**，不缓存原始 token。
  - Key：`sess:{token_digest}`，Value：JSON，TTL = min(session_cache_ttl, 剩余有效期)。
  - `resolve()` 先查 Redis；命中则构造轻量 `LoginSession`/user 引用直接返回；未命中回源 DB，成功后回填。
  - `start_session()`：DB 提交后 `SET sess:{digest} ... EX ttl`。
  - `logout()` / `revoke()`：DB 删除后 `DEL sess:{digest}`（双删或先删缓存再删 DB，避免并发复活：删 DB → DEL → 再次 DEL 延迟双删可选）。
  - 限流/会话码不在缓存中存明文；`csrf_token` 本就在 DB，缓存它可接受，但要确保 key 用 digest。
- **注意**：现有 `resolve()` 里有一句"释放只读事务"的 `commit()`；走缓存命中后不再开 DB Session，进一步减轻连接池压力。

### 5.3 认证限流迁移到 Redis（P0，fail-closed）

- 现状：`AuthRepository.consume_bucket()` 用 `begin_nested` + 唯一键 + 条件 UPDATE 做 15 分钟窗口计数（`AuthService.throttle`）。
- Redis 方案：用 Lua 脚本保证"读计数 + 自增 + 设过期"原子：

```lua
-- rate_limit.lua：KEYS[1]=bucket key, ARGV[1]=limit, ARGV[2]=ttl_seconds
local current = tonumber(redis.call('INCR', KEYS[1]))
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if current > tonumber(ARGV[1]) then return 0 end
return 1
```

- `AuthRateLimiter` 类封装 `script(keys=[bucket], args=[limit, 900])`；按现有 `ip:{address}` / `user:{username}` 维度、注册/登录不同 limit 保持不变。
- **窗口对齐**：DB 版本用 `window = ts // 900` 固定窗口，Redis 用 `EXPIRE` 从首次请求起 900s，属于滑动起始固定窗口，语义等价且更平滑。
- **迁移策略**：保留 `AuthRateLimit` 表与旧代码作为 fallback；配置开关 `RATE_LIMIT_BACKEND=redis|db`，默认 redis；Redis 异常 → fail-closed（返回 429/503），不静默放行。
- **好处**：去掉 `prune()` 每请求删表和嵌套事务，显著降低写放大。

### 5.4 幂等键（P1，fail-closed）

- 补齐架构文档承认的缺口：`POST /api/v1/todos` 接受 `Idempotency-Key` 请求头。
- 流程（`TodoService.create`）：

```
key = idem:{user_id}:{sha256(idempotency_key)}
result = SET key <serialized response> NX EX 86400
if result is None:
    stored = GET key
    if stored: return stored        # 重放第一次的结果
    else: raise 409 "idempotency_in_progress"   # 并发中
# 首次：DB commit → SET key <response>（覆盖，带 TTL）
# 提交失败：DEL key 回滚标记
```

- 首次写用 `SET NX EX` 占位（"processing"），提交成功后回写真实响应；DB 失败删除 key 允许重试。
- key 必须带 `user_id` 前缀（与 owner 隔离一致）；明文幂等键用 SHA-256 摘要，不直接暴露。
- 幂等与缓存失效顺序：DB commit → 写幂等结果 → 失效 Todo 缓存。

### 5.5 Agent 长任务队列与状态（P1，对齐架构文档阶段三）

- **API**：新增 `POST /api/v1/agent/runs`（202 返回 `run_id`）、`GET /api/v1/agent/runs/{id}`、`POST /api/v1/agent/runs/{id}/cancel`、`GET /api/v1/agent/runs/{id}/events`（SSE）。
- **事实源**：run 记录落 PostgreSQL 表（owner、status、version、deadline、idempotency_key、budget、error_code、timestamps）——遵循文档"不只放在进程内字典"。新建一个 Alembic revision（版本号需同步更新 `routes.py` 的 readiness 检查，从 `0002` 改成新值）。
- **Redis 承担**：
  - **队列**：`XADD agent:runs MAXLEN ~ 10000 * run_id <id>`（Streams，支持消费组 `XGROUP CREATE`、`XREADGROUP`、`XACK`；比 List 多消费者更稳健）。
  - **状态缓存**：`HSET run:{id} status/version/updated_at`，GET 接口先读 Hash，未命中回源 DB。
  - **租约与心跳**：worker `XCLAIM` 认领超时消息；处理中定期 `HSET run:{id} heartbeat_ts`，崩溃由别的 worker 抢租约重跑（至少一次投递，工具副作用必须幂等）。
  - **SSE / Pub-Sub**：worker 状态变更 `PUBLISH run:{id}:events <event json>`；SSE 端点 `SUBSCRIBE` 并带序号，断线用 `GET /runs/{id}` 补状态（事件不丢，因为事实在 DB）。
  - **outbox 投递器**：DB 写 run + outbox 同事务 → 独立投递器把 run_id `XADD` 进 Stream，解决"DB 成功但消息没发"的双写问题（文档原文要求）。
- **worker 生命周期**：在 `app/main.py` lifespan 里 `asyncio.create_task(worker.run())`，`finally` 里 `cancel()` + `await`；worker 用独立 DB Session（遵守"不要共享 Session"约束），每次调用 `AgentService.run()` 带超时。
- **安全**：run 按 `owner_id` 隔离；SSE 和 GET 都校验 `require_session` 的 user_id 等于 run.owner；取消先写 DB 标记再让 worker 在检查点停止。

### 5.6 全局网关限流（P2，fail-closed）

- 中间件 `rate_limit_middleware`：按 IP（登录用户可按 user_id）做每分钟滑动窗口。
- Lua + `ZSet`：`ZREMRANGEBYSCORE` 清理窗口外 → `ZCARD` 计数 → 未超限 `ZADD now member` + `EXPIRE 60`。
- 返回 `429` + `Retry-After`；读健康检查和静态资源跳过。与现有认证限流叠加，分别兜底。

### 5.7 Agent 响应缓存（P2，fail-open）

- Key：`agent:{sha256(prompt + model)}`，Value：`AgentResponse` JSON，TTL 可配置（如 300s）。
- `AgentService.run()` 调用模型前先查缓存；命中直接返回（记录 `cache_hit` 日志字段，不记录 prompt 内容）。适合确定性/只读 prompt；涉及工具副作用的调用不缓存。

---

## 6. 关键一致性与安全决策（面试可深挖的点）

1. **Cache-aside + 写后失效**：更新走 DB → 失效缓存，而非写穿；好处是缓存故障不影响写入。
2. **为什么不用 Redis 做会话事实源**：现有 `login_sessions` 已提供可审计、可重启恢复的持久化；缓存 + 短 TTL 是加速而非替换，避免 Redis 持久化配置不当导致大规模掉线。
3. **限流 fail-closed vs 缓存 fail-open**：限流是安全边界，缓存挂了放行等于限流失效；列表缓存挂了回源只是慢。按功能区分降级策略。
4. **多 worker 原子性**：限流/幂等用 Lua 或 `SET NX`，不依赖 Python 侧锁；这是从 DB 条件 UPDATE 思维到 Redis 原子命令的自然迁移。
5. **TTL 策略**：所有 key 必须带 TTL（禁止永久 key），配合 `volatile-lru` 防内存打满；键名带 owner_id 做租户隔离。
6. **密钥与 PII**：token 用 digest 作 key；幂等键 SHA-256；日志只记 key 类型与命中与否，不记内容。
7. **版本化缓存键 vs 主动失效**：列表用 `ver:{owner}` 版本号让旧 key 自然过期，避免 `KEYS`/`SCAN` 扫库影响线上。
8. **至少一次投递**：Streams + 消费组 + XACK 保证不丢；worker 幂等是前提，不宣称 exactly-once（与架构文档口径一致）。

---

## 7. 测试方案（沿用现有 pytest 风格）

- `tests/conftest.py` 的 `settings` fixture 增加 `redis_enabled` 开关；新增 `redis_client` fixture 用 `fakeredis` 注入 `app.state.redis`，或直接用真实 Redis 的 `TEST_REDIS_URL`（可选）。
- 新增测试：
  - `test_cache.py`：列表首次回源、二次命中、写后失效、owner 隔离（两个用户 key 不串）。
  - `test_session_cache.py`：登录回填、命中不走 DB、登出清除、TTL 过期回源。
  - `test_rate_limit.py`：连续请求第 N+1 次 429；Lua 原子性（并发 20 次仅 N 次通过）。
  - `test_idempotency.py`：同 key 两次 POST 只创建 1 条 Todo；不同用户同 key 互不干扰；并发同 key 只 1 成功。
  - `test_agent_queue.py`：投递 → worker 消费 → 状态 Hash 更新 → SSE 收到带序号事件；run 归属校验。
  - `test_redis_down.py`：模拟 Redis 抛异常，验证缓存回源、限流 fail-closed、幂等 fail-closed、health/ready 返回 503。
- 现有 `tests/test_api.py` / `test_auth.py` 不改语义，只在 Redis 关/开两种模式下各跑一遍（参数化 fixture）。

---

## 8. 分阶段落地路线图

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| 1 | 配置 + RedisPool + lifespan + health + compose + 依赖与文档 | 启动成功；`/health/ready` 检查 Redis；测试可关闭 Redis |
| 2 | P0：Todo 缓存、会话缓存、认证限流迁移 | 现有测试全绿；压测显示 DB 查询数下降；限流并发测试通过 |
| 3 | P1：幂等键 + Agent Streams 队列/状态/SSE + Alembic run 表 | 重复 POST 只产生 1 条；任务重启可恢复；SSE 断线补状态 |
| 4 | P2/P3：全局限流、Agent 响应缓存、可选 RediSearch | 指标与成本可观测；评测集验证 |

---

## 9. 面试讲述口径（一句话总结）

> "这个 demo 原本是纯 SQLAlchemy + PostgreSQL 的分层单体，我在不破坏分层边界的前提下接入 Redis：先用缓存和 Lua 原子限流把高频读写从数据库挪走（fail-open 缓存 / fail-closed 限流）；再用幂等键和 Streams 消费组补上架构文档里预留的幂等与持久任务队列能力，PostgreSQL 始终是事实源，Redis 只做加速与协调，任何 Redis 故障都能安全降级或明确拒绝。"
