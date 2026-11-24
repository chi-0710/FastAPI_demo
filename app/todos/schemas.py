from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class TodoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    completed: bool = False


class TodoReplace(TodoCreate):
    version: int = Field(ge=1, description="读取记录时获得的版本号，用于防止覆盖他人修改")


class TodoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    completed: bool
    version: int
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value):
        # SQLite 不保留时区；本应用写入时间始终为 UTC。
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class TodoPage(BaseModel):
    items: list[TodoRead]
    limit: int
    offset: int
