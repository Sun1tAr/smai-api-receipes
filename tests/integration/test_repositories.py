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
    ClaimedTask,
    MenuRepository,
    PromptTemplateRepository,
    TaskRepository,
)
from receipes.domain.errors import TaskStateConflict
from receipes.domain.menu import (
    IngredientPayload,
    MenuPayload,
    NutritionPayload,
    RecipePayload,
)
from receipes.domain.params import GenerationParams
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
        return await TaskRepository(session).create(
            GenerationParams(people, Decimal(budget))
        )


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
        claim = await TaskRepository(session).claim_next()
        assert claim is not None and claim.task_id == first
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

    async def holder() -> ClaimedTask | None:
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
    held_claim = await first
    assert held_claim is not None and held_claim.task_id == only


async def test_two_pending_tasks_are_claimed_by_different_workers(
    session_factory: Factory,
) -> None:
    created = {await create_task(session_factory), await create_task(session_factory)}
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> ClaimedTask | None:
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
    claims = [await first, second]
    assert {claim.task_id for claim in claims if claim} == created


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
        claim = await TaskRepository(session).claim_next()
    assert claim is not None
    return claim.task_id


async def test_mark_failed_is_conditional_on_in_progress(
    session_factory: Factory,
) -> None:
    pending = await create_task(session_factory)
    async with session_factory() as session, session.begin():
        assert await TaskRepository(session).mark_failed(pending, "x") is None
    task_id = await claimed_task(session_factory)
    async with session_factory() as session, session.begin():
        repository = TaskRepository(session)
        assert await repository.mark_failed(task_id, "Neurocore timeout") is not None
        assert await repository.mark_failed(task_id, "second") is None
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
        assert await repository.mark_done(task_id, menu_id) is not None
        assert await repository.mark_done(task_id, menu_id) is None
        assert await repository.mark_failed(task_id, "late") is None
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
        recovered = await TaskRepository(session).fail_in_progress("Service restarted")
        assert len(recovered) == 1
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


async def source_menu(factory: Factory) -> tuple[UUID, UUID, dict[str, UUID]]:
    async with factory() as session, session.begin():
        tasks = TaskRepository(session)
        source_id = await tasks.create(GenerationParams(3, Decimal("750")), user_id=1)
        await tasks.claim_next()
        menu_id = await MenuRepository(session).save(
            menu_of(
                recipe("Омлет", "Соль"), recipe("Суп", "Вода"), recipe("Каша", "Соль")
            )
        )
        await tasks.mark_done(source_id, menu_id)
        source = await tasks.get(source_id)
        recipes = {
            slot.recipe.name: slot.recipe_id for slot in source.menu.menu_recipes
        }
    return source_id, menu_id, recipes


