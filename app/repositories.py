from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Todo


class TodoRepository:
    """只负责数据库访问；事务提交由 service 控制。"""

    def __init__(self, session: AsyncSession, owner_id: str):
        self.session = session
        self.owner_id = owner_id

    async def list(self, completed: bool | None, limit: int, offset: int):
        query = (
            select(Todo)
            .where(Todo.owner_id == self.owner_id)
            .order_by(Todo.created_at, Todo.id)
            .limit(limit)
            .offset(offset)
        )
        if completed is not None:
            query = query.where(Todo.completed == completed)
        return list((await self.session.scalars(query)).all())

    async def get(self, todo_id: str):
        return await self.session.scalar(
            select(Todo).where(Todo.id == todo_id, Todo.owner_id == self.owner_id)
        )

    def add(self, todo: Todo):
        self.session.add(todo)

    async def delete(self, todo: Todo):
        await self.session.delete(todo)
