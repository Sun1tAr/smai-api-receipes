import os
import re
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from receipes.config import Settings
from receipes.database import create_engine
from receipes.db.repositories import PromptTemplateRepository
from receipes.generation.prompt import render_prompt, validate_template

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def templates(settings: Settings) -> dict[UUID, str]:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT id, template FROM prompt_template")
            )
            return {template_id: template for template_id, template in rows}
    finally:
        await engine.dispose()


async def test_seed_migrations_add_both_templates_and_downgrade_removes_them(
    settings: Settings, alembic: Callable[..., None]
) -> None:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    rows = await templates(settings)
    assert len(rows) == 2
    validate_template(rows[UUID("b42d2707-086d-4df0-96f2-37f77280e66b")])
    alembic("downgrade", "0001_domain")
    assert await templates(settings) == {}
    alembic("upgrade", "head")
    assert len(await templates(settings)) == 2


async def test_active_template_after_upgrade_is_the_seeded_one(
    settings: Settings, alembic: Callable[..., None]
) -> None:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    engine = create_engine(settings)
    try:
        async with async_sessionmaker(engine)() as session:
            active = await PromptTemplateRepository(session).get_active()
    finally:
        await engine.dispose()
    assert active is not None and active.kind == "menu"
    prompt = render_prompt(active.template, 4, Decimal("5000"))
    assert "для 4 человек" in prompt and "5000 рублей" in prompt


async def test_seed_is_idempotent_when_the_row_already_exists(
    settings: Settings, alembic: Callable[..., None]
) -> None:
    alembic("downgrade", "base")
    alembic("upgrade", "0001_domain")
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO prompt_template (id, template, created_at) VALUES "
                    "('b42d2707-086d-4df0-96f2-37f77280e66b', 'manual', now())"
                )
            )
        alembic("upgrade", "head")
        rows = await templates(settings)
    finally:
        await engine.dispose()
    assert len(rows) == 2
    assert rows[UUID("b42d2707-086d-4df0-96f2-37f77280e66b")] == "manual"


async def test_replacement_migration_preserves_v3_rows_and_refuses_lossy_downgrade(
    settings: Settings,
    alembic: Callable[..., str],
) -> None:
    previous = "0005_parameters_user_id"
    engine = create_engine(settings)
    try:
        alembic("downgrade", "base")
        alembic("upgrade", previous)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO generation_parameters"
                    " (id, people_count, weekly_budget)"
                    " VALUES ('00000000-0000-0000-0000-000000000001', 3, 500)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO generation_task"
                    " (task_id, status, parameters_id, created_at)"
                    " VALUES ('00000000-0000-0000-0000-000000000002', 'pending',"
                    " '00000000-0000-0000-0000-000000000001', now())"
                )
            )
        alembic("upgrade", "head")
        async with engine.begin() as connection:
            task = (
                await connection.execute(
                    text(
                        "SELECT kind, source_task_id, replaced_recipe_id,"
                        " result_recipe_id"
                        " FROM generation_task"
                    )
                )
            ).one()
            assert tuple(task) == ("menu", None, None, None)
            rows = (
                await connection.execute(
                    text("SELECT kind, template FROM prompt_template ORDER BY kind")
                )
            ).all()
            assert [row.kind for row in rows] == ["menu", "replace"]
            assert {
                "{people_count}",
                "{weekly_budget}",
                "{replaced_recipe}",
                "{existing_recipes}",
            } <= {part for part in re.findall(r"\{[^}]+\}", rows[1].template)}
            await connection.execute(
                text(
                    "INSERT INTO generation_task"
                    " (task_id, kind, status, parameters_id, created_at)"
                    " VALUES ('00000000-0000-0000-0000-000000000003',"
                    " 'replace', 'pending',"
                    " '00000000-0000-0000-0000-000000000001', now())"
                )
            )
        with pytest.raises(AssertionError, match="Cannot downgrade"):
            alembic("downgrade", previous)
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM alembic_version_receipes")
                )
                == "0006_recipe_replacement"
            )
            deleted = await connection.execute(
                text("DELETE FROM generation_task WHERE kind = 'replace'")
            )
            assert deleted.rowcount == 1
        alembic("downgrade", previous)
        assert len(await templates(settings)) == 1
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT status FROM generation_task"))
                == "pending"
            )
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
