from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AppError
from app.models import Todo
from app.repositories import TodoRepository
from app.schemas import TodoCreate, TodoReplace


class TodoService:
    def __init__(self, repository: TodoRepository):
        self.repo = repository

    async def list(self, completed: bool | None, limit: int, offset: int):
        return await self.repo.list(completed, limit, offset)

    async def get(self, todo_id: str):
        todo = await self.repo.get(todo_id)
        if todo is None:
            raise AppError(404, "todo_not_found", "待办不存在")
        return todo

    async def create(self, data: TodoCreate):
        todo = Todo(owner_id=self.repo.owner_id, **data.model_dump())
        self.repo.add(todo)
        await self.commit()
        return todo

    async def replace(self, todo_id: str, data: TodoReplace):
        todo = await self.get(todo_id)
        self.check_version(todo, data.version)
        todo.title = data.title
        todo.completed = data.completed
        todo.version += 1
        await self.commit()
        return todo

    async def delete(self, todo_id: str, version: int):
        todo = await self.get(todo_id)
        self.check_version(todo, version)
        await self.repo.delete(todo)
        await self.commit()

    @staticmethod
    def check_version(todo: Todo, version: int):
        if todo.version != version:
            raise AppError(409, "version_conflict", "记录已更新，请重新读取后再提交")

    async def commit(self):
        try:
            await self.repo.session.commit()
        except StaleDataError as exc:
            await self.repo.session.rollback()
            raise AppError(409, "version_conflict", "记录已更新，请重新读取后再提交") from exc
