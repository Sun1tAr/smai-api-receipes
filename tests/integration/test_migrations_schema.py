import os
import subprocess
import uuid

import asyncpg
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

DOMAIN_TABLES = {
    "prompt_template",
    "generation_parameters",
    "generation_task",
    "menu",
    "menu_recipes",
    "recipe",
    "recipe_ingredients",
    "ingredient",
    "instruction",
    "nutrition",
}
BASE_REVISION = "0001_base_schema"


def alembic(env: dict[str, str], *arguments: str) -> None:
    result = subprocess.run(
        ["alembic", *arguments], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def connection(settings):
    connection = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password.get_secret_value(),
        database=settings.db_name,
    )
    yield connection
    await connection.close()


async def tables(connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
    )
    return {row["table_name"] for row in rows}


async def columns(connection, table: str) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
        table,
    )
    return {row["column_name"] for row in rows}


async def test_upgrade_downgrade_upgrade_and_schema_matches_models(
    connection, alembic_env, migrated_database
) -> None:
    try:
        alembic(alembic_env, "downgrade", "base")
        assert await tables(connection) == set()
        assert (
            await connection.fetch(
                "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
            == []
        )
        alembic(alembic_env, "upgrade", "head")
        assert await tables(connection) == DOMAIN_TABLES
        alembic(alembic_env, "check")
    finally:
        alembic(alembic_env, "upgrade", "head")


async def test_erd_deviations_are_a_separate_reversible_revision(
    connection, alembic_env, migrated_database
) -> None:
    try:
        alembic(alembic_env, "downgrade", BASE_REVISION)
        assert await tables(connection) == DOMAIN_TABLES
        assert "position" not in await columns(connection, "menu_recipes")
        assert "position" not in await columns(connection, "recipe_ingredients")
        assert "position" in await columns(connection, "instruction")
        task_columns = await columns(connection, "generation_task")
        assert not {"started_at", "finished_at"} & task_columns
        assert "name_key" not in await columns(connection, "ingredient")
        # В базовой схеме ключа ингредиента нет, уникальных индексов по имени тоже.
        assert not await connection.fetch(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'ingredient' "
            "AND indexname <> 'pk_ingredient'"
        )
        alembic(alembic_env, "upgrade", "head")
        assert "position" in await columns(connection, "menu_recipes")
        assert {"started_at", "finished_at"} <= await columns(
            connection, "generation_task"
        )
    finally:
        alembic(alembic_env, "upgrade", "head")


async def test_partial_indexes_exist(connection, migrated_database) -> None:
    rows = await connection.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'generation_task'"
    )
    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert "pending" in definitions["ix_generation_task_created_at_pending"]
    assert "WHERE" in definitions["ix_generation_task_created_at_pending"]
    assert "in_progress" in definitions["ix_generation_task_started_at_in_progress"]
    ingredient = await connection.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_ingredient_name_key'"
    )
    assert "UNIQUE" in ingredient and "(name_key)" in ingredient


