import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from receipes.migration_diagnostics import find_postgres_error

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

CHECK, UNIQUE, FK, NOT_NULL, TOO_LONG = "23514", "23505", "23503", "23502", "22001"

IDS = {
    name: str(uuid.uuid4())
    for name in (
        "nutrition",
        "recipe",
        "recipe2",
        "menu",
        "ingredient",
        "params",
        "params2",
        "task",
        "missing",
    )
}


# Родительские строки, на которые ссылаются проверяемые вставки.
@pytest.fixture
async def parents(session_factory):
    async with session_factory() as session:
        for sql in (
            "INSERT INTO nutrition VALUES (:nutrition, 1, 1, 1, 1)",
            "INSERT INTO recipe VALUES (:recipe, 'r', 1, 1, NULL, :nutrition)",
            "INSERT INTO recipe VALUES (:recipe2, 'r2', 1, 1, NULL, :nutrition)",
            "INSERT INTO menu VALUES (:menu)",
            "INSERT INTO ingredient VALUES (:ingredient, 'Молоко', 'молоко')",
            "INSERT INTO generation_parameters VALUES (:params, 2, 100)",
            "INSERT INTO generation_parameters VALUES (:params2, 2, 100)",
            "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :menu, :recipe, 1)",
            "INSERT INTO recipe_ingredients VALUES "
            "(gen_random_uuid(), :recipe, :ingredient, 1, 'г', 1)",
            "INSERT INTO instruction VALUES (gen_random_uuid(), :recipe, 'шаг', 1)",
            "INSERT INTO generation_task (task_id, status, parameters_id) "
            "VALUES (:task, 'pending', :params)",
        ):
            await session.execute(text(sql), IDS)
        await session.commit()
    return session_factory


async def violation(factory, sql: str, sqlstate: str, constraint: str | None) -> None:
    async with factory() as session:
        with pytest.raises(DBAPIError) as error:
            await session.execute(text(sql), IDS)
        driver_error = find_postgres_error(error.value)
        assert driver_error.sqlstate == sqlstate
        if constraint is not None:
            assert driver_error.constraint_name == constraint


TASK = (
    "INSERT INTO generation_task (task_id, status, prompt, parameters_id, menu_id, "
    "error, started_at, finished_at) VALUES (gen_random_uuid(), {values})"
)


def task(status, prompt="'p'", menu=None, error=None, started=None, finished=None):
    values = (
        f"'{status}', {prompt}, :params2, {menu or 'NULL'}, {error or 'NULL'}, "
        f"{started or 'NULL'}, {finished or 'NULL'}"
    )
    return TASK.format(values=values)


