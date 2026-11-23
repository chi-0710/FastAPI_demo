# 账号、会话与用户数据隔离

更新：2026-11-22。此前的共享 API Key 流程已移除；旧 `.env` 中的 API_KEY 会被忽略，可自行删除。

## 用户流程

首次访问 → 注册用户名和密码 → 自动登录 → 创建自己的任务。已有账号直接登录；刷新页面通过 `/auth/me` 恢复身份，退出时服务端撤销当前会话并清除 Cookie。默认绝对有效期 8 小时，`SESSION_HOURS` 可设 1–168；不会通过活动无限续期。

用户名为 3–32 位 ASCII 字母、数字或下划线，不区分大小写。密码为 6–128 字符，需包含至少一个英文字母（a–z 或 A–Z）。当前没有邮箱验证、忘记密码、改密、MFA、OIDC、账号删除或团队权限；不会显示尚未实现的入口。

## 接口与代码

| 接口 | 行为 |
| --- | --- |
| POST `/api/v1/auth/register` | JSON `{username,password}`；201，创建用户和会话 |
| POST `/api/v1/auth/login` | 同上；200，创建新会话，撤销本浏览器旧会话 |
| GET `/api/v1/auth/me` | 200，返回 `{user:{id,username},csrf_token}`；未登录 401 |
| POST `/api/v1/auth/logout` | 需 Cookie 和 X-CSRF-Token；204，删除当前服务端会话 |

注册与登录的响应也包含 `user` 和 `csrf_token`。只有服务端设置的 HttpOnly Cookie 是登录凭证；CSRF 令牌仅保存在前端内存，每次恢复会话重新获取。任何任务和 Agent 接口都要求有效会话，写接口额外要求 CSRF 令牌。脚本调用者需保存响应 Cookie，并传 `X-CSRF-Token`；仅发送旧 X-API-Key 不再有效。

- `app/api/auth.py`：输入契约、认证 HTTP 路由、Cookie 设置。
- `app/api/dependencies.py`：会话解析、Origin 与 CSRF 校验、注入用户身份。
- `app/auth/service.py`：注册、密码验证、会话创建／撤销、限流事务。
- `app/auth/repository.py`：用户与会话查询、限流原子更新。
- `app/auth/security.py`：密码哈希、常量时间比较、令牌摘要。
- `app/repositories.py`：按 owner_id 过滤列表和单条读写，跨用户 ID 返回 404。

密码使用 Python 标准库 scrypt（N=32768、r=8、p=3，随机盐），哈希运算在线程中执行并限制每个 worker 同时最多 4 个。参数取自 [OWASP 密码存储建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。原始密码不入库、不写日志；不存在的账号也执行一次哈希验证，错误文案统一。

会话 token 使用 32 随机字节，数据库只保存 SHA-256 摘要。Cookie 使用 HttpOnly、SameSite=Lax、Path=/；production 使用 `__Host-flowspace_session` 名称并强制 Secure，无 Domain 属性。实现参考 [OWASP 会话管理说明](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。登录时轮换本浏览器会话，其他已登录设备保持独立；退出不等于退出所有设备。

认证限流存储在数据库，多个 worker 共享计数。固定 15 分钟窗口：登录每 IP 30 次、每用户名 10 次；注册每 IP 10 次。成功和失败均计数，超限 429；过期计数与会话在后续认证请求中清理。固定窗口边界可能出现短时双倍突发，上线时还需网关请求体大小、全局流量和连接限制。可信代理范围必须明确配置，不能信任任意客户端的 X-Forwarded-For。

## 数据迁移

`0002` 新增 users、login_sessions、auth_rate_limits，以及 todos.owner_id 外键与索引。先备份数据库，再执行 `python -m alembic upgrade head`，之后重启后端。

旧任务保留原值，owner_id=NULL，普通账号不可见。不能把共享历史任务自动转给首个注册账号。如需归属迁移，应由管理员先确认所有权，再离线备份和更新指定记录。降级到 0001 会删除账号／会话数据，并移除归属字段，不能作为生产环境的无损回滚方式。

## 分离部署

前后端仍然是两个独立工程、构建产物和服务。Cookie 认证要求同站点部署：例如 `https://app.example.com` 和 `https://api.example.com`，配置准确的 CORS_ORIGINS、ALLOWED_HOSTS 和 VITE_API_BASE_URL。前端 fetch 使用 credentials:include；后端 CORS 允许 credentials，禁止通配 origin。写请求验证 Origin，并拒绝 Sec-Fetch-Site:cross-site。

本地统一使用 `http://127.0.0.1:5173` 与 `http://127.0.0.1:8000`；不要混用 localhost 和 127.0.0.1。Compose 默认 development，便于本机 HTTP 演示；公开部署必须显式 APP_ENV=production 并配置 HTTPS 和 PostgreSQL。生产 Secure Cookie 不允许用纯 HTTP 替代 HTTPS 验收。

## 验证

自动测试覆盖注册、用户名规范化、重复账号、错误密码、密码输入约束、会话轮换／撤销／过期、重启后持久化、CSRF 与来源拒绝、跨用户读写和伪造 owner_id 拒绝、历史无归属数据不可见，以及数据库认证限流。原有乐观锁、迁移一致性、Agent 和 CORS 回归测试保留。

完整容器与 PostgreSQL 验收需 Docker 引擎可用；本机验证不能替代生产容量测试与安全评审。未来 Agent 工具必须继承服务器认证的 user_id 并通过带 owner_id 的 Service 读写，不能接受模型自报用户身份。

### 本次本地验收记录

- 后端 31 项测试通过（SQLite），Ruff 检查与格式检查通过。
- 前端 7 项测试通过，Vite 生产构建通过，Compose 配置解析通过。
- 浏览器通过：未登录新建 → 注册 → 自动打开新建表单 → 创建任务 → 刷新恢复 → 退出 → 错误密码提示 → 正确密码登录恢复任务。
- 检查了默认宽度登录弹窗和 390px 窄屏注册表单；测试账号及其任务已清理。
- 本地数据库迁移前备份至 `.local/demo-before-auth-*.db`，已升级为 0002；与备份核对历史任务保持一致。
- Docker Linux 引擎未启动，未实测完整容器和 PostgreSQL；CI 保留 PostgreSQL 测试配置。
