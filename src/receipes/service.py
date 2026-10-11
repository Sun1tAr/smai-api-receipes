import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from receipes.domain import GenerationParameters, GenerationTaskNotFound, TaskStatus
from receipes.models import Menu
from receipes.repositories import MenuRepository, TaskRepository


@dataclass(frozen=True)
class TaskView:
    status: TaskStatus
    menu: Menu | None
    error: str | None


# Логика API-1/API-2 над репозиториями; роутеры только переводят HTTP ↔ вызовы.
class MenuGenerationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_task(self, parameters: GenerationParameters) -> uuid.UUID:
        task = await TaskRepository(self.session).create(parameters)
        await self.session.commit()
        return task.task_id

    # Мусорный (не UUID) и неизвестный идентификатор — одинаково «не найдено».
    async def get_task(self, raw_task_id: str) -> TaskView:
        try:
            task_id = uuid.UUID(raw_task_id)
        except ValueError:
            raise GenerationTaskNotFound(raw_task_id) from None
        task = await TaskRepository(self.session).get(task_id)
        status = TaskStatus(task.status)
        menu = None
        if status is TaskStatus.DONE and task.menu_id is not None:
            menu = await MenuRepository(self.session).load(task.menu_id)
        error = task.error if status is TaskStatus.FAILED else None
        return TaskView(status=status, menu=menu, error=error)
