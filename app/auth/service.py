import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.auth.repository import AuthRepository
from app.auth.security import password_hash, password_matches, token_digest
from app.core.errors import AppError
from app.models import LoginSession, User


class AuthService:
    def __init__(self, repository: AuthRepository, session_hours: int):
        self.repo = repository
        self.session_hours = session_hours

    async def throttle(self, action: str, address: str, username: str):
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
        token = secrets.token_urlsafe(32)
        session = LoginSession(
            token_hash=token_digest(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
        )
        self.repo.session.add(session)
        await self.repo.session.commit()
        return token, session

    async def resolve(self, token: str | None):
        row = await self.repo.resolve(token_digest(token), datetime.now(UTC)) if token else None
        if row is None:
            raise AppError(401, "unauthorized", "请先登录，或重新登录已过期的会话")
        # 释放只读事务，避免 Agent 外部调用期间持续占用数据库连接。
        await self.repo.session.commit()
        return row

    async def logout(self, session: LoginSession):
        await self.repo.revoke(session.token_hash)
        await self.repo.session.commit()
