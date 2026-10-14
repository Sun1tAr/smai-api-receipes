from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from receipes.db.models import (
    GenerationParameters,
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
from receipes.domain.menu import MenuPayload
from receipes.domain.names import name_key, normalize_name
from receipes.domain.status import TaskStatus

# Репозитории работают в транзакции вызывающего кода и сами не делают commit.


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Параметры и задача pending создаются вместе; возвращается task_id.
    async def create(self, people_count: int, weekly_budget: Decimal) -> UUID:
        parameters = GenerationParameters(
            id=uuid4(), people_count=people_count, weekly_budget=weekly_budget
        )
        task = GenerationTask(
            task_id=uuid4(),
            status=TaskStatus.PENDING,
            parameters_id=parameters.id,
        )
        self._session.add_all([parameters, task])
        await self._session.flush()
        return task.task_id

    # Задача с параметрами и (если есть) полным меню; None, если задачи нет.
    async def get(self, task_id: UUID) -> GenerationTask | None:
        recipe = selectinload(GenerationTask.menu).selectinload(Menu.menu_recipes)
        recipe_load = recipe.selectinload(MenuRecipe.recipe)
        query = (
            select(GenerationTask)
            .where(GenerationTask.task_id == task_id)
            .options(
                selectinload(GenerationTask.parameters),
                recipe_load.selectinload(Recipe.nutrition),
                recipe_load.selectinload(Recipe.instructions),
                recipe_load.selectinload(Recipe.ingredients).selectinload(
                    RecipeIngredient.ingredient
                ),
            )
        )
        return await self._session.scalar(query)

    # Атомарный захват самой старой pending: pending -> in_progress.
    async def claim_next(self) -> UUID | None:
        query = (
            select(GenerationTask.task_id)
            .where(GenerationTask.status == TaskStatus.PENDING)
            .order_by(GenerationTask.created_at, GenerationTask.task_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task_id = await self._session.scalar(query)
        if task_id is None:
            return None
        await self._session.execute(
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id)
            .values(status=TaskStatus.IN_PROGRESS)
        )
        return task_id

    async def get_parameters(self, task_id: UUID) -> GenerationParameters | None:
        query = (
            select(GenerationParameters)
            .join(
                GenerationTask, GenerationTask.parameters_id == GenerationParameters.id
            )
            .where(GenerationTask.task_id == task_id)
        )
        return await self._session.scalar(query)

    async def set_prompt(self, task_id: UUID, prompt: str) -> None:
        await self._session.execute(
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id)
            .values(prompt=prompt)
        )

    # in_progress -> done; возвращает число изменённых строк (0 — статус уже иной).
    async def mark_done(self, task_id: UUID, menu_id: UUID) -> int:
        result = await self._session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.task_id == task_id,
                GenerationTask.status == TaskStatus.IN_PROGRESS,
            )
            .values(status=TaskStatus.DONE, menu_id=menu_id, error=None)
        )
        return result.rowcount

    # in_progress -> failed; возвращает число изменённых строк.
    async def mark_failed(self, task_id: UUID, error: str) -> int:
        result = await self._session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.task_id == task_id,
                GenerationTask.status == TaskStatus.IN_PROGRESS,
            )
            .values(status=TaskStatus.FAILED, error=error)
        )
        return result.rowcount

    # Восстановление при старте: все in_progress -> failed. Возвращает их число.
    async def fail_in_progress(self, error: str) -> int:
        result = await self._session.execute(
            update(GenerationTask)
            .where(GenerationTask.status == TaskStatus.IN_PROGRESS)
            .values(status=TaskStatus.FAILED, error=error)
        )
        return result.rowcount


class MenuRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Сохраняет меню целиком; рецепты каждый раз новые, ингредиенты переиспользуются
    # по ключу имени (name_key: регистр, ё/е, кавычки, пробелы, конечная пунктуация).
    async def save(self, payload: MenuPayload) -> UUID:
        menu = Menu(id=uuid4())
        entities: list[object] = [menu]
        known = await self._known_ingredients()
        for item in payload.recipes:
            nutrition = Nutrition(id=uuid4(), **item.nutrition.model_dump())
            recipe = Recipe(
                id=uuid4(),
                name=normalize_name(item.name),
                calories=item.calories,
                cooking_time=item.cooking_time,
                photo=None,
                nutrition_id=nutrition.id,
            )
            entities += [
                nutrition,
                recipe,
                MenuRecipe(id=uuid4(), menu_id=menu.id, recipe_id=recipe.id),
            ]
            for part in item.ingredients:
                name = normalize_name(part.name)
                key = name_key(name)
                if key not in known:
                    ingredient = Ingredient(id=uuid4(), name=name)
                    entities.append(ingredient)
                    known[key] = ingredient.id
                entities.append(
                    RecipeIngredient(
                        id=uuid4(),
                        recipe_id=recipe.id,
                        ingredient_id=known[key],
                        quantity=part.quantity,
                        unit=normalize_name(part.unit),
                    )
                )
            for position, text in enumerate(item.instructions, start=1):
                entities.append(
                    Instruction(
                        id=uuid4(), recipe_id=recipe.id, text=text, position=position
                    )
                )
        self._session.add_all(entities)
        await self._session.flush()
        return menu.id

    # Ключ имени -> id существующего ингредиента (при дублях побеждает меньший id).
    # Справочник продуктов мал, поэтому читается одним запросом на сохранение.
    async def _known_ingredients(self) -> dict[str, UUID]:
        rows = await self._session.execute(
            select(Ingredient.id, Ingredient.name).order_by(Ingredient.id)
        )
        known: dict[str, UUID] = {}
        for ingredient_id, name in rows:
            known.setdefault(name_key(name), ingredient_id)
        return known


class PromptTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Активный шаблон — последний по created_at, при равенстве по id.
    async def get_active(self) -> PromptTemplate | None:
        query = (
            select(PromptTemplate)
            .order_by(PromptTemplate.created_at.desc(), PromptTemplate.id.desc())
            .limit(1)
        )
        return await self._session.scalar(query)
