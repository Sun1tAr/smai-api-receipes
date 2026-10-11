import os
import uuid

import pytest
from sqlalchemy import text

from receipes.domain import MenuNotFound
from receipes.repositories import MenuRepository
from tests.integration.seed import menu_data

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
            ("Овсянка", 100, "г", 1),
            ("Молоко", 200, "мл", 2),
            ("Мёд", 1, "ложка", 3),
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
    missing = uuid.uuid4()
    with pytest.raises(MenuNotFound) as error:
        await MenuRepository(session).load(missing)
    assert error.value.menu_id == missing


async def test_ingredients_are_reused_between_recipes_and_menus(session) -> None:
    repository = MenuRepository(session)
    first = await repository.save(menu_data())
    second = await repository.save(menu_data(["Пирог", "Блины", "Омлет"]))
    assert first != second
    # «молоко» и «мука» у шести рецептов двух меню — ровно две строки.
    assert await ingredient_names(session) == ["Молоко", "Мука"]
    links = await session.scalar(text("SELECT count(*) FROM recipe_ingredients"))
    assert links == 12


async def test_ingredient_spelling_variants_are_one_ingredient(session) -> None:
    generated = menu_data(
        ["Каша", "Суп", "Салат"],
        Каша=[("Мёд", 1, "ложка")],
        Суп=[("мед ", 2, "ложка")],
        Салат=[("МЁД", 3, "ложка")],
    )
    repository = MenuRepository(session)
    menu_id = await repository.save(generated)
    # Показывается первое написание, ключ дедупликации — отдельная колонка.
    assert await ingredient_names(session) == ["Мёд"]
    assert await session.scalar(text("SELECT name_key FROM ingredient")) == "мед"
    menu = await repository.load(menu_id)
    assert {
        link.recipe.recipe_ingredients[0].ingredient.name for link in menu.menu_recipes
    } == {"Мёд"}


async def test_later_menu_does_not_change_the_display_name(session) -> None:
    repository = MenuRepository(session)
    await repository.save(menu_data(["Каша", "Суп", "Салат"], Каша=[("Мёд", 1, "г")]))
    second = await repository.save(
        menu_data(["Пирог", "Блины", "Омлет"], Пирог=[("мёд", 1, "г")])
    )
    assert set(await ingredient_names(session)) == {"Мёд", "Молоко", "Мука"}
    menu = await repository.load(second)
    names = [i.ingredient.name for i in menu.menu_recipes[0].recipe.recipe_ingredients]
    assert names == ["Мёд"]


async def test_existing_ingredient_with_other_spelling_is_reused(session) -> None:
    await session.execute(
        text("INSERT INTO ingredient VALUES (gen_random_uuid(), 'Молоко', 'молоко')")
    )
    await MenuRepository(session).save(menu_data())
    assert await ingredient_names(session) == ["Молоко", "Мука"]


async def test_save_survives_manually_inserted_ingredients(session) -> None:
    # Строка, вставленная в обход приложения, с произвольным name: ключ решает всё.
    await session.execute(
        text("INSERT INTO ingredient VALUES (gen_random_uuid(), 'MOLOKO!', 'молоко')")
    )
    await MenuRepository(session).save(menu_data())
    assert await session.scalar(text("SELECT count(*) FROM ingredient")) == 2


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
