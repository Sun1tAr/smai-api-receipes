import json
import os
import re
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from menu_samples import ollama_reply, sample_menu
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_generation_parameters import Ms2Stub
from test_likes_api import OWNER, add_recipes

from receipes.config import Settings
from receipes.database import create_engine
from receipes.db.models import PromptTemplate
from receipes.db.repositories import PromptTemplateRepository, TaskRepository
from receipes.domain.params import GenerationParams
from receipes.generation.prompt import render_prompt, validate_template
from receipes.main import create_app

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
        alembic("upgrade", "0006_recipe_replacement")
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


@pytest.mark.parametrize(
    ("user_id", "limit", "fallback", "expected"),
    [
        (42, 2, False, ["Блюдо 3", "Блюдо 2"]),
        (42, 1, True, ["Блюдо 3"]),
        (7, 10, False, ["Блюдо 4"]),
        (5, 10, False, []),
        (None, 10, False, []),
        (None, 10, True, []),
        (42, 0, False, []),
        (42, 0, True, []),
    ],
)
async def test_menu_likes_snapshot_and_worker_fallback(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int | None,
    limit: int,
    fallback: bool,
    expected: list[str],
) -> None:
    recipe_ids = [UUID(int=index) for index in range(1, 5)]
    await add_recipes(session_factory, *recipe_ids)
    async with session_factory() as session, session.begin():
        session.add(
            PromptTemplate(template="{people_count} {weekly_budget}\n\nТолько JSON")
        )
    prompts: list[str] = []

    def model(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json=ollama_reply(sample_menu()))

    app = create_app(
        settings.model_copy(update={"likes_in_prompt_limit": limit}),
        ollama_transport=httpx.MockTransport(model),
        ms2_transport=httpx.MockTransport(Ms2Stub()),
    )
    queries: list[str] = []

    @event.listens_for(app.state.engine.sync_engine, "before_cursor_execute")
    def record_likes_query(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "recipe_like" in statement:
            queries.append(statement)

    async def snapshot(task_id: UUID) -> str | None:
        async with session_factory() as session:
            return await TaskRepository(session).get_prompt(task_id)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for index, recipe_id in enumerate(recipe_ids):
            like = await client.post(
                f"/recipes/{recipe_id}/like",
                headers=OWNER if index < 3 else {"X-User-Id": "7"},
            )
            assert like.status_code == 200
        if fallback:
            async with session_factory() as session, session.begin():
                task_id = await TaskRepository(session).create(
                    GenerationParams(2, Decimal("100"), habits=(1,)), user_id=user_id
                )
            assert await snapshot(task_id) is None
        else:
            response = await client.post(
                "/generate-menu",
                json={
                    "peopleCount": 2,
                    "weeklyBudget": 100,
                    "habits": [1],
                    "kitchenTools": [],
                },
                headers={"X-User-Id": str(user_id)} if user_id is not None else {},
            )
            assert response.status_code == 202
            task_id = UUID(response.json()["task_id"])
            saved = await snapshot(task_id)
            assert saved is not None
            if user_id == 42 and limit > 0:
                removed = await client.delete(
                    f"/recipes/{recipe_ids[2]}/like", headers=OWNER
                )
                assert removed.status_code == 200

        assert await app.state.worker.process_one() is True
    [prompt] = prompts
    assert await snapshot(task_id) == prompt
    if not fallback:
        assert saved == prompt
    assert "привычки питания: «n1»" in prompt
    preference_lines = [line for line in prompt.splitlines() if line.startswith("[")]
    if expected:
        [data] = preference_lines
        assert json.loads(data) == expected
    else:
        assert not preference_lines
        assert "Предпочтения пользователя" not in prompt
    if user_id is None or limit == 0:
        assert not queries
    else:
        [query] = queries
        assert "LIMIT" in query
