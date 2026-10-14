import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.db.models import (
    Ingredient,
    Instruction,
    Menu,
    MenuRecipe,
    PromptTemplate,
    Recipe,
    RecipeIngredient,
)
from receipes.db.repositories import (
    MenuRepository,
    PromptTemplateRepository,
    TaskRepository,
)
from receipes.domain.menu import (
    IngredientPayload,
    MenuPayload,
    NutritionPayload,
    RecipePayload,
)
from receipes.domain.status import TaskStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]


def recipe(name: str, *ingredients: str, steps: int = 2) -> RecipePayload:
    return RecipePayload(
        name=name,
        ingredients=[
            IngredientPayload(name=item, quantity=Decimal("100.5"), unit="г")
            for item in ingredients
        ],
        calories=Decimal("350"),
        nutrition=NutritionPayload(
            calories=Decimal("350"),
            proteins=Decimal("20.25"),
            fats=Decimal("10"),
            carbohydrates=Decimal("40"),
        ),
        cookingTime=Decimal("25"),
        instructions=[f"Шаг {index}" for index in range(1, steps + 1)],
    )


def menu_of(*recipes: RecipePayload) -> MenuPayload:
    return MenuPayload(recipes=list(recipes))


async def create_task(factory: Factory, people: int = 4, budget: str = "5000") -> UUID:
    async with factory() as session, session.begin():
        return await TaskRepository(session).create(people, Decimal(budget))


async def test_create_and_get_pending_task(session_factory: Factory) -> None:
    task_id = await create_task(session_factory, 3, "1234.5")
    async with session_factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None
    assert task.status == TaskStatus.PENDING
    assert task.menu_id is None and task.prompt is None and task.error is None
    assert task.parameters.people_count == 3
    assert task.parameters.weekly_budget == Decimal("1234.50")
    assert task.created_at.tzinfo is not None


async def test_get_unknown_task_returns_none(session_factory: Factory) -> None:
    async with session_factory() as session:
        assert await TaskRepository(session).get(uuid4()) is None


async def test_claim_takes_oldest_pending_and_marks_in_progress(
    session_factory: Factory,
) -> None:
    first = await create_task(session_factory)
    second = await create_task(session_factory)
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).claim_next() == first
    async with session_factory() as session:
        claimed = await TaskRepository(session).get(first)
        waiting = await TaskRepository(session).get(second)
    assert claimed is not None and claimed.status == TaskStatus.IN_PROGRESS
    assert waiting is not None and waiting.status == TaskStatus.PENDING


async def test_claim_returns_none_without_pending(session_factory: Factory) -> None:
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).claim_next() is None


async def test_concurrent_claims_never_take_the_same_task(
    session_factory: Factory,
) -> None:
    only = await create_task(session_factory)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> UUID | None:
        async with session_factory() as session, session.begin():
            claimed = await TaskRepository(session).claim_next()
            held.set()
            await release.wait()
            return claimed

    first = asyncio.create_task(holder())
    await held.wait()
    # Первая транзакция ещё держит строку: вторая обязана получить None, а не ждать.
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).claim_next() is None
    release.set()
    assert await first == only


async def test_two_pending_tasks_are_claimed_by_different_workers(
    session_factory: Factory,
) -> None:
    created = {await create_task(session_factory), await create_task(session_factory)}
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> UUID | None:
        async with session_factory() as session, session.begin():
            claimed = await TaskRepository(session).claim_next()
            held.set()
            await release.wait()
            return claimed

    first = asyncio.create_task(holder())
    await held.wait()
    async with session_factory() as session, session.begin():
        second = await TaskRepository(session).claim_next()
    release.set()
    assert {await first, second} == created


async def test_prompt_and_parameters_are_stored_and_read(
    session_factory: Factory,
) -> None:
    task_id = await create_task(session_factory, 2, "700")
    async with session_factory() as session, session.begin():
        repository = TaskRepository(session)
        await repository.set_prompt(task_id, "готовый промпт")
        parameters = await repository.get_parameters(task_id)
        assert parameters is not None
        assert (parameters.people_count, parameters.weekly_budget) == (2, 700)
    async with session_factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None and task.prompt == "готовый промпт"


