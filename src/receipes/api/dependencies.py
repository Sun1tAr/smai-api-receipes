from decimal import Decimal
from typing import Protocol
from uuid import UUID

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.db.models import GenerationTask
from receipes.db.repositories import PromptTemplateRepository, TaskRepository
from receipes.domain.errors import PromptTemplateError
from receipes.generation.prompt import render_prompt

logger = structlog.get_logger()


# Порт хранилища задач для API: каждая операция идёт в своей транзакции.
class TaskStore(Protocol):
    async def create(self, people_count: int, weekly_budget: Decimal) -> UUID: ...

    async def get(self, task_id: UUID) -> GenerationTask | None: ...


class SqlTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, people_count: int, weekly_budget: Decimal) -> UUID:
        async with self._sessions() as session, session.begin():
            prompt = await self._render_prompt(session, people_count, weekly_budget)
            return await TaskRepository(session).create(
                people_count, weekly_budget, prompt
            )

    # Промпт фиксируется на момент создания задачи. Нет годного шаблона: задача всё
    # равно принимается, промпт соберёт воркер (текст промпта и параметры не логируем).
    @staticmethod
    async def _render_prompt(
        session: AsyncSession, people_count: int, weekly_budget: Decimal
    ) -> str | None:
        template = await PromptTemplateRepository(session).get_active()
        if template is None:
            logger.warning("task_prompt_not_saved", reason="missing")
            return None
        try:
            return render_prompt(template.template, people_count, weekly_budget)
        except PromptTemplateError:
            logger.warning("task_prompt_not_saved", reason="invalid")
            return None

    async def get(self, task_id: UUID) -> GenerationTask | None:
        async with self._sessions() as session:
            return await TaskRepository(session).get(task_id)


def get_task_store(request: Request) -> TaskStore:
    return SqlTaskStore(request.app.state.session_factory)
