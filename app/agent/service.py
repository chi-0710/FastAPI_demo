import asyncio
from typing import Protocol

from app.agent.schemas import AgentResponse
from app.core.errors import AppError


class AgentRunner(Protocol):
    """模型 SDK / Agent 框架的适配边界，不接触 HTTP 和数据库 Session。"""

    async def run(self, prompt: str, *, request_id: str) -> AgentResponse: ...


class MockAgent:
    async def run(self, prompt: str, *, request_id: str) -> AgentResponse:
        return AgentResponse(provider="mock", output=f"[模拟结果，未调用模型] 收到任务：{prompt}")


class AgentService:
    def __init__(self, runner: AgentRunner | None, timeout: float):
        self.runner = runner
        self.timeout = timeout

    async def run(self, prompt: str, request_id: str):
        if self.runner is None:
            raise AppError(503, "agent_disabled", "尚未配置 Agent 执行器")
        try:
            async with asyncio.timeout(self.timeout):
                return await self.runner.run(prompt, request_id=request_id)
        except TimeoutError as exc:
            raise AppError(504, "agent_timeout", "Agent 执行超时") from exc
