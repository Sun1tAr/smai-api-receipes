import os
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from receipes.config import Settings
from receipes.database import create_engine
from receipes.db.models import PromptTemplate
from receipes.db.repositories import PromptTemplateRepository
from receipes.generation.prompt import render_prompt, validate_template

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def templates(settings: Settings) -> list[PromptTemplate]:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(select(PromptTemplate))
            return list(rows)
    finally:
        await engine.dispose()


async def test_seed_migration_adds_one_template_and_downgrade_removes_it(
    settings: Settings, alembic: Callable[..., None]
) -> None:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    rows = await templates(settings)
    assert len(rows) == 1
    assert rows[0].id == UUID("b42d2707-086d-4df0-96f2-37f77280e66b")
    validate_template(rows[0].template)
    alembic("downgrade", "0001_domain")
    assert await templates(settings) == []
    alembic("upgrade", "head")
    assert len(await templates(settings)) == 1


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
    assert active is not None
    prompt = render_prompt(active.template, 4, Decimal("5000"))
    assert "для 4 человек" in prompt and "5000 рублей" in prompt


async def test_seed_is_idempotent_when_the_row_already_exists(
    settings: Settings, alembic: Callable[..., None]
) -> None:
    from sqlalchemy import text

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
    assert len(rows) == 1 and rows[0].template == "manual"