NOW = "now()"
CASES = [
    # CHECK
    *[
        (
            "INSERT INTO generation_parameters VALUES "
            f"(gen_random_uuid(), {people}, 10)",
            CHECK,
            "ck_generation_parameters_people_count_range",
        )
        for people in (0, 21, 25)
    ],
    *[
        (
            "INSERT INTO generation_parameters VALUES "
            f"(gen_random_uuid(), 1, {budget})",
            CHECK,
            "ck_generation_parameters_weekly_budget_range",
        )
        for budget in (0, -1, "1000000.01")
    ],
    (
        "INSERT INTO nutrition VALUES (gen_random_uuid(), -1, 0, 0, 0)",
        CHECK,
        "ck_nutrition_values_non_negative",
    ),
    (
        "INSERT INTO recipe VALUES (gen_random_uuid(), 'x', 1, -1, NULL, :nutrition)",
        CHECK,
        "ck_recipe_values_non_negative",
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :recipe2, :ingredient, -1, 'г', 1)",
        CHECK,
        "ck_recipe_ingredients_quantity_non_negative",
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :recipe2, :ingredient, 1, 'г', 0)",
        CHECK,
        "ck_recipe_ingredients_position_positive",
    ),
    (
        "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :menu, :recipe2, 0)",
        CHECK,
        "ck_menu_recipes_position_positive",
    ),
    (
        "INSERT INTO instruction VALUES (gen_random_uuid(), :recipe2, 'x', 0)",
        CHECK,
        "ck_instruction_position_positive",
    ),
    (task("bogus"), CHECK, "ck_generation_task_status_valid"),
    (
        task("done", started=NOW, finished=NOW),
        CHECK,
        "ck_generation_task_menu_iff_done",
    ),
    (task("pending", menu=":menu"), CHECK, "ck_generation_task_menu_iff_done"),
    (
        task("failed", started=NOW, finished=NOW),
        CHECK,
        "ck_generation_task_error_iff_failed",
    ),
    (task("pending", error="'boom'"), CHECK, "ck_generation_task_error_iff_failed"),
    (
        task("in_progress", prompt="NULL", started=NOW),
        CHECK,
        "ck_generation_task_prompt_when_started",
    ),
    (
        task("done", prompt="NULL", menu=":menu", started=NOW, finished=NOW),
        CHECK,
        "ck_generation_task_prompt_when_started",
    ),
    (task("in_progress"), CHECK, "ck_generation_task_started_at_when_started"),
    (
        task("done", menu=":menu", finished=NOW),
        CHECK,
        "ck_generation_task_started_at_when_started",
    ),
    (
        task("done", menu=":menu", started=NOW),
        CHECK,
        "ck_generation_task_finished_at_when_finished",
    ),
    (
        task("failed", error="'boom'"),
        CHECK,
        "ck_generation_task_finished_at_when_finished",
    ),
    # UNIQUE
    (
        "INSERT INTO generation_task (task_id, status, parameters_id) "
        "VALUES (gen_random_uuid(), 'pending', :params)",
        UNIQUE,
        "uq_generation_task_parameters_id",
    ),
    (
        "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :menu, :recipe, 2)",
        UNIQUE,
        "uq_menu_recipes_menu_id_recipe_id",
    ),
    (
        "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :menu, :recipe2, 1)",
        UNIQUE,
        "uq_menu_recipes_menu_id_position",
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :recipe, :ingredient, 1, 'г', 2)",
        UNIQUE,
        "uq_recipe_ingredients_recipe_id_ingredient_id",
    ),
    (
        "INSERT INTO instruction VALUES (gen_random_uuid(), :recipe, 'x', 1)",
        UNIQUE,
        "uq_instruction_recipe_id_position",
    ),
    (
        "INSERT INTO ingredient VALUES (gen_random_uuid(), 'Другое', 'молоко')",
        UNIQUE,
        "uq_ingredient_name_key",
    ),
    # FOREIGN KEY
    (
        "INSERT INTO generation_task (task_id, status, parameters_id) "
        "VALUES (gen_random_uuid(), 'pending', :missing)",
        FK,
        "fk_generation_task_parameters_id_generation_parameters",
    ),
    (
        task("done", menu=":missing", started=NOW, finished=NOW),
        FK,
        "fk_generation_task_menu_id_menu",
    ),
    (
        "INSERT INTO recipe VALUES (gen_random_uuid(), 'x', 1, 1, NULL, :missing)",
        FK,
        "fk_recipe_nutrition_id_nutrition",
    ),
    (
        "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :missing, :recipe2, 5)",
        FK,
        "fk_menu_recipes_menu_id_menu",
    ),
    (
        "INSERT INTO menu_recipes VALUES (gen_random_uuid(), :menu, :missing, 5)",
        FK,
        "fk_menu_recipes_recipe_id_recipe",
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :missing, :ingredient, 1, 'г', 5)",
        FK,
        "fk_recipe_ingredients_recipe_id_recipe",
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :recipe2, :missing, 1, 'г', 5)",
        FK,
        "fk_recipe_ingredients_ingredient_id_ingredient",
    ),
    (
        "INSERT INTO instruction VALUES (gen_random_uuid(), :missing, 'x', 5)",
        FK,
        "fk_instruction_recipe_id_recipe",
    ),
    # NOT NULL и длины
    (
        "INSERT INTO generation_task (task_id, status, parameters_id) "
        "VALUES (gen_random_uuid(), 'pending', NULL)",
        NOT_NULL,
        None,
    ),
    (
        "INSERT INTO recipe VALUES "
        "(gen_random_uuid(), repeat('я', 256), 1, 1, NULL, :nutrition)",
        TOO_LONG,
        None,
    ),
    (
        "INSERT INTO recipe_ingredients VALUES "
        "(gen_random_uuid(), :recipe2, :ingredient, 1, repeat('г', 33), 5)",
        TOO_LONG,
        None,
    ),
    (
        "INSERT INTO ingredient VALUES (gen_random_uuid(), repeat('я', 256), 'k')",
        TOO_LONG,
        None,
    ),
]


