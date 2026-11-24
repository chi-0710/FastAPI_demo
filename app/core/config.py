from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量优先于 .env；生产环境启动时拒绝不安全的配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_env: Literal["development", "test", "production"] = "development"
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./demo.db")
    session_hours: int = Field(default=8, ge=1, le=168)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    agent_backend: Literal["disabled", "mock"] = "disabled"
    agent_timeout_seconds: float = Field(default=10, gt=0, le=60)
    # Redis 是加速/协调层，不取代 PostgreSQL 作为事实源。
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    redis_enabled: bool = True
    cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    session_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    rate_limit_per_minute: int = Field(default=60, ge=1)
    idempotency_ttl_seconds: int = Field(default=86400, ge=60)
    agent_reconcile_interval_seconds: int = Field(default=30, ge=5, le=3600)
    agent_reconcile_stale_seconds: int = Field(default=120, ge=30, le=86400)

    @model_validator(mode="after")
    def validate_production(self):
        url = self.database_url.get_secret_value()
        if not url.startswith(("sqlite+aiosqlite://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL 必须使用 sqlite+aiosqlite 或 postgresql+asyncpg")
        if not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS 不能为空")
        redis_url = self.redis_url.get_secret_value()
        if not redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL 必须使用 redis:// 或 rediss://")
        if self.session_cache_ttl_seconds > self.session_hours * 3600:
            raise ValueError("SESSION_CACHE_TTL_SECONDS 不能超过会话有效期")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("CORS_ORIGINS 必须为明确的 HTTP(S) origin，不含路径或通配符")
        if self.app_env == "production":
            if not url.startswith("postgresql+asyncpg://"):
                raise ValueError("production 必须使用 PostgreSQL")
            if "*" in self.allowed_hosts:
                raise ValueError("production 必须明确指定 ALLOWED_HOSTS")
            if self.agent_backend == "mock":
                raise ValueError("模拟 Agent 仅可用于开发和测试")
            if not self.redis_enabled:
                raise ValueError("production 必须启用 Redis")
            if not redis_url.startswith("rediss://") or "@" not in redis_url:
                raise ValueError("production 必须使用带认证的 rediss://")
        return self