async def claimed_task(factory: Factory) -> UUID:
    await create_task(factory)
    async with factory() as session, session.begin():
        task_id = await TaskRepository(session).claim_next()
    assert task_id is not None
    return task_id


async def test_mark_failed_is_conditional_on_in_progress(
    session_factory: Factory,
) -> None:
    pending = await create_task(session_factory)
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).mark_failed(pending, "x") == 0
    task_id = await claimed_task(session_factory)
    async with session_factory() as session, session.begin():
        repository = TaskRepository(session)
        assert await repository.mark_failed(task_id, "Neurocore timeout") == 1
        assert await repository.mark_failed(task_id, "second") == 0
    async with session_factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None
    assert (task.status, task.error) == (TaskStatus.FAILED, "Neurocore timeout")


async def test_mark_done_is_conditional_and_needs_in_progress(
    session_factory: Factory,
) -> None:
    task_id = await claimed_task(session_factory)
    async with session_factory() as session, session.begin():
        menu_id = await MenuRepository(session).save(
            menu_of(recipe("A", "Соль"), recipe("B", "Соль"), recipe("C", "Соль"))
        )
        repository = TaskRepository(session)
        assert await repository.mark_done(task_id, menu_id) == 1
        assert await repository.mark_done(task_id, menu_id) == 0
        assert await repository.mark_failed(task_id, "late") == 0
    async with session_factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None
    assert (task.status, task.menu_id, task.error) == (TaskStatus.DONE, menu_id, None)


async def test_fail_in_progress_only_touches_in_progress(
    session_factory: Factory,
) -> None:
    stuck = await claimed_task(session_factory)
    waiting = await create_task(session_factory)
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).fail_in_progress("Service restarted") == 1
    async with session_factory() as session:
        repository = TaskRepository(session)
        failed = await repository.get(stuck)
        pending = await repository.get(waiting)
    assert failed is not None and failed.error == "Service restarted"
    assert pending is not None and pending.status == TaskStatus.PENDING


async def test_saved_menu_is_loaded_back_in_full(session_factory: Factory) -> None:
    task_id = await claimed_task(session_factory)
    payload = menu_of(
        recipe("Омлет", "Яйцо", "Молоко", steps=3),
        recipe("Суп", "Вода", "Картофель"),
        recipe("Каша", "Овсянка", "Молоко"),
    )
    async with session_factory() as session, session.begin():
        menu_id = await MenuRepository(session).save(payload)
        await TaskRepository(session).mark_done(task_id, menu_id)
    async with session_factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None and task.menu is not None
    recipes = {link.recipe.name: link.recipe for link in task.menu.menu_recipes}
    assert set(recipes) == {"Омлет", "Суп", "Каша"}
    omelette = recipes["Омлет"]
    assert omelette.photo is None
    assert (omelette.calories, omelette.cooking_time) == (350, 25)
    assert omelette.nutrition.proteins == Decimal("20.25")
    assert [(item.position, item.text) for item in omelette.instructions] == [
        (1, "Шаг 1"),
        (2, "Шаг 2"),
        (3, "Шаг 3"),
    ]
    assert {
        (item.ingredient.name, item.quantity, item.unit)
        for item in omelette.ingredients
    } == {("Яйцо", Decimal("100.50"), "г"), ("Молоко", Decimal("100.50"), "г")}


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_new_ingredient_in_two_recipes_is_one_row(
    session_factory: Factory,
) -> None:
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(
            menu_of(
                recipe("A", "Соль", "Перец"), recipe("B", "Соль"), recipe("C", "Рис")
            )
        )
    async with session_factory() as session:
        names = (await session.scalars(select(Ingredient.name))).all()
        links = await count(session, RecipeIngredient)
    assert sorted(names) == ["Перец", "Рис", "Соль"]
    assert links == 4