async def test_replacement_keeps_parameters_and_exact_result(
    session_factory: Factory,
) -> None:
    source, menu, old = await source_menu(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        latest = await tasks.create(GenerationParams(2, Decimal("250")), user_id=1)
        await tasks.claim_next()
        await tasks.mark_done(latest, menu)
        assert await tasks.create_replace(uuid4(), old["Омлет"]) is None
        assert await tasks.create_replace(source, uuid4()) is None
        task_id = await tasks.create_replace(source, old["Омлет"])
        assert task_id is not None
        assert await tasks.create_replace(task_id, old["Омлет"]) is None
        claim = await tasks.claim_next()
        assert claim is not None and (claim.task_id, claim.kind) == (task_id, "replace")
        assert await tasks.mark_done(task_id, menu) is None
        context = await tasks.get_replace_context(task_id, 120)
        assert context is not None
        assert context.replaced_name == "Омлет"
        assert set(context.recipe_names) == set(old)
        assert 0 < context.remaining_seconds <= 120
        assert await tasks.finish_replace(task_id, recipe("Рагу", "Соль"), 120)

    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        replaced = await tasks.get_replacement(task_id)
        assert replaced is not None and replaced.result_recipe is not None
        first_result_id = replaced.result_recipe.id
        assert await tasks.expire_replace(task_id, 0, "deadline") is None
        assert replaced.source_task_id == source
        assert replaced.replaced_recipe_id == old["Омлет"]
        assert replaced.menu_id == menu and replaced.status == TaskStatus.DONE
        assert replaced.result_recipe.ingredients[0].ingredient.name == "Соль"
        assert len(replaced.result_recipe.instructions) == 2
        assert replaced.result_recipe.nutrition.proteins == Decimal("20.25")
        assert await tasks.get_params(task_id) == GenerationParams(3, Decimal("750"))
        saved = await tasks.find_last_done_parameters(1)
        assert saved is not None and saved.people_count == 2
        assert await tasks.create_replace(task_id, first_result_id) is None
        assert await tasks.create_replace(source, old["Омлет"]) is None
        next_task_id = await tasks.create_replace(source, first_result_id)
        assert next_task_id is not None
        await tasks.claim_next()
        assert await tasks.finish_replace(next_task_id, recipe("Рис", "Вода"), 120)

    async with session_factory() as session:
        tasks = TaskRepository(session)
        first = await tasks.get_replacement(task_id)
        current = await tasks.get(source)
        assert first is not None and first.result_recipe is not None
        assert current is not None and current.menu is not None
        assert (first.result_recipe.id, first.result_recipe.name) == (
            first_result_id,
            "Рагу",
        )
        slots = {slot.recipe.name: slot.recipe_id for slot in current.menu.menu_recipes}
        assert slots == {"Каша": old["Каша"], "Суп": old["Суп"], "Рис": slots["Рис"]}
        assert await session.get(Recipe, old["Омлет"]) is not None
        assert await tasks.get_replacement(source) is None


@pytest.mark.parametrize("duplicate", ["«ОМЛЕТ!»", "суп."])
async def test_replacement_rejects_a_duplicate_name(
    session_factory: Factory,
    duplicate: str,
) -> None:
    source, _, old = await source_menu(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        task_id = await tasks.create_replace(source, old["Омлет"])
        assert task_id is not None
        await tasks.claim_next()
        with pytest.raises(TaskStateConflict):
            await tasks.finish_replace(task_id, recipe(duplicate, "Масло"), 120)
        assert await session.scalar(select(func.count(Recipe.id))) == 3
        assert await session.scalar(select(func.count(Ingredient.id))) == 2


async def test_concurrent_replacements_only_swap_one_slot_once(
    session_factory: Factory,
) -> None:
    source, menu, old = await source_menu(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        first = await tasks.create_replace(source, old["Омлет"])
        second = await tasks.create_replace(source, old["Омлет"])
        assert first is not None and second is not None
        await tasks.claim_next()
        await tasks.claim_next()

    async def swap(task_id: UUID, name: str) -> bool:
        try:
            async with session_factory() as session, session.begin():
                assert await TaskRepository(session).finish_replace(
                    task_id, recipe(name, "Масло"), 120
                )
        except TaskStateConflict:
            return False
        return True

    assert sum(await asyncio.gather(swap(first, "Рагу"), swap(second, "Пюре"))) == 1
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Recipe.id))) == 4
        links = await session.scalars(
            select(MenuRecipe.recipe_id).where(MenuRecipe.menu_id == menu)
        )
        ids = set(links)
        assert len(ids) == 3
        assert old["Омлет"] not in ids and {old["Суп"], old["Каша"]} <= ids