async def test_erd_deviations_backfill_existing_rows(
    connection, alembic_env, migrated_database
) -> None:
    ids = {name: uuid.uuid4() for name in ("n", "r", "m", "i")}
    try:
        alembic(alembic_env, "downgrade", BASE_REVISION)
        await connection.execute("TRUNCATE menu, recipe, nutrition, ingredient CASCADE")
        await connection.execute(
            "INSERT INTO nutrition VALUES ($1, 1, 1, 1, 1)", ids["n"]
        )
        await connection.execute(
            "INSERT INTO recipe VALUES ($1, 'r', 1, 1, NULL, $2)", ids["r"], ids["n"]
        )
        await connection.execute("INSERT INTO menu VALUES ($1)", ids["m"])
        await connection.execute(
            "INSERT INTO menu_recipes VALUES (gen_random_uuid(), $1, $2)",
            ids["m"],
            ids["r"],
        )
        await connection.execute("INSERT INTO ingredient VALUES ($1, 'Мёд')", ids["i"])
        await connection.execute(
            "INSERT INTO ingredient VALUES (gen_random_uuid(), '  Овсяные   ХЛОПЬЯ ')"
        )
        await connection.execute(
            "INSERT INTO recipe_ingredients VALUES (gen_random_uuid(), $1, $2, 1, 'г')",
            ids["r"],
            ids["i"],
        )
        params = uuid.uuid4()
        await connection.execute(
            "INSERT INTO generation_parameters VALUES ($1, 1, 1)", params
        )
        await connection.execute(
            "INSERT INTO generation_task (task_id, status, prompt, parameters_id, "
            "menu_id) VALUES (gen_random_uuid(), 'done', 'p', $1, $2)",
            params,
            ids["m"],
        )
        alembic(alembic_env, "upgrade", "head")
        assert await connection.fetchval("SELECT position FROM menu_recipes") == 1
        assert await connection.fetchval("SELECT position FROM recipe_ingredients") == 1
        row = await connection.fetchrow(
            "SELECT created_at, started_at, finished_at FROM generation_task"
        )
        assert row["started_at"] == row["created_at"] == row["finished_at"]
        # Ключ считается Python-функцией; в имени схлопнуты пробелы, регистр цел.
        keys = await connection.fetch("SELECT name, name_key FROM ingredient")
        assert {(r["name"], r["name_key"]) for r in keys} == {
            ("Мёд", "мед"),
            ("Овсяные ХЛОПЬЯ", "овсяные хлопья"),
        }
    finally:
        alembic(alembic_env, "upgrade", "head")
        await connection.execute(
            "TRUNCATE generation_task, generation_parameters, menu, recipe, "
            "nutrition, ingredient CASCADE"
        )


SEED_ID = "6f3b1c0e-5d2a-4f7e-9a41-0c8b7d2e1a10"


async def test_seed_revision_inserts_the_prompt_template_and_is_reversible(
    connection, alembic_env, migrated_database, settings
) -> None:
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from receipes.database import create_engine
    from receipes.domain import GenerationParameters
    from receipes.prompt import build_prompt
    from receipes.repositories import PromptTemplateRepository

    try:
        alembic(alembic_env, "downgrade", "0002_erd_deviations")
        assert await connection.fetchval("SELECT count(*) FROM prompt_template") == 0
        alembic(alembic_env, "upgrade", "head")
        assert await connection.fetchval("SELECT count(*) FROM prompt_template") == 1
        alembic(alembic_env, "check")
        engine = create_engine(settings)
        try:
            async with AsyncSession(engine) as session:
                latest = await PromptTemplateRepository(session).latest()
        finally:
            await engine.dispose()
        assert latest is not None and str(latest.id) == SEED_ID
        prompt = build_prompt(latest.template, GenerationParameters(2, Decimal("5000")))
        assert "2 чел." in prompt and "5000.00 руб." in prompt
    finally:
        alembic(alembic_env, "upgrade", "head")


async def test_seed_downgrade_keeps_other_templates(
    connection, alembic_env, migrated_database
) -> None:
    try:
        alembic(alembic_env, "upgrade", "head")
        await connection.execute(
            "INSERT INTO prompt_template (id, template) VALUES ($1, 'custom')",
            uuid.uuid4(),
        )
        alembic(alembic_env, "downgrade", "0002_erd_deviations")
        rows = await connection.fetch("SELECT template FROM prompt_template")
        assert [row["template"] for row in rows] == ["custom"]
    finally:
        await connection.execute("DELETE FROM prompt_template WHERE template='custom'")
        alembic(alembic_env, "upgrade", "head")


async def test_seed_upgrade_tolerates_an_existing_row_with_the_same_id(
    connection, alembic_env, migrated_database
) -> None:
    try:
        alembic(alembic_env, "downgrade", "0002_erd_deviations")
        await connection.execute(
            "INSERT INTO prompt_template (id, template) VALUES ($1, 'manual')",
            uuid.UUID(SEED_ID),
        )
        alembic(alembic_env, "upgrade", "head")
        assert await connection.fetchval("SELECT count(*) FROM prompt_template") == 1
        template = await connection.fetchval("SELECT template FROM prompt_template")
        assert template == "manual"
    finally:
        await connection.execute("DELETE FROM prompt_template")
        alembic(alembic_env, "upgrade", "head")
