from decimal import Decimal
from typing import Protocol
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.db.models import GenerationTask
from receipes.db.repositories import TaskRepository


# Порт хранилища задач для API: каждая операция идёт в своей транзакции.
class TaskStore(Protocol):
    async def create(self, people_count: int, weekly_budget: Decimal) -> UUID: ...

    async def get(self, task_id: UUID) -> GenerationTask | None: ...


class SqlTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, people_count: int, weekly_budget: Decimal) -> UUID:
        async with self._sessions() as session, session.begin():
            return await TaskRepository(session).create(people_count, weekly_budget)

    async def get(self, task_id: UUID) -> GenerationTask | None:
        async with self._sessions() as session:
            return await TaskRepository(session).get(task_id)


def get_task_store(request: Request) -> TaskStore:
    return SqlTaskStore(request.app.state.session_factory)