@pytest.mark.parametrize("claimed", [False, True])
async def test_replacement_expiration_wins_once_and_blocks_late_writes(
    session_factory: Factory,
    claimed: bool,
) -> None:
    source, menu, old = await source_menu(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        task_id = await tasks.create_replace(source, old["Омлет"])
        assert task_id is not None
        claim = await tasks.claim_next() if claimed else None
        assert await tasks.expire_replace(task_id, 120, "deadline") is None
        await session.execute(
            text(
                "UPDATE generation_task SET created_at = now() - interval '3 min'"
                " WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        )
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        expired = await tasks.expire_replace(task_id, 120, "deadline")
        assert expired is not None and expired.kind == "replace"
        assert expired.claimed_at == (claim.claimed_at if claim else None)
        assert await tasks.expire_replace(task_id, 120, "deadline") is None
        assert await tasks.mark_failed(task_id, "late") is None
        await tasks.set_prompt(task_id, "late")
        assert await tasks.finish_replace(task_id, recipe("Рагу", "Масло"), 120) is None
        failed = await tasks.get_replacement(task_id)
        assert failed is not None
        assert (failed.status, failed.error, failed.prompt) == (
            TaskStatus.FAILED,
            "deadline",
            None,
        )
        assert failed.menu_id == menu and failed.result_recipe_id is None
        assert await session.scalar(select(func.count(Recipe.id))) == 3


async def test_replacement_rechecks_deadline_after_waiting_for_menu_lock(
    session_factory: Factory,
) -> None:
    source, menu, old = await source_menu(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        task_id = await tasks.create_replace(source, old["Омлет"])
        assert task_id is not None
        await tasks.claim_next()
        await session.execute(
            text(
                "UPDATE generation_task"
                " SET created_at = clock_timestamp() - interval '119 s'"
                " WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        )

    waiting_pid: asyncio.Queue[int] = asyncio.Queue()

    async def finish():
        async with session_factory() as session, session.begin():
            await waiting_pid.put(await session.scalar(select(func.pg_backend_pid())))
            return await TaskRepository(session).finish_replace(
                task_id, recipe("Рагу", "Масло"), 120
            )

    async with session_factory() as session, session.begin():
        await session.execute(select(Menu.id).where(Menu.id == menu).with_for_update())
        finisher = asyncio.create_task(finish())
        async with asyncio.timeout(2):
            pid = await waiting_pid.get()
            while not await session.scalar(select(func.pg_blocking_pids(pid))):
                await asyncio.sleep(0.01)
        await session.execute(select(func.pg_sleep(1.05)))

    assert await finisher is None
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        assert await tasks.expire_replace(task_id, 120, "deadline")
        assert await session.scalar(select(func.count(Recipe.id))) == 3


async def test_replacement_rolls_back_when_deadline_passes_during_save(
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, old = await source_menu(session_factory)
    original_save = MenuRepository.save_recipe
    saved_recipe: UUID | None = None

    async def slow_save(repository: MenuRepository, payload: RecipePayload) -> UUID:
        nonlocal saved_recipe
        saved_recipe = await original_save(repository, payload)
        await asyncio.sleep(1.05)
        return saved_recipe

    monkeypatch.setattr(MenuRepository, "save_recipe", slow_save)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        task_id = await tasks.create_replace(source, old["Омлет"])
        assert task_id is not None
        await tasks.claim_next()
        await session.execute(
            text(
                "UPDATE generation_task"
                " SET created_at = clock_timestamp() - interval '119 s'"
                " WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        )
        assert await tasks.finish_replace(task_id, recipe("Рагу", "Масло"), 120) is None
        assert saved_recipe is not None
        assert await tasks.expire_replace(task_id, 120, "deadline")

    async with session_factory() as session:
        assert await session.get(Recipe, saved_recipe) is None
        assert await session.scalar(select(func.count(Instruction.id))) == 6
        assert await session.scalar(select(func.count(Ingredient.id))) == 2
        current = await TaskRepository(session).get(source)
        assert current is not None and current.menu is not None
        assert {slot.recipe_id for slot in current.menu.menu_recipes} == set(
            old.values()
        )


async def test_claim_cannot_skip_a_disabled_menu_at_the_head(
    session_factory: Factory,
) -> None:
    source, _, old = await source_menu(session_factory)
    pending_menu = await create_task(session_factory)
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        replacement = await tasks.create_replace(source, old["Омлет"])
        assert replacement is not None
        assert await tasks.claim_next(menu_enabled=False) is None
        first = await tasks.claim_next()
        assert first is not None and (first.task_id, first.kind) == (
            pending_menu,
            "menu",
        )
        second = await tasks.claim_next(menu_enabled=False)
        assert second is not None and (second.task_id, second.kind) == (
            replacement,
            "replace",
        )
