import asyncio
import re
from typing import Protocol
from uuid import UUID

import structlog
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.api.schemas import INT4_MAX
from receipes.config import DEFAULT_LIKES_IN_PROMPT_LIMIT
from receipes.db.models import GenerationParameters, GenerationTask, Recipe
from receipes.db.repositories import (
    PromptTemplateRepository,
    RecipeLikeRepository,
    TaskRepository,
)
from receipes.dictionaries import Dictionaries, DictionarySource
from receipes.domain.errors import (
    REPLACEMENT_TIMED_OUT,
    PromptTemplateError,
    UnknownDictionaryId,
)
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskStatus
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.prompt import (
    build_constraints,
    needs_dictionaries,
    render_prompt,
)

logger = structlog.get_logger()
REPLACE_POLL_SECONDS = 0.25


# Порт хранилища задач для API: каждая операция идёт в своей транзакции.
class TaskStore(Protocol):
    async def create(
        self,
        params: GenerationParams,
        dictionaries: Dictionaries | None = None,
        user_id: int | None = None,
    ) -> UUID: ...

    async def get(self, task_id: UUID) -> GenerationTask | None: ...

    async def create_replace(self, task_id: UUID, recipe_id: UUID) -> UUID | None: ...

    async def wait_for_replace(
        self, task_id: UUID, wait_seconds: float, metrics: GenerationMetrics
    ) -> Recipe | None: ...

    async def last_done_parameters(
        self, user_id: int
    ) -> GenerationParameters | None: ...


class SqlTaskStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        likes_in_prompt_limit: int = DEFAULT_LIKES_IN_PROMPT_LIMIT,
    ) -> None:
        self._sessions = session_factory
        self._likes_in_prompt_limit = likes_in_prompt_limit

    async def create(
        self,
        params: GenerationParams,
        dictionaries: Dictionaries | None = None,
        user_id: int | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            prompt, problem = await self._render_prompt(
                session, params, dictionaries, user_id
            )
            task_id = await TaskRepository(session).create(params, prompt, user_id)
        if problem is not None:
            logger.warning(
                "task_prompt_not_saved", task_id=str(task_id), reason=problem
            )
        return task_id

    # Промпт фиксируется на момент создания задачи. Нет годного шаблона: задача всё
    # равно принимается, промпт соберёт воркер. То же, если у запроса есть ID, а
    # справочников нет или они не знают ID (API этого не допускает: ID проверены).
    # Второе значение — причина (для лога после коммита); текст промпта и параметры
    # в лог не попадают.
    async def _render_prompt(
        self,
        session: AsyncSession,
        params: GenerationParams,
        dictionaries: Dictionaries | None,
        user_id: int | None,
    ) -> tuple[str | None, str | None]:
        template = await PromptTemplateRepository(session).get_active()
        if template is None:
            return None, "missing"
        constraints = ""
        if needs_dictionaries(params):
            if dictionaries is None:
                return None, "dictionaries_missing"
            try:
                constraints = build_constraints(params, dictionaries)
            except UnknownDictionaryId:
                return None, "unknown_id"
        liked_names = await RecipeLikeRepository(session).recent_names(
            user_id, self._likes_in_prompt_limit
        )
        try:
            return (
                render_prompt(
                    template.template,
                    params.people_count,
                    params.weekly_budget,
                    constraints,
                    liked_recipe_names=liked_names,
                ),
                None,
            )
        except PromptTemplateError:
            return None, "invalid"

    async def get(self, task_id: UUID) -> GenerationTask | None:
        async with self._sessions() as session:
            return await TaskRepository(session).get(task_id)

    async def create_replace(self, task_id: UUID, recipe_id: UUID) -> UUID | None:
        async with self._sessions() as session, session.begin():
            return await TaskRepository(session).create_replace(task_id, recipe_id)

    async def wait_for_replace(
        self, task_id: UUID, wait_seconds: float, metrics: GenerationMetrics
    ) -> Recipe | None:
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        while True:
            expired = None
            check_expiry = deadline is not None and loop.time() >= deadline
            async with self._sessions() as session, session.begin():
                tasks = TaskRepository(session)
                if check_expiry:
                    expired = await tasks.expire_replace(
                        task_id, wait_seconds, REPLACEMENT_TIMED_OUT
                    )
                task = await tasks.get_replacement(task_id)
                if (
                    task is not None
                    and task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
                    and (deadline is None or check_expiry)
                ):
                    database_now = await session.scalar(select(func.clock_timestamp()))
                    elapsed = (database_now - task.created_at).total_seconds()
                    deadline = loop.time() + wait_seconds - elapsed
            if expired is not None:
                metrics.observe_finished(
                    "failed",
                    expired.created_at,
                    expired.finished_at,
                    expired.claimed_at,
                    kind=expired.kind,
                )
            if task is None or task.status == TaskStatus.FAILED:
                return None
            if task.status == TaskStatus.DONE:
                return task.result_recipe
            assert deadline is not None
            await asyncio.sleep(
                max(0, min(deadline - loop.time(), REPLACE_POLL_SECONDS))
            )

    async def last_done_parameters(self, user_id: int) -> GenerationParameters | None:
        async with self._sessions() as session:
            return await TaskRepository(session).find_last_done_parameters(user_id)


def get_task_store(request: Request) -> TaskStore:
    return SqlTaskStore(
        request.app.state.session_factory,
        request.app.state.settings.likes_in_prompt_limit,
    )


def get_dictionaries(request: Request) -> DictionarySource:
    return request.app.state.dictionaries


USER_ID_HEADER = "X-User-Id"
# Только десятичные цифры без знака и пробелов; диапазон — положительный int4.
_USER_ID = re.compile(r"[0-9]{1,10}")


# Владелец запроса из заголовка шлюза; None, если заголовка нет, он повторён или
# некорректен. Все значения передаются списком: дубль нельзя принять за одно из них.
def parse_user_id(values: list[str]) -> int | None:
    if len(values) != 1 or _USER_ID.fullmatch(values[0]) is None:
        return None
    value = int(values[0])
    return value if 1 <= value <= INT4_MAX else None