async def test_existing_ingredient_is_reused_between_menus_but_recipes_are_new(
    session_factory: Factory,
) -> None:
    payload = menu_of(recipe("A", "Соль"), recipe("B", "Соль"), recipe("C", "Соль"))
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(payload)
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(payload)
    async with session_factory() as session:
        assert await count(session, Ingredient) == 1
        assert await count(session, Recipe) == 6
        assert await count(session, Menu) == 2
        assert await count(session, MenuRecipe) == 6


async def test_ingredient_is_found_by_name_key_and_stored_normalized(
    session_factory: Factory,
) -> None:
    payload = menu_of(
        recipe("A", "  Мёд   липовый "),
        recipe("B", "мед\u00a0липовый"),
        recipe("C", "«Мёд липовый»."),
    )
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(payload)
    async with session_factory() as session:
        names = (await session.scalars(select(Ingredient.name))).all()
        links = await count(session, RecipeIngredient)
    # Одна строка: имя первого вхождения, нормализованное (NFKC, пробелы, strip).
    assert names == ["Мёд липовый"]
    assert links == 3


async def test_existing_row_with_other_case_is_reused(session_factory: Factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(Ingredient(id=uuid4(), name="Яйцо"))
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(
            menu_of(recipe("A", "яйцо"), recipe("B", "Соль"), recipe("C", "Рис"))
        )
    async with session_factory() as session:
        names = (await session.scalars(select(Ingredient.name))).all()
    assert sorted(names) == ["Рис", "Соль", "Яйцо"]


async def test_duplicate_ingredient_names_in_db_do_not_break_save(
    session_factory: Factory,
) -> None:
    low, high = sorted([uuid4(), uuid4()])
    async with session_factory() as session, session.begin():
        session.add_all(
            [Ingredient(id=high, name="Соль"), Ingredient(id=low, name="Соль")]
        )
    async with session_factory() as session, session.begin():
        await MenuRepository(session).save(
            menu_of(recipe("A", "Соль"), recipe("B", "Перец"), recipe("C", "Рис"))
        )
    async with session_factory() as session:
        used = (
            await session.scalars(
                select(RecipeIngredient.ingredient_id)
                .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
                .where(Recipe.name == "A")
            )
        ).all()
    assert used == [low]


async def test_failure_in_the_middle_of_a_menu_rolls_everything_back(
    session_factory: Factory,
) -> None:
    task_id = await claimed_task(session_factory)
    broken = recipe("B", "Соль")
    # 201 символ не помещается в колонку: сбой случится при записи, а не в Python.
    broken.ingredients[0].unit = "г" * 201
    with pytest.raises(DBAPIError):
        async with session_factory() as session, session.begin():
            repository = MenuRepository(session)
            menu_id = await repository.save(menu_of(recipe("A", "Соль"), broken))
            await TaskRepository(session).mark_done(task_id, menu_id)
    async with session_factory() as session:
        for model in (Menu, Recipe, MenuRecipe, Ingredient, Instruction):
            assert await count(session, model) == 0
        task = await TaskRepository(session).get(task_id)
    assert task is not None and task.status == TaskStatus.IN_PROGRESS


async def test_active_template_is_the_latest_by_created_at_then_id(
    session_factory: Factory,
) -> None:
    async with session_factory() as session:
        assert await PromptTemplateRepository(session).get_active() is None
    now = datetime.now(UTC)
    low, high = sorted([uuid4(), uuid4()])
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                PromptTemplate(
                    id=uuid4(), template="old", created_at=now - timedelta(1)
                ),
                PromptTemplate(id=low, template="tie-low", created_at=now),
                PromptTemplate(id=high, template="tie-high", created_at=now),
            ]
        )
    async with session_factory() as session:
        active = await PromptTemplateRepository(session).get_active()
    assert active is not None and active.template == "tie-high"


async def test_queue_index_exists(session_factory: Factory) -> None:
    async with session_factory() as session:
        names = (
            await session.scalars(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'generation_task'"
                )
            )
        ).all()
    assert "ix_generation_task_status_created_at" in names
