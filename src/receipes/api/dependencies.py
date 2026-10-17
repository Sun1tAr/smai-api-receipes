from typing import Protocol
from uuid import UUID

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.db.models import GenerationTask
from receipes.db.repositories import PromptTemplateRepository, TaskRepository
from receipes.dictionaries import Dictionaries
from receipes.domain.errors import PromptTemplateError
from receipes.domain.params import GenerationParams
from receipes.generation.prompt import render_prompt

logger = structlog.get_logger()


# Порт хранилища задач для API: каждая операция идёт в своей транзакции.
class TaskStore(Protocol):
    async def create(self, params: GenerationParams) -> UUID: ...

    async def get(self, task_id: UUID) -> GenerationTask | None: ...


class SqlTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, params: GenerationParams) -> UUID:
        async with self._sessions() as session, session.begin():
            prompt, problem = await self._render_prompt(session, params)
            task_id = await TaskRepository(session).create(params, prompt)
        if problem is not None:
            logger.warning(
                "task_prompt_not_saved", task_id=str(task_id), reason=problem
            )
        return task_id

    # Промпт фиксируется на момент создания задачи. Нет годного шаблона: задача всё
    # равно принимается, промпт соберёт воркер. Второе значение — причина (для лога
    # после коммита); текст промпта и параметры в лог не попадают.
    @staticmethod
    async def _render_prompt(
        session: AsyncSession, params: GenerationParams
    ) -> tuple[str | None, str | None]:
        template = await PromptTemplateRepository(session).get_active()
        if template is None:
            return None, "missing"
        try:
            return (
                render_prompt(
                    template.template, params.people_count, params.weekly_budget
                ),
                None,
            )
        except PromptTemplateError:
            return None, "invalid"

    async def get(self, task_id: UUID) -> GenerationTask | None:
        async with self._sessions() as session:
            return await TaskRepository(session).get(task_id)


def get_task_store(request: Request) -> TaskStore:
    return SqlTaskStore(request.app.state.session_factory)


# Порт справочников MS-2 для API: в тестах подменяется фейком.
class DictionarySource(Protocol):
    async def fetch(self) -> Dictionaries: ...


def get_dictionaries(request: Request) -> DictionarySource:
    return request.app.state.dictionaries
