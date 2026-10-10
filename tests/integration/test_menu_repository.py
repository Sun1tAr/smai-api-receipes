import os
import uuid

import pytest
from seed import menu_data
from sqlalchemy import text

from receipes.repositories import MenuRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def ingredient_names(session) -> list[str]:
    rows = await session.execute(text("SELECT name FROM ingredient ORDER BY name"))
    return [row[0] for row in rows]


async def test_saved_menu_is_loaded_in_the_saved_order(session_factory) -> None:
    generated = menu_data(
        ["Каша", "Суп", "Салат"],
        Каша=[("Овсянка", 100, "г"), ("Молоко", 200, "мл"), ("Мёд", 1, "ложка")],
    )
    async with session_factory() as session:
        menu_id = await MenuRepository(session).save(generated)
        await session.commit()
    async with session_factory() as session:
        menu = await MenuRepository(session).load(menu_id)
        assert [link.position for link in menu.menu_recipes] == [1, 2, 3]
        recipes = [link.recipe for link in menu.menu_recipes]
        assert [recipe.name for recipe in recipes] == ["Каша", "Суп", "Салат"]
        first = recipes[0]
        assert [
            (item.ingredient.name, item.quantity, item.unit, item.position)
            for item in first.recipe_ingredients
        ] == [
            ("овсянка", 100, "г", 1),
            ("молоко", 200, "мл", 2),
            ("мед", 1, "ложка", 3),
        ]
        assert [step.text for step in first.instructions] == [
            "Первый шаг.",
            "Второй шаг.",
            "Третий шаг.",
        ]
        assert [step.position for step in first.instructions] == [1, 2, 3]
        assert first.nutrition.proteins == 12
        assert (first.calories, first.cooking_time, first.photo) == (320, 15, None)


async def test_unknown_menu_is_not_found(session) -> None:
    assert await MenuRepository(session).load(uuid.uuid4()) is None


async def test_ingredients_are_reused_between_recipes_and_menus(session) -> None:
    repository = MenuRepository(session)
    first = await repository.save(menu_data())
    second = await repository.save(menu_data(["Пирог", "Блины", "Омлет"]))
    assert first != second
    # «молоко» и «мука» у шести рецептов двух меню — ровно две строки.
    assert await ingredient_names(session) == ["молоко", "мука"]
    links = await session.scalar(text("SELECT count(*) FROM recipe_ingredients"))
    assert links == 12


async def test_ingredient_spelling_variants_are_one_ingredient(session) -> None:
    generated = menu_data(
        ["Каша", "Суп", "Салат"],
        Каша=[("Мёд", 1, "ложка")],
        Суп=[("  МЕД ", 2, "ложка")],
        Салат=[("мед", 3, "ложка")],
    )
    menu_id = await MenuRepository(session).save(generated)
    assert await ingredient_names(session) == ["мед"]
    # Нормализованное имя в БД совпадает с индексом lower(btrim(name)).
    menu = await MenuRepository(session).load(menu_id)
    assert {
        link.recipe.recipe_ingredients[0].ingredient.name for link in menu.menu_recipes
    } == {"мед"}


async def test_existing_ingredient_with_other_case_is_reused(session) -> None:
    await session.execute(
        text("INSERT INTO ingredient VALUES (gen_random_uuid(), 'молоко')")
    )
    await MenuRepository(session).save(menu_data())
    assert await ingredient_names(session) == ["молоко", "мука"]


async def test_failed_save_leaves_nothing_behind(session_factory) -> None:
    generated = menu_data()
    generated.recipes[0].name = "я" * 300  # обходит «ворота», ловится колонкой
    async with session_factory() as session:
        with pytest.raises(Exception):  # noqa: B017  # DBAPIError драйвера
            await MenuRepository(session).save(generated)
        await session.rollback()
    async with session_factory() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM menu), "
                    "(SELECT count(*) FROM recipe), "
                    "(SELECT count(*) FROM ingredient)"
                )
            )
        ).one()
    assert tuple(counts) == (0, 0, 0)
