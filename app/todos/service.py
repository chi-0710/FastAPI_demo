from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AppError
from app.models import Todo
from app.todos.cache import TodoCache
from app.todos.idempotency import IdempotencyGuard
from app.todos.repository import TodoRepository
from app.todos.schemas import TodoCreate, TodoRead, TodoReplace


class TodoService:
    """业务规则：归属、版本校验、事务提交；缓存与幂等由协作对象承担。"""

    def __init__(
        self,
        repository: TodoRepository,
        *,
        cache: TodoCache | None = None,
        idempotency: IdempotencyGuard | None = None,
    ):
        self.repo = repository
        self.cache = cache or TodoCache(None, repository.owner_id, 60)
        self.idempotency = idempotency or IdempotencyGuard(None, repository.owner_id, 86400)

    async def list(self, completed: bool | None, limit: int, offset: int):
        cached = await self.cache.get_list(completed, limit, offset)
        if cached is not None:
            return [TodoRead.model_validate(item) for item in cached]
        items = await self.repo.list(completed, limit, offset)
        payload = [TodoRead.model_validate(item).model_dump(mode="json") for item in items]
        await self.cache.set_list(completed, limit, offset, payload)
        return items

    async def _require_entity(self, todo_id: str) -> Todo:
        """写路径读库，拿到可提交的 ORM 实体并做归属校验。"""
        todo = await self.repo.get(todo_id)
        if todo is None:
            raise AppError(404, "todo_not_found", "待办不存在")
        return todo

    async def get(self, todo_id: str):
        cached = await self.cache.get_detail(todo_id)
        if cached is not None:
            return TodoRead.model_validate(cached)
        todo = await self._require_entity(todo_id)
        data = TodoRead.model_validate(todo).model_dump(mode="json")
        await self.cache.set_detail(todo_id, data)
        return todo

    async def create(self, data: TodoCreate, idempotency_key: str | None = None):
        if idempotency_key:
            acquired, replay = await self.idempotency.begin(idempotency_key)
            if not acquired:
                return replay
        todo = Todo(owner_id=self.repo.owner_id, **data.model_dump())
        self.repo.add(todo)
        try:
            await self.commit()
        except Exception:
            if idempotency_key:
                await self.idempotency.abort(idempotency_key)
            raise
        if idempotency_key:
            result = TodoRead.model_validate(todo).model_dump_json()
            await self.idempotency.commit(idempotency_key, result)
        await self.cache.invalidate()
        return todo

    async def replace(self, todo_id: str, data: TodoReplace):
        todo = await self._require_entity(todo_id)
        self.check_version(todo, data.version)
        todo.title = data.title
        todo.completed = data.completed
        todo.version += 1
        await self.commit()
        await self.cache.invalidate()
        return todo

    async def delete(self, todo_id: str, version: int):
        todo = await self._require_entity(todo_id)
        self.check_version(todo, version)
        await self.repo.delete(todo)
        await self.commit()
        await self.cache.invalidate()

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
