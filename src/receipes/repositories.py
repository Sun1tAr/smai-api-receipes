import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from receipes.domain import (
    GenerationParameters,
    GenerationTaskNotFound,
    InvalidTaskTransition,
    TaskStatus,
)
from receipes.generated_menu import GeneratedMenu
from receipes.models import (
    INGREDIENT_NAME_KEY,
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

QUEUE_TIMEOUT_ERROR = "Queue timeout"
INTERRUPTED_ERROR = "Generation interrupted"


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

    # Старейшая pending-задача переходит в in_progress; залоченные другой транзакцией
    # пропускаются. Промпт собирается из параметров здесь же: CHECK требует его
    # у любой задачи in_progress.
    async def claim_next(
        self, build_prompt: Callable[[GenerationParameters], str]
    ) -> GenerationTask | None:
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
        prompt = build_prompt(
            GenerationParameters(record.people_count, record.weekly_budget)
        )
        await self.session.execute(
            update(GenerationTask)
            .where(GenerationTask.task_id == task.task_id)
            .values(status=TaskStatus.IN_PROGRESS, prompt=prompt, started_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self.session.refresh(task)
        return task

    # Условный переход: False, если задача уже в другом статусе (её обработал
    # другой воркер или она просрочена). Допустимость перехода проверяет домен.
    async def transition(
        self,
        task_id: uuid.UUID,
        expected: TaskStatus,
        to: TaskStatus,
        *,
        prompt: str | None = None,
        menu_id: uuid.UUID | None = None,
        error: str | None = None,
    ) -> bool:
        if not expected.can_transition_to(to):
            raise InvalidTaskTransition(expected, to)
        values: dict[str, object] = {"status": to}
        if to is TaskStatus.IN_PROGRESS:
            if prompt is None:
                raise ValueError("prompt is required for in_progress")
            values.update(prompt=prompt, started_at=func.now())
        elif to is TaskStatus.DONE:
            if menu_id is None:
                raise ValueError("menu_id is required for done")
            values.update(menu_id=menu_id, finished_at=func.now())
        else:
            if error is None:
                raise ValueError("error is required for failed")
            values.update(error=error, finished_at=func.now())
        result = await self.session.execute(
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id, GenerationTask.status == expected)
            .values(**values)
            .returning(GenerationTask.task_id)
            .execution_options(synchronize_session=False)
        )
        return result.first() is not None

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
                .where(
                    GenerationTask.status == status,
                    age_column < func.now() - limit,
                )
                .values(status=TaskStatus.FAILED, error=message, finished_at=func.now())
                .returning(GenerationTask.task_id)
                .execution_options(synchronize_session=False)
            )
            expired += len(result.all())
        return expired

    async def count_pending(self) -> int:
        return await self.session.scalar(
            select(func.count())
            .select_from(GenerationTask)
            .where(GenerationTask.status == TaskStatus.PENDING)
        )

    async def oldest_pending_age(self) -> timedelta | None:
        return await self.session.scalar(
            select(func.now() - func.min(GenerationTask.created_at)).where(
                GenerationTask.status == TaskStatus.PENDING
            )
        )


class MenuRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Сохраняет меню целиком и возвращает его id. Ингредиенты общие для всех
    # рецептов: имена нормализуются, вставка идёт через ON CONFLICT по тому же
    # выражению, что и уникальный индекс.
    async def save(self, generated: GeneratedMenu) -> uuid.UUID:
        names = sorted(
            {
                ingredient.normalized_name
                for recipe in generated.recipes
                for ingredient in recipe.ingredients
            }
        )
        await self.session.execute(
            pg_insert(Ingredient)
            .values([{"id": uuid.uuid4(), "name": name} for name in names])
            .on_conflict_do_nothing(index_elements=[INGREDIENT_NAME_KEY])
        )
        ingredient_ids = dict(
            (
                await self.session.execute(
                    select(Ingredient.name, Ingredient.id).where(
                        INGREDIENT_NAME_KEY.in_(
                            [func.lower(func.btrim(name)) for name in names]
                        )
                    )
                )
            ).all()
        )

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
                        "ingredient_id": ingredient_ids[ingredient.normalized_name],
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
    async def load(self, menu_id: uuid.UUID) -> Menu | None:
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
        return (
            await self.session.execute(
                select(Menu)
                .where(Menu.id == menu_id)
                .options(recipe_loading)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()


class PromptTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest(self) -> PromptTemplate | None:
        return (
            await self.session.execute(
                select(PromptTemplate)
                .order_by(PromptTemplate.created_at.desc(), PromptTemplate.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