@pytest.mark.parametrize(("sql", "sqlstate", "constraint"), CASES)
async def test_constraint_violation_is_rejected(
    parents, sql: str, sqlstate: str, constraint: str | None
) -> None:
    await violation(parents, sql, sqlstate, constraint)


@pytest.mark.parametrize(("people", "budget"), [(1, "0.01"), (20, "1000000")])
async def test_parameter_bounds_are_inclusive(parents, people, budget) -> None:
    async with parents() as session:
        await session.execute(
            text(
                "INSERT INTO generation_parameters VALUES "
                "(gen_random_uuid(), :people, :budget)"
            ),
            {"people": people, "budget": budget},
        )
        await session.commit()


async def test_valid_task_rows_are_accepted(parents) -> None:
    async with parents() as session:
        for sql in (
            task("pending"),
            task("in_progress", started=NOW),
            task("failed", error="'boom'", finished=NOW),
        ):
            # Каждая строка нуждается в своих параметрах (1:1).
            params = str(uuid.uuid4())
            await session.execute(
                text("INSERT INTO generation_parameters VALUES (:p, 1, 1)"),
                {"p": params},
            )
            await session.execute(text(sql.replace(":params2", f"'{params}'")), IDS)
        await session.commit()


async def test_deleting_a_menu_cascades_to_menu_recipes_only(parents) -> None:
    async with parents() as session:
        await session.execute(text("DELETE FROM menu WHERE id = :menu"), IDS)
        counts = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM menu_recipes), "
                    "(SELECT count(*) FROM recipe)"
                )
            )
        ).one()
    assert tuple(counts) == (0, 2)


async def test_deleting_a_recipe_cascades_to_ingredients_and_steps(parents) -> None:
    async with parents() as session:
        await session.execute(text("DELETE FROM menu_recipes"))
        await session.execute(text("DELETE FROM recipe WHERE id = :recipe"), IDS)
        counts = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM recipe_ingredients), "
                    "(SELECT count(*) FROM instruction), "
                    "(SELECT count(*) FROM ingredient), "
                    "(SELECT count(*) FROM nutrition)"
                )
            )
        ).one()
    assert tuple(counts) == (0, 0, 1, 1)


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("DELETE FROM recipe WHERE id = :recipe", "fk_menu_recipes_recipe_id_recipe"),
        (
            "DELETE FROM ingredient WHERE id = :ingredient",
            "fk_recipe_ingredients_ingredient_id_ingredient",
        ),
        (
            "DELETE FROM nutrition WHERE id = :nutrition",
            "fk_recipe_nutrition_id_nutrition",
        ),
        (
            "DELETE FROM generation_parameters WHERE id = :params",
            "fk_generation_task_parameters_id_generation_parameters",
        ),
    ],
)
async def test_referenced_rows_cannot_be_deleted(parents, sql, constraint) -> None:
    await violation(parents, sql, FK, constraint)


async def test_menu_referenced_by_a_task_cannot_be_deleted(parents) -> None:
    async with parents() as session:
        await session.execute(
            text(
                "UPDATE generation_task SET status = 'done', menu_id = :menu, "
                "prompt = 'p', started_at = now(), finished_at = now() "
                "WHERE task_id = :task"
            ),
            IDS,
        )
        await session.commit()
    await violation(
        parents,
        "DELETE FROM menu WHERE id = :menu",
        FK,
        "fk_generation_task_menu_id_menu",
    )
