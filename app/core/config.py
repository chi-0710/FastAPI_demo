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

    @model_validator(mode="after")
    def validate_production(self):
        url = self.database_url.get_secret_value()
        if not url.startswith(("sqlite+aiosqlite://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL 必须使用 sqlite+aiosqlite 或 postgresql+asyncpg")
        if not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS 不能为空")
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
        return self
