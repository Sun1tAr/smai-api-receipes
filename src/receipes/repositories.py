import uuid
from collections.abc import Callable
from datetime import timedelta

import structlog
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from receipes.domain import (
    DomainError,
    GenerationParameters,
    GenerationTaskNotFound,
    InvalidTaskTransition,
    MenuNotFound,
    TaskStatus,
)
from receipes.generated_menu import GeneratedMenu
from receipes.models import (
    GenerationParametersRecord,
    GenerationTask,
    Ingredient,
    Instruction,
    Menu,
    MenuRecipe,
    Nutrition,
    PromptTemplate,
    Recipe,
    RecipeIngredient,
)

logger = structlog.get_logger()

# Сколько подряд «ядовитых» задач claim_next готов пропустить за один вызов.
MAX_CLAIM_ATTEMPTS = 20
INVALID_PARAMETERS_ERROR = "Invalid generation parameters"
QUEUE_TIMEOUT_ERROR = "Queue timeout"
INTERRUPTED_ERROR = "Generation interrupted"


# now() в PostgreSQL — начало транзакции; моменты старта и завершения берутся из
# clock_timestamp(), иначе при захвате в той же транзакции started_at == created_at.
def clock():
    return func.clock_timestamp()


# Репозитории не коммитят: границы транзакции задаёт вызывающий код.
class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, parameters: GenerationParameters) -> GenerationTask:
        record = GenerationParametersRecord(
            id=uuid.uuid4(),
            people_count=parameters.people_count,
            weekly_budget=parameters.weekly_budget,
        )
        task = GenerationTask(
            task_id=uuid.uuid4(), status=TaskStatus.PENDING, parameters_id=record.id
        )
        self.session.add_all([record, task])
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def get(self, task_id: uuid.UUID) -> GenerationTask:
        task = await self.session.get(GenerationTask, task_id, populate_existing=True)
        if task is None:
            raise GenerationTaskNotFound(task_id)
        return task

    # Единственный путь в in_progress. Старейшая pending-задача переходит в
    # in_progress; залоченные другой транзакцией пропускаются. Промпт собирается из
    # параметров здесь же: CHECK требует его у любой задачи in_progress. Задача с
    # невалидными параметрами или промптом, который не собрать, не должна блокировать
    # очередь: она переводится в failed, и берётся следующая. Вызывающий коммитит
    # транзакцию сразу после claim_next, чтобы не держать блокировку строки.
    async def claim_next(
        self, build_prompt: Callable[[GenerationParameters], str]
    ) -> GenerationTask | None:
        for _ in range(MAX_CLAIM_ATTEMPTS):
            row = (
                await self.session.execute(
                    select(GenerationTask, GenerationParametersRecord)
                    .join(
                        GenerationParametersRecord,
                        GenerationParametersRecord.id == GenerationTask.parameters_id,
                    )
                    .where(GenerationTask.status == TaskStatus.PENDING)
                    .order_by(GenerationTask.created_at, GenerationTask.task_id)
                    .limit(1)
                    .with_for_update(of=GenerationTask, skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).first()
            if row is None:
                return None
            task, record = row
            try:
                prompt = build_prompt(
                    GenerationParameters(record.people_count, record.weekly_budget)
                )
            except DomainError as error:
                logger.error(
                    "invalid_task_parameters",
                    task_id=str(task.task_id),
                    error_type=type(error).__name__,
                )
                await self.session.execute(
                    update(GenerationTask)
                    .where(
                        GenerationTask.task_id == task.task_id,
                        GenerationTask.status == TaskStatus.PENDING,
                    )
                    .values(
                        status=TaskStatus.FAILED,
                        error=INVALID_PARAMETERS_ERROR,
                        finished_at=clock(),
                    )
                    .execution_options(synchronize_session=False)
                )
                continue
            await self.session.execute(
                update(GenerationTask)
                .where(GenerationTask.task_id == task.task_id)
                .values(
                    status=TaskStatus.IN_PROGRESS, prompt=prompt, started_at=clock()
                )
                .execution_options(synchronize_session=False)
            )
            await self.session.refresh(task)
            return task
        logger.error("claim_attempts_exhausted", attempts=MAX_CLAIM_ATTEMPTS)
        return None

    # Условный переход в done или failed: False, если задача уже в другом статусе
    # (её завершил expire_stale или другой воркер). Допустимость перехода проверяет
    # домен. В in_progress ведёт только claim_next. Для done предпочтительнее
    # complete(): он сохраняет меню и переводит задачу атомарно.
    async def transition(
        self,
        task_id: uuid.UUID,
        expected: TaskStatus,
        to: TaskStatus,
        *,
        menu_id: uuid.UUID | None = None,
        error: str | None = None,
    ) -> bool:
        if not expected.can_transition_to(to):
            raise InvalidTaskTransition(expected, to)
        if to is TaskStatus.IN_PROGRESS:
            raise ValueError("in_progress is reached only through claim_next")
        if to is TaskStatus.DONE:
            if menu_id is None:
                raise ValueError("menu_id is required for done")
            values: dict[str, object] = {"menu_id": menu_id}
        else:
            if error is None:
                raise ValueError("error is required for failed")
            values = {"error": error}
        result = await self.session.execute(
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id, GenerationTask.status == expected)
            .values(status=to, finished_at=clock(), **values)
            .returning(GenerationTask.task_id)
            .execution_options(synchronize_session=False)
        )
        return result.first() is not None

    # Блокирует задачу in_progress (SELECT … FOR UPDATE; если её нет, например её
    # просрочил expire_stale, возвращает False, ничего не записав), затем в savepoint
    # сохраняет меню и переводит in_progress → done. Если переход всё же не
    # состоялся или сохранение упало, savepoint откатывается: меню и добавленные им
    # ингредиенты не остаются. Внешней транзакцией управляет вызывающий. Отмену
    # корутины (CancelledError) откат не перехватывает: транзакцию завершает
    # владелец сессии.
    async def complete(self, task_id: uuid.UUID, generated: GeneratedMenu) -> bool:
        locked = await self.session.scalar(
            select(GenerationTask.task_id)
            .where(
                GenerationTask.task_id == task_id,
                GenerationTask.status == TaskStatus.IN_PROGRESS,
            )
            .with_for_update()
        )
        if locked is None:
            return False
        savepoint = await self.session.begin_nested()
        try:
            menu_id = await MenuRepository(self.session).save(generated)
            done = await self.transition(
                task_id, TaskStatus.IN_PROGRESS, TaskStatus.DONE, menu_id=menu_id
            )
        except Exception:
            await savepoint.rollback()
            raise
        if done:
            await savepoint.commit()
        else:
            await savepoint.rollback()
        return done

    # Завершает просроченные задачи условными UPDATE; возвращает их число.
    async def expire_stale(self, pending_ttl: timedelta, stale_after: timedelta) -> int:
        expired = 0
        for status, age_column, limit, message in (
            (
                TaskStatus.PENDING,
                GenerationTask.created_at,
                pending_ttl,
                QUEUE_TIMEOUT_ERROR,
            ),
            (
                TaskStatus.IN_PROGRESS,
                GenerationTask.started_at,
                stale_after,
                INTERRUPTED_ERROR,
            ),
        ):
            result = await self.session.execute(
                update(GenerationTask)
                .where(GenerationTask.status == status, age_column < clock() - limit)
                .values(status=TaskStatus.FAILED, error=message, finished_at=clock())
                .returning(GenerationTask.task_id)
                .execution_options(synchronize_session=False)
            )
            expired += len(result.all())
        return expired

    async def count_pending(self) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(GenerationTask)
            .where(GenerationTask.status == TaskStatus.PENDING)
        )
        return int(count or 0)

    async def oldest_pending_age(self) -> timedelta | None:
        return await self.session.scalar(
            select(clock() - func.min(GenerationTask.created_at)).where(
                GenerationTask.status == TaskStatus.PENDING
            )
        )


class MenuRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Сохраняет меню целиком и возвращает его id. Ингредиенты общие для всех рецептов:
    # ключ name_key считается в Python, вставка идёт через ON CONFLICT (name_key) DO
    # NOTHING, а в name остаётся первое встреченное написание.
    async def save(self, generated: GeneratedMenu) -> uuid.UUID:
        display_names: dict[str, str] = {}
        for recipe in generated.recipes:
            for ingredient in recipe.ingredients:
                display_names.setdefault(ingredient.name_key, ingredient.name)
        await self.session.execute(
            pg_insert(Ingredient)
            .values(
                [
                    {"id": uuid.uuid4(), "name": name, "name_key": key}
                    for key, name in display_names.items()
                ]
            )
            .on_conflict_do_nothing(index_elements=[Ingredient.name_key])
        )
        found = await self.session.execute(
            select(Ingredient.name_key, Ingredient.id).where(
                Ingredient.name_key.in_(list(display_names))
            )
        )
        ingredient_ids: dict[str, uuid.UUID] = {row[0]: row[1] for row in found}

        menu_id = uuid.uuid4()
        nutrition_rows, recipe_rows, link_rows = [], [], []
        ingredient_rows, instruction_rows = [], []
        for position, recipe in enumerate(generated.recipes, start=1):
            recipe_id, nutrition_id = uuid.uuid4(), uuid.uuid4()
            nutrition_rows.append({"id": nutrition_id, **recipe.nutrition.model_dump()})
            recipe_rows.append(
                {
                    "id": recipe_id,
                    "name": recipe.name,
                    "calories": recipe.calories,
                    "cooking_time": recipe.cooking_time,
                    "photo": None,
                    "nutrition_id": nutrition_id,
                }
            )
            link_rows.append(
                {
                    "id": uuid.uuid4(),
                    "menu_id": menu_id,
                    "recipe_id": recipe_id,
                    "position": position,
                }
            )
            for index, ingredient in enumerate(recipe.ingredients, start=1):
                ingredient_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "recipe_id": recipe_id,
                        "ingredient_id": ingredient_ids[ingredient.name_key],
                        "quantity": ingredient.quantity,
                        "unit": ingredient.unit,
                        "position": index,
                    }
                )
            for index, text in enumerate(recipe.instructions, start=1):
                instruction_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "recipe_id": recipe_id,
                        "text": text,
                        "position": index,
                    }
                )
        # Порядок вставки следует зависимостям внешних ключей.
        await self.session.execute(insert(Menu).values(id=menu_id))
        await self.session.execute(insert(Nutrition), nutrition_rows)
        await self.session.execute(insert(Recipe), recipe_rows)
        await self.session.execute(insert(MenuRecipe), link_rows)
        await self.session.execute(insert(RecipeIngredient), ingredient_rows)
        await self.session.execute(insert(Instruction), instruction_rows)
        return menu_id

    # Меню с рецептами, ингредиентами и шагами; всё отсортировано по position.
    async def load(self, menu_id: uuid.UUID) -> Menu:
        recipe_loading = (
            selectinload(Menu.menu_recipes)
            .joinedload(MenuRecipe.recipe)
            .options(
                joinedload(Recipe.nutrition),
                selectinload(Recipe.recipe_ingredients).joinedload(
                    RecipeIngredient.ingredient
                ),
                selectinload(Recipe.instructions),
            )
        )
        menu = (
            await self.session.execute(
                select(Menu)
                .where(Menu.id == menu_id)
                .options(recipe_loading)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if menu is None:
            raise MenuNotFound(menu_id)
        return menu


class PromptTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Шаблонов может не быть: первый создаётся сидом (SMAI-56).
    async def latest(self) -> PromptTemplate | None:
        return (
            await self.session.execute(
                select(PromptTemplate)
                .order_by(PromptTemplate.created_at.desc(), PromptTemplate.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
