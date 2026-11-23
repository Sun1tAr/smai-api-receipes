import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.db.models import GenerationParameters
from receipes.db.repositories import TaskRepository
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def add_task(
    factory: Factory,
    user_id: int | None,
    *,
    habits: tuple[int, ...] = (),
    diet: int | None = None,
    status: TaskStatus = TaskStatus.DONE,
    created_at: datetime = NOW,
    task_id: UUID | None = None,
) -> UUID:
    params = GenerationParams(2, Decimal("100"), habits, diet)
    async with factory() as session, session.begin():
        created = await TaskRepository(session).create(params, None, user_id)
        await session.execute(
            text(
                "UPDATE generation_task SET status = :status, created_at ="
                " :created_at, task_id = coalesce(:new_id, task_id)"
                " WHERE task_id = :task_id"
            ),
            {
                "status": status.value,
                "created_at": created_at,
                "new_id": task_id,
                "task_id": created,
            },
        )
    return task_id or created


async def saved(factory: Factory, user_id: int) -> GenerationParameters | None:
    async with factory() as session:
        return await TaskRepository(session).find_last_done_parameters(user_id)


async def test_create_stores_owner_and_guest_stays_null(session_factory: Factory):
    guest = await add_task(session_factory, None)
    owner = await add_task(session_factory, 7)
    async with session_factory() as session:
        for task_id, expected in ((guest, None), (owner, 7)):
            task = await TaskRepository(session).get(task_id)
            assert task is not None and task.parameters.user_id == expected


async def test_nothing_saved_returns_none(session_factory: Factory):
    assert await saved(session_factory, 1) is None


async def test_users_do_not_see_each_other(session_factory: Factory):
    await add_task(session_factory, 1, habits=(1,))
    await add_task(session_factory, 2, habits=(2,))
    await add_task(session_factory, None, habits=(3,))
    first = await saved(session_factory, 1)
    second = await saved(session_factory, 2)
    assert first is not None and [h.habit_id for h in first.habits] == [1]
    assert second is not None and [h.habit_id for h in second.habits] == [2]
    assert await saved(session_factory, 3) is None


async def test_latest_done_by_created_at_wins(session_factory: Factory):
    await add_task(session_factory, 1, diet=1, created_at=NOW)
    await add_task(session_factory, 1, diet=2, created_at=NOW + timedelta(hours=1))
    row = await saved(session_factory, 1)
    assert row is not None and row.diet_id == 2


@pytest.mark.parametrize(
    "status", [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.FAILED]
)
async def test_newer_unfinished_task_does_not_count(session_factory: Factory, status):
    await add_task(session_factory, 1, diet=1, created_at=NOW)
    await add_task(
        session_factory, 1, diet=2, status=status, created_at=NOW + timedelta(hours=1)
    )
    row = await saved(session_factory, 1)
    assert row is not None and row.diet_id == 1


async def test_equal_created_at_is_decided_by_task_id(session_factory: Factory):
    low = UUID("00000000-0000-0000-0000-000000000001")
    high = UUID("00000000-0000-0000-0000-000000000002")
    await add_task(session_factory, 1, diet=1, task_id=high)
    await add_task(session_factory, 1, diet=2, task_id=low)
    row = await saved(session_factory, 1)
    assert row is not None and row.diet_id == 1


async def test_api_flow_and_empty_answer(settings: Settings, session_factory: Factory):
    from receipes.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            empty = await client.get("/profile/parameters", headers={"X-User-Id": "9"})
            assert empty.json() == {
                "habits": [],
                "diet": None,
                "allergies": [],
                "kitchenTools": [],
            }
            # Генерация без ID MS-2 не зовёт; в статус done задачу переводит воркер.
            created = await client.post(
                "/generate-menu",
                headers={"X-User-Id": "9"},
                json={
                    "peopleCount": 2,
                    "weeklyBudget": 100,
                    "habits": [],
                    "kitchenTools": [],
                },
            )
            assert created.status_code == 202
            still = await client.get("/profile/parameters", headers={"X-User-Id": "9"})
            assert still.json()["habits"] == []
            async with session_factory() as session, session.begin():
                await session.execute(
                    text("UPDATE generation_task SET status = 'done'")
                )
            await add_task(
                session_factory,
                9,
                habits=(2, 1),
                diet=3,
                created_at=datetime.now(UTC) + timedelta(hours=1),
            )
            done = await client.get("/profile/parameters", headers={"X-User-Id": "9"})
            assert done.json() == {
                "habits": [1, 2],
                "diet": 3,
                "allergies": [],
                "kitchenTools": [],
            }
            other = await client.get("/profile/parameters", headers={"X-User-Id": "8"})
            assert other.json()["habits"] == []


async def test_migration_adds_nullable_user_id_and_downgrade_removes_it(
    settings: Settings, alembic: Callable[..., str], schema: None
) -> None:
    from receipes.database import create_engine

    engine = create_engine(settings)
    try:
        alembic("downgrade", "0004_generation_parameters_v2")
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE generation_task, generation_parameters CASCADE")
            )
            await connection.execute(
                text(
                    "INSERT INTO generation_parameters"
                    " (id, people_count, weekly_budget)"
                    " VALUES ('00000000-0000-0000-0000-000000000001', 3, 500)"
                )
            )
        alembic("upgrade", "head")
        async with engine.connect() as connection:
            old = await connection.scalar(
                text("SELECT user_id FROM generation_parameters")
            )
            nullable = await connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns WHERE"
                    " table_name = 'generation_parameters' AND column_name = 'user_id'"
                )
            )
            indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes"
                        " WHERE tablename = 'generation_parameters'"
                    )
                )
            )
        assert old is None and nullable == "YES"
        assert "ix_generation_parameters_user_id" in indexes
        alembic("downgrade", "0004_generation_parameters_v2")
        async with engine.connect() as connection:
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'generation_parameters'"
                    )
                )
            )
            indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes"
                        " WHERE tablename = 'generation_parameters'"
                    )
                )
            )
        assert "user_id" not in columns
        assert "ix_generation_parameters_user_id" not in indexes
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
