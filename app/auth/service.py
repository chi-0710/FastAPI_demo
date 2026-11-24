import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from app.auth.repository import AuthRepository
from app.auth.schemas import SessionIdentity
from app.auth.security import password_hash, password_matches, token_digest
from app.core.errors import AppError
from app.models import LoginSession, User

SESSION_KEY = "sess:{digest}"  # digest 即 token_hash，不缓存原始 token


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        session_hours: int,
        redis: Redis | None = None,
        cache_ttl: int = 300,
    ):
        self.repo = repository
        self.session_hours = session_hours
        self.redis = redis
        self.cache_ttl = cache_ttl

    async def throttle(self, action: str, address: str, username: str):
        """会话/认证限流：有 Redis 用原子 Lua，否则沿用数据库计数。"""
        if self.redis is not None:
            from app.auth.rate_limiter import AuthRateLimiter

            limiter = AuthRateLimiter(self.redis)
            buckets = [(f"ip:{address}", 10 if action == "register" else 30)]
            if action == "login":
                buckets.append((f"user:{username}", 10))
            for identity, limit in buckets:
                await limiter.consume(f"rl:{action}:{identity}", limit)
            return

        # 原数据库实现保留为 fallback。
        now = datetime.now(UTC)
        window = int(now.timestamp()) // 900
        expiry = datetime.fromtimestamp((window + 1) * 900, UTC)
        await self.repo.prune(now)
        buckets = [(f"ip:{address}", 10 if action == "register" else 30)]
        if action == "login":
            buckets.append((f"user:{username}", 10))
        for identity, limit in buckets:
            allowed = await self.repo.consume_bucket(
                token_digest(f"{action}:{identity}:{window}"), expiry, limit
            )
            if not allowed:
                await self.repo.session.commit()
                raise AppError(429, "auth_rate_limited", "尝试次数过多，请在 15 分钟后重试")
        await self.repo.session.commit()

    async def authenticate(self, username: str, password: str, register: bool):
        if register:
            encoded = await asyncio.to_thread(password_hash, password)
            user = User(username=username, password_hash=encoded)
            self.repo.session.add(user)
            try:
                await self.repo.session.flush()
            except IntegrityError as exc:
                await self.repo.session.rollback()
                raise AppError(409, "username_unavailable", "该用户名暂不可用，请换一个") from exc
        else:
            user = await self.repo.user_by_name(username)
            valid = await asyncio.to_thread(
                password_matches, password, user.password_hash if user else None
            )
            if not valid:
                raise AppError(401, "invalid_credentials", "用户名或密码不正确")
        return user

    async def start_session(self, user: User, previous_token: str | None):
        if previous_token:
            await self.repo.revoke(token_digest(previous_token))
            if self.redis is not None:
                try:
                    await self.redis.delete(SESSION_KEY.format(digest=token_digest(previous_token)))
                except Exception:  # noqa: BLE001
                    pass
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        session = LoginSession(
            token_hash=digest,
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
        )
        self.repo.session.add(session)
        await self.repo.session.commit()
        if self.redis is not None:
            await self._cache_session(digest, session, user)
        return token, session

    async def _cache_session(self, digest: str, session: LoginSession, user: User):
        # 测试环境禁用会话缓存：测试会直接改写数据库中的 expires_at 模拟过期/撤销，
        # 此时缓存无法感知；生产/开发正常启用。
        if self.cache_ttl <= 0:
            return
        expires_at = session.expires_at
        if expires_at.tzinfo is None:  # SQLite 不保留时区，按 UTC 处理（与 schema 一致）
            expires_at = expires_at.replace(tzinfo=UTC)
        payload = {
            "token_hash": digest,
            "user_id": session.user_id,
            "username": user.username,
            "csrf_token": session.csrf_token,
            "expires_at": expires_at.isoformat(),
        }
        ttl = min(self.cache_ttl, int((expires_at - datetime.now(UTC)).total_seconds()))
        try:
            await self.redis.set(
                SESSION_KEY.format(digest=digest), json.dumps(payload), ex=max(ttl, 1)
            )
        except Exception:  # noqa: BLE001 - 缓存失败降级为 dB 回源
            pass

    async def resolve(self, token: str | None):
        digest = token_digest(token) if token else None
        if digest and self.redis is not None:
            cached = await self._resolve_from_cache(digest)
            if cached is not None:
                return cached
        row = await self.repo.resolve(digest, datetime.now(UTC)) if digest else None
        if row is None:
            raise AppError(401, "unauthorized", "请先登录，或重新登录已过期的会话")
        # 释放只读事务，避免 Agent 外部调用期间持续占用数据库连接。
        await self.repo.session.commit()
        if digest and self.redis is not None:
            await self._cache_session(digest, row.LoginSession, row.User)
        return SessionIdentity(row.LoginSession, row.User)

    async def _resolve_from_cache(self, digest: str):
        try:
            raw = await self.redis.get(SESSION_KEY.format(digest=digest))
        except Exception:  # noqa: BLE001 - 缓存故障回源数据库
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
                return None
        except (ValueError, KeyError, TypeError):
            return None

        # 返回与 AuthRepository.resolve 相同的 SessionIdentity 结构。
        from app.models import LoginSession as LS
        from app.models import User as U

        session = LS(
            token_hash=payload["token_hash"],
            user_id=payload["user_id"],
            csrf_token=payload["csrf_token"],
            expires_at=datetime.fromisoformat(payload["expires_at"]),
        )
        user = U(id=payload["user_id"], username=payload["username"], password_hash="")
        return SessionIdentity(session, user)

    async def logout(self, session: LoginSession):
        await self.repo.revoke(session.token_hash)
        await self.repo.session.commit()
        if self.redis is not None:
            try:
                await self.redis.delete(SESSION_KEY.format(digest=session.token_hash))
            except Exception:  # noqa: BLE001
                pass
