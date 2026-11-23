from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuthRateLimit, LoginSession, User


class AuthRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def user_by_name(self, username: str):
        return await self.session.scalar(select(User).where(User.username == username))

    async def resolve(self, digest: str, now: datetime):
        result = await self.session.execute(
            select(LoginSession, User)
            .join(User, LoginSession.user_id == User.id)
            .where(LoginSession.token_hash == digest, LoginSession.expires_at > now)
        )
        return result.first()

    async def revoke(self, digest: str):
        await self.session.execute(delete(LoginSession).where(LoginSession.token_hash == digest))

    async def prune(self, now: datetime):
        await self.session.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
        await self.session.execute(delete(AuthRateLimit).where(AuthRateLimit.expires_at <= now))

    async def consume_bucket(self, bucket: str, expires_at: datetime, limit: int) -> bool:
        # 唯一键 + 原子自增，多 worker 共用数据库限流。
        try:
            async with self.session.begin_nested():
                self.session.add(AuthRateLimit(bucket=bucket, attempts=0, expires_at=expires_at))
                await self.session.flush()
        except IntegrityError:
            pass
        result = await self.session.execute(
            update(AuthRateLimit)
            .where(AuthRateLimit.bucket == bucket, AuthRateLimit.attempts < limit)
            .values(attempts=AuthRateLimit.attempts + 1)
        )
        return result.rowcount == 1
