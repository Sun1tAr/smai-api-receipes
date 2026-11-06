from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import NamedTuple
from uuid import UUID, uuid4

from sqlalchemy import (
    Interval,
    Row,
    Select,
    bindparam,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from receipes.db.models import (
    GenerationParameterAllergen,
    GenerationParameterHabit,
    GenerationParameterKitchenTool,
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
    RecipeLike,
    utcnow,
)
from receipes.domain.errors import TaskStateConflict
from receipes.domain.menu import MenuPayload, RecipePayload
from receipes.domain.names import name_key, normalize_name
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskKind, TaskStatus

# Репозитории работают в транзакции вызывающего кода и сами не делают commit.


# Захваченная задача и время её перехода в in_progress по часам БД.
class ClaimedTask(NamedTuple):
    task_id: UUID
    claimed_at: datetime
    kind: TaskKind = TaskKind.MENU


# Времена завершённой задачи: created_at и время терминального перехода.
class FinishedTask(NamedTuple):
    created_at: datetime
    finished_at: datetime
    kind: TaskKind = TaskKind.MENU
    claimed_at: datetime | None = None


class ReplaceContext(NamedTuple):
    remaining_seconds: float
    replaced_name: str
    recipe_names: tuple[str, ...]


_RECIPE_CONTENTS = (
    selectinload(Recipe.nutrition),
    selectinload(Recipe.instructions),
    selectinload(Recipe.ingredients).selectinload(RecipeIngredient.ingredient),
)
_PARAMETER_LINKS = (
    selectinload(GenerationParameters.habits),
    selectinload(GenerationParameters.allergies),
    selectinload(GenerationParameters.kitchen_tools),
)


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Параметры и задача pending создаются вместе; возвращается task_id. Промпт, если
    # он уже собран, сохраняется сразу; иначе его добавит воркер при захвате.
    async def create(
        self,
        params: GenerationParams,
        prompt: str | None = None,
        user_id: int | None = None,
    ) -> UUID:
        parameters = GenerationParameters(
            id=uuid4(),
            people_count=params.people_count,
            weekly_budget=params.weekly_budget,
            diet_id=params.diet,
            user_id=user_id,
        )
        links: list[object] = [
            *(
                GenerationParameterHabit(
                    id=uuid4(), parameters_id=parameters.id, habit_id=value
                )
                for value in params.habits
            ),
            *(
                GenerationParameterAllergen(
                    id=uuid4(), parameters_id=parameters.id, allergen_id=value
                )
                for value in params.allergies
            ),
            *(
                GenerationParameterKitchenTool(
                    id=uuid4(), parameters_id=parameters.id, kitchen_tool_id=value
                )
                for value in params.kitchen_tools
            ),
        ]
        created_at = utcnow()
        task = GenerationTask(
            task_id=uuid4(),
            status=TaskStatus.PENDING,
            kind=TaskKind.MENU,
            prompt=prompt,
            parameters_id=parameters.id,
            created_at=created_at,
            status_changed_at=created_at,
        )
        self._session.add_all([parameters, *links, task])
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
                # Связи параметров подгружаются заранее: lazy="raise" не даёт ленивой
                # загрузки (+3 дешёвых SELECT по индексу parameters_id).
                selectinload(GenerationTask.parameters).options(*_PARAMETER_LINKS),
                recipe_load.options(*_RECIPE_CONTENTS),
            )
        )
        return await self._session.scalar(query)

    # Атомарный захват самой старой pending: pending -> in_progress.
    # Возвращает задачу и status_changed_at, записанный этим же UPDATE.
    async def claim_next(self, *, menu_enabled: bool = True) -> ClaimedTask | None:
        query = (
            select(GenerationTask.task_id, GenerationTask.kind)
            .where(GenerationTask.status == TaskStatus.PENDING)
            .order_by(GenerationTask.created_at, GenerationTask.task_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await self._session.execute(query)).one_or_none()
        if row is None:
            return None
        task_id, kind = row
        claim = (
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id)
            .values(
                status=TaskStatus.IN_PROGRESS, status_changed_at=func.clock_timestamp()
            )
            .returning(GenerationTask.status_changed_at)
        )
        if not menu_enabled:
            claim = claim.where(GenerationTask.kind == TaskKind.REPLACE)
        claimed_at = await self._session.scalar(claim)
        return (
            None
            if claimed_at is None
            else ClaimedTask(task_id, claimed_at, TaskKind(kind))
        )

    async def get_parameters(self, task_id: UUID) -> GenerationParameters | None:
        query = (
            select(GenerationParameters)
            .join(
                GenerationTask, GenerationTask.parameters_id == GenerationParameters.id
            )
            .where(GenerationTask.task_id == task_id)
            .options(*_PARAMETER_LINKS)
        )
        return await self._session.scalar(query)

    # Параметры самой свежей задачи пользователя в статусе done (по created_at, при
    # равенстве по task_id); None, если таких задач нет.
    async def find_last_done_parameters(
        self, user_id: int
    ) -> GenerationParameters | None:
        query = (
            select(GenerationParameters)
            .join(
                GenerationTask, GenerationTask.parameters_id == GenerationParameters.id
            )
            .where(
                GenerationParameters.user_id == user_id,
                GenerationTask.kind == TaskKind.MENU,
                GenerationTask.status == TaskStatus.DONE,
            )
            .order_by(GenerationTask.created_at.desc(), GenerationTask.task_id.desc())
            .limit(1)
            .options(*_PARAMETER_LINKS)
        )
        return await self._session.scalar(query)

    # Параметры задачи целиком; GenerationParams сам сортирует списки по ID.
    async def get_params(self, task_id: UUID) -> GenerationParams | None:
        row = await self.get_parameters(task_id)
        return None if row is None else row.to_params()

    async def get_prompt(self, task_id: UUID) -> str | None:
        query = select(GenerationTask.prompt).where(GenerationTask.task_id == task_id)
        return await self._session.scalar(query)

    async def set_prompt(self, task_id: UUID, prompt: str) -> None:
        await self._session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.task_id == task_id,
                or_(
                    GenerationTask.kind == TaskKind.MENU,
                    GenerationTask.status == TaskStatus.IN_PROGRESS,
                ),
            )
            .values(prompt=prompt)
        )

    # in_progress -> done; None, если строка не изменилась (статус уже иной).
    async def mark_done(self, task_id: UUID, menu_id: UUID) -> FinishedTask | None:
        result = await self._session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.task_id == task_id,
                GenerationTask.status == TaskStatus.IN_PROGRESS,
                GenerationTask.kind == TaskKind.MENU,
            )
            .values(
                status=TaskStatus.DONE,
                menu_id=menu_id,
                error=None,
                status_changed_at=func.now(),
            )
            .returning(GenerationTask.created_at, GenerationTask.status_changed_at)
        )
        row = result.one_or_none()
        return None if row is None else FinishedTask(*row)

    # in_progress -> failed; None, если строка не изменилась.
    async def mark_failed(self, task_id: UUID, error: str) -> FinishedTask | None:
        result = await self._session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.task_id == task_id,
                GenerationTask.status == TaskStatus.IN_PROGRESS,
            )
            .values(
                status=TaskStatus.FAILED,
                error=error,
                status_changed_at=func.now(),
            )
            .returning(
                GenerationTask.created_at,
                GenerationTask.status_changed_at,
                GenerationTask.kind,
            )
        )
        row = result.one_or_none()
        return (
            None
            if row is None
            else FinishedTask(row.created_at, row.status_changed_at, TaskKind(row.kind))
        )

    # Восстановление при старте: все in_progress -> failed. Возвращает их времена.
    async def fail_in_progress(self, error: str) -> list[FinishedTask]:
        result = await self._session.execute(
            update(GenerationTask)
            .where(GenerationTask.status == TaskStatus.IN_PROGRESS)
            .values(
                status=TaskStatus.FAILED,
                error=error,
                status_changed_at=func.now(),
            )
            .returning(
                GenerationTask.created_at,
                GenerationTask.status_changed_at,
                GenerationTask.kind,
            )
        )
        return [
            FinishedTask(row.created_at, row.status_changed_at, TaskKind(row.kind))
            for row in result
        ]

    async def create_replace(
        self, source_task_id: UUID, recipe_id: UUID
    ) -> UUID | None:
        source = await self._session.scalar(
            select(GenerationTask).where(
                GenerationTask.task_id == source_task_id,
                GenerationTask.status == TaskStatus.DONE,
                GenerationTask.kind == TaskKind.MENU,
            )
        )
        if source is None or source.menu_id is None:
            return None
        if not await self._lock_menu(source.menu_id):
            return None
        slot = await self._session.scalar(
            select(MenuRecipe.id)
            .where(
                MenuRecipe.menu_id == source.menu_id, MenuRecipe.recipe_id == recipe_id
            )
            .limit(1)
        )
        if slot is None:
            return None
        now = await self._session.scalar(select(func.clock_timestamp()))
        assert now is not None
        task = GenerationTask(
            task_id=uuid4(),
            kind=TaskKind.REPLACE,
            status=TaskStatus.PENDING,
            parameters_id=source.parameters_id,
            menu_id=source.menu_id,
            source_task_id=source_task_id,
            replaced_recipe_id=recipe_id,
            created_at=now,
            status_changed_at=now,
        )
        self._session.add(task)
        await self._session.flush()
        return task.task_id

    async def get_replacement(self, task_id: UUID) -> GenerationTask | None:
        query = (
            self._replace_query(task_id)
            .options(
                selectinload(GenerationTask.result_recipe).options(*_RECIPE_CONTENTS)
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(query)

    async def get_replace_context(
        self, task_id: UUID, wait_seconds: float
    ) -> ReplaceContext | None:
        task = await self._session.scalar(
            self._replace_query(task_id)
            .where(GenerationTask.status == TaskStatus.IN_PROGRESS)
            .execution_options(populate_existing=True)
        )
        if task is None or task.menu_id is None:
            return None
        slots = await self._recipe_slots(task.menu_id)
        replaced = next(
            (row.name for row in slots if row.recipe_id == task.replaced_recipe_id),
            None,
        )
        remaining = await self._remaining_seconds(task.created_at, wait_seconds)
        if replaced is None or remaining <= 0:
            return None
        return ReplaceContext(remaining, replaced, tuple(row.name for row in slots))

    async def expire_replace(
        self, task_id: UUID, wait_seconds: float, error: str
    ) -> FinishedTask | None:
        task = await self._session.scalar(
            self._replace_query(task_id)
            .where(
                GenerationTask.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS))
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            task is None
            or await self._remaining_seconds(task.created_at, wait_seconds) > 0
        ):
            return None
        claimed_at = (
            task.status_changed_at if task.status == TaskStatus.IN_PROGRESS else None
        )
        finished_at = await self._session.scalar(
            update(GenerationTask)
            .where(GenerationTask.task_id == task_id)
            .values(
                status=TaskStatus.FAILED,
                error=error,
                status_changed_at=func.clock_timestamp(),
            )
            .returning(GenerationTask.status_changed_at)
        )
        assert finished_at is not None
        return FinishedTask(task.created_at, finished_at, TaskKind.REPLACE, claimed_at)

    async def finish_replace(
        self, task_id: UUID, payload: RecipePayload, wait_seconds: float
    ) -> FinishedTask | None:
        task = await self._session.scalar(
            self._replace_query(task_id)
            .where(GenerationTask.status == TaskStatus.IN_PROGRESS)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            return None
        # Таймаут синхронизируется по задаче, параллельные замены — по меню.
        if task.menu_id is None or not await self._lock_menu(task.menu_id):
            raise TaskStateConflict
        slots = await self._recipe_slots(task.menu_id)
        if await self._remaining_seconds(task.created_at, wait_seconds) <= 0:
            return None
        targets = [row.id for row in slots if row.recipe_id == task.replaced_recipe_id]
        candidate = name_key(payload.name)
        if len(targets) != 1 or any(name_key(row.name) == candidate for row in slots):
            raise TaskStateConflict

        # Проверяем срок повторно после записи: ожидание БД тоже входит в бюджет.
        async with self._session.begin_nested() as savepoint:
            result_id = await MenuRepository(self._session).save_recipe(payload)
            await self._session.execute(
                update(MenuRecipe)
                .where(MenuRecipe.id == targets[0])
                .values(recipe_id=result_id)
            )
            deadline = GenerationTask.created_at + bindparam(
                None, timedelta(seconds=wait_seconds), type_=Interval()
            )
            finish_clock = select(func.clock_timestamp().label("now")).cte()
            finished_at = await self._session.scalar(
                update(GenerationTask)
                .where(GenerationTask.task_id == task_id, finish_clock.c.now < deadline)
                .values(
                    status=TaskStatus.DONE,
                    result_recipe_id=result_id,
                    error=None,
                    status_changed_at=finish_clock.c.now,
                )
                .returning(GenerationTask.status_changed_at)
            )
            if finished_at is None:
                await savepoint.rollback()
                return None
        return FinishedTask(task.created_at, finished_at, TaskKind.REPLACE)

    @staticmethod
    def _replace_query(task_id: UUID) -> Select[tuple[GenerationTask]]:
        return select(GenerationTask).where(
            GenerationTask.task_id == task_id, GenerationTask.kind == TaskKind.REPLACE
        )

    async def _lock_menu(self, menu_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(Menu.id).where(Menu.id == menu_id).with_for_update()
            )
            is not None
        )

    async def _recipe_slots(self, menu_id: UUID) -> list[Row[tuple[UUID, UUID, str]]]:
        rows = await self._session.execute(
            select(MenuRecipe.id, MenuRecipe.recipe_id, Recipe.name)
            .join(Recipe, Recipe.id == MenuRecipe.recipe_id)
            .where(MenuRecipe.menu_id == menu_id)
            .order_by(MenuRecipe.id)
        )
        return list(rows)

    async def _remaining_seconds(
        self, created_at: datetime, wait_seconds: float
    ) -> float:
        now = await self._session.scalar(select(func.clock_timestamp()))
        assert now is not None
        return wait_seconds - (now - created_at).total_seconds()


class MenuRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_history(self, user_id: int) -> Sequence[Row]:
        query = (
            select(
                GenerationTask.task_id,
                Menu.id.label("menu_id"),
                GenerationTask.created_at,
                Recipe.id.label("recipe_id"),
                Recipe.name,
                Recipe.calories,
                Recipe.cooking_time,
            )
            .select_from(GenerationTask)
            .join(
                GenerationParameters,
                GenerationParameters.id == GenerationTask.parameters_id,
            )
            .join(Menu, Menu.id == GenerationTask.menu_id)
            .outerjoin(MenuRecipe, MenuRecipe.menu_id == Menu.id)
            .outerjoin(Recipe, Recipe.id == MenuRecipe.recipe_id)
            .where(
                GenerationParameters.user_id == user_id,
                GenerationTask.kind == TaskKind.MENU,
                GenerationTask.status == TaskStatus.DONE,
            )
            .order_by(GenerationTask.created_at.desc(), GenerationTask.task_id.desc())
        )
        return (await self._session.execute(query)).all()

    async def find_cart_recipe(self, recipe_id: UUID) -> Recipe | None:
        query = (
            select(Recipe)
            .join(MenuRecipe, MenuRecipe.recipe_id == Recipe.id)
            .join(GenerationTask, GenerationTask.menu_id == MenuRecipe.menu_id)
            .join(
                GenerationParameters,
                GenerationParameters.id == GenerationTask.parameters_id,
            )
            .where(
                Recipe.id == recipe_id,
                GenerationTask.status == TaskStatus.DONE,
                GenerationParameters.user_id.is_not(None),
            )
            .options(
                selectinload(Recipe.ingredients).selectinload(
                    RecipeIngredient.ingredient
                )
            )
        )
        return await self._session.scalar(query)

    # Сохраняет меню целиком; рецепты каждый раз новые, ингредиенты переиспользуются
    # по ключу имени (name_key: регистр, ё/е, кавычки, пробелы, конечная пунктуация).
    async def save(self, payload: MenuPayload) -> UUID:
        menu = Menu(id=uuid4())
        entities: list[object] = [menu]
        known = await self._known_ingredients()
        for item in payload.recipes:
            recipe_id = self._add_recipe(item, known)
            entities.append(
                MenuRecipe(id=uuid4(), menu_id=menu.id, recipe_id=recipe_id)
            )
        self._session.add_all(entities)
        await self._session.flush()
        return menu.id

    async def save_recipe(self, payload: RecipePayload) -> UUID:
        recipe_id = self._add_recipe(payload, await self._known_ingredients())
        await self._session.flush()
        return recipe_id

    def _add_recipe(self, item: RecipePayload, known: dict[str, UUID]) -> UUID:
        nutrition = Nutrition(id=uuid4(), **item.nutrition.model_dump())
        recipe = Recipe(
            id=uuid4(),
            name=normalize_name(item.name),
            calories=item.calories,
            cooking_time=item.cooking_time,
            photo=None,
            nutrition_id=nutrition.id,
        )
        entities: list[object] = [nutrition, recipe]
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
        return recipe.id

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
    async def get_active(self, kind: TaskKind = TaskKind.MENU) -> PromptTemplate | None:
        query = (
            select(PromptTemplate)
            .where(PromptTemplate.kind == kind)
            .order_by(PromptTemplate.created_at.desc(), PromptTemplate.id.desc())
            .limit(1)
        )
        return await self._session.scalar(query)


class RecipeLikeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recipe_exists(self, recipe_id: UUID) -> bool:
        query = select(Recipe.id).where(Recipe.id == recipe_id)
        # Удерживаем рецепт от удаления до вставки связи.
        return (
            await self._session.scalar(query.with_for_update(read=True, key_share=True))
            is not None
        )

    async def add(self, user_id: int, recipe_id: UUID) -> bool:
        query = (
            insert(RecipeLike)
            .values(user_id=user_id, recipe_id=recipe_id)
            .on_conflict_do_nothing(
                index_elements=[RecipeLike.user_id, RecipeLike.recipe_id]
            )
            .returning(RecipeLike.recipe_id)
        )
        return await self._session.scalar(query) is not None

    async def remove(self, user_id: int, recipe_id: UUID) -> bool:
        query = (
            delete(RecipeLike)
            .where(RecipeLike.user_id == user_id, RecipeLike.recipe_id == recipe_id)
            .returning(RecipeLike.recipe_id)
        )
        return await self._session.scalar(query) is not None

    @staticmethod
    def _by_user(query: Select, user_id: int) -> Select:
        return (
            query.join(RecipeLike, RecipeLike.recipe_id == Recipe.id)
            .where(RecipeLike.user_id == user_id)
            .order_by(RecipeLike.created_at.desc(), RecipeLike.recipe_id.desc())
        )

    async def list_recipes(self, user_id: int) -> Sequence[Row]:
        query = select(
            Recipe.id,
            Recipe.name,
            Recipe.calories,
            Recipe.cooking_time,
            Nutrition.calories.label("nutrition_calories"),
            Nutrition.proteins,
            Nutrition.fats,
            Nutrition.carbohydrates,
        ).join(Nutrition, Nutrition.id == Recipe.nutrition_id)
        return (await self._session.execute(self._by_user(query, user_id))).all()

    async def recent_names(self, user_id: int | None, limit: int) -> tuple[str, ...]:
        if user_id is None or limit == 0:
            return ()
        query = self._by_user(select(Recipe.name), user_id).limit(limit)
        return tuple(await self._session.scalars(query))
