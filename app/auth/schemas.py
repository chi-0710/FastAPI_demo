from typing import Annotated, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from app.models import LoginSession, User


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_lower=True,
            min_length=3,
            max_length=32,
            pattern=r"^[a-zA-Z0-9_]+$",
        ),
    ]
    password: SecretStr = Field(min_length=1, max_length=128)


class RegistrationCredentials(Credentials):
    password: SecretStr = Field(min_length=6, max_length=128)

    @field_validator("password")
    @classmethod
    def require_letter(cls, value: SecretStr) -> SecretStr:
        if not any("a" <= char <= "z" or "A" <= char <= "Z" for char in value.get_secret_value()):
            raise ValueError("密码需包含至少一个英文字母")
        return value


class SessionIdentity(NamedTuple):
    """已认证身份：会话与用户成对出现，取代裸 tuple 与魔法下标。"""

    session: LoginSession
    user: User
