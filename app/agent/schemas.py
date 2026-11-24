from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class AgentResponse(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    output: str
