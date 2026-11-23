import asyncio
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.database import create_engine
from receipes.db.models import Nutrition, Recipe
from receipes.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
OWNER = {"X-User-Id": "42"}
OTHER = {"X-User-Id": "7"}
RECIPE_ID = UUID(int=1)
RECIPE_PATH = f"/recipes/{RECIPE_ID}/like"
LIST_PATH = "/recipes/liked"
PREVIOUS_REVISION = "0006_recipe_replacement"
LIKE_REVISION = "0007_recipe_like"


async def add_recipes(factory: Factory, *recipe_ids: UUID) -> None:
    async with factory() as session, session.begin():
        nutrition = Nutrition(
            calories=Decimal("345.5"),
            proteins=Decimal("10"),
            fats=Decimal("20"),
            carbohydrates=Decimal("30.25"),
        )
        session.add_all(
            Recipe(
                id=recipe_id,
                name=f"Блюдо {recipe_id.int}",
                calories=Decimal("350"),
                nutrition=nutrition,
                cooking_time=Decimal("25.5"),
            )
            for recipe_id in recipe_ids
        )


@pytest.fixture
async def client(
    settings: Settings, session_factory: Factory
) -> AsyncIterator[httpx.AsyncClient]:
    await add_recipes(session_factory, RECIPE_ID, UUID(int=2), UUID(int=3))
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        yield client


async def test_likes_are_isolated_by_user(client: httpx.AsyncClient) -> None:
    assert (await client.get(LIST_PATH, headers=OWNER)).json() == {"recipes": []}
    response = await client.post(RECIPE_PATH, headers=OWNER)
    assert (response.status_code, response.json()) == (200, {"success": True})
    assert (await client.get(LIST_PATH, headers=OTHER)).json() == {"recipes": []}
    assert (await client.delete(RECIPE_PATH, headers=OTHER)).status_code == 404
    assert (await client.post(RECIPE_PATH, headers=OTHER)).status_code == 200
    response = await client.delete(RECIPE_PATH, headers=OWNER)
    assert (response.status_code, response.json()) == (200, {"success": True})
    assert (await client.delete(RECIPE_PATH, headers=OWNER)).status_code == 404
    assert (await client.get(LIST_PATH, headers=OWNER)).json() == {"recipes": []}
    assert (await client.get(LIST_PATH, headers=OTHER)).json() == {
        "recipes": [
            {
                "recipeId": str(RECIPE_ID),
                "name": "Блюдо 1",
                "calories": 350,
                "nutrition": {
                    "calories": 345.5,
                    "proteins": 10,
                    "fats": 20,
                    "carbohydrates": 30.25,
                },
                "cookingTime": 25.5,
            }
        ]
    }


async def wait_for_pending_locks(
    connection: AsyncConnection | AsyncSession, mode: str, count: int
) -> None:
    query = text(
        "SELECT count(*) FROM pg_locks"
        " WHERE relation = 'recipe_like'::regclass AND mode = :mode AND NOT granted"
    )
    async with asyncio.timeout(3):
        while await connection.scalar(query, {"mode": mode}) != count:
            await asyncio.sleep(0.01)


async def test_duplicate_requests_overlap_and_one_returns_conflict(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    async with session_factory() as session:
        # Both HTTP transactions must attempt their insert before either can commit.
        await session.execute(text("LOCK TABLE recipe_like IN SHARE MODE"))
        requests = [
            asyncio.create_task(client.post(RECIPE_PATH, headers=OWNER))
            for _ in range(2)
        ]
        try:
            await wait_for_pending_locks(session, "RowExclusiveLock", 2)
        finally:
            await session.rollback()
            responses = await asyncio.gather(*requests)
    first, second = sorted(responses, key=lambda response: response.status_code)
    assert (first.status_code, first.json()) == (200, {"success": True})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert len((await client.get(LIST_PATH, headers=OWNER)).json()["recipes"]) == 1


@pytest.mark.parametrize("recipe_id", ["invalid", str(UUID(int=999))])
async def test_recipe_is_unknown(client: httpx.AsyncClient, recipe_id: str) -> None:
    for method in ("POST", "DELETE"):
        response = await client.request(
            method, f"/recipes/{recipe_id}/like", headers=OWNER
        )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND"
        expected = "Recipe not found" if method == "POST" else "Recipe like not found"
        assert error["message"] == expected


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-User-Id", "no")],
        [("X-User-Id", "0")],
        [("X-User-Id", "2147483648")],
        [("X-User-Id", "42"), ("X-User-Id", "42")],
    ],
)
async def test_every_operation_requires_one_valid_owner(
    client: httpx.AsyncClient, headers: list[tuple[str, str]]
) -> None:
    for method, path in (
        ("POST", RECIPE_PATH),
        ("DELETE", RECIPE_PATH),
        ("GET", LIST_PATH),
    ):
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_liked_recipes_have_stable_order(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    recipe_ids = [UUID(int=index) for index in range(1, 4)]
    async with session_factory() as session, session.begin():
        for recipe_id, date in zip(
            recipe_ids,
            [datetime(2026, 1, day, tzinfo=UTC) for day in (2, 1, 2)],
            strict=True,
        ):
            await session.execute(
                text(
                    "INSERT INTO recipe_like (user_id, recipe_id, created_at)"
                    " VALUES (42, :recipe, :date)"
                ),
                {"recipe": recipe_id, "date": date},
            )
    response = await client.get(LIST_PATH, headers=OWNER)
    assert response.status_code == 200
    assert [item["recipeId"] for item in response.json()["recipes"]] == [
        str(recipe_ids[index]) for index in (2, 0, 1)
    ]


async def test_likes_migration_preserves_recipes_and_refuses_lossy_downgrade(
    settings: Settings, alembic: Callable[..., str], empty_schema: None
) -> None:
    engine = create_engine(settings)
    try:
        alembic("upgrade", PREVIOUS_REVISION)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM alembic_version_receipes")
                )
                == PREVIOUS_REVISION
            )
        await add_recipes(async_sessionmaker(engine), RECIPE_ID)
        alembic("upgrade", "head")
        alembic("check")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT name FROM recipe WHERE id = :id"), {"id": RECIPE_ID}
                )
                == "Блюдо 1"
            )
            assert (
                await connection.scalar(
                    text(
                        "INSERT INTO recipe_like (user_id, recipe_id)"
                        " VALUES (42, :id) RETURNING created_at"
                    ),
                    {"id": RECIPE_ID},
                )
                is not None
            )
            downgrade = asyncio.create_task(
                asyncio.to_thread(alembic, "downgrade", PREVIOUS_REVISION)
            )
            try:
                await wait_for_pending_locks(connection, "AccessExclusiveLock", 1)
            finally:
                await connection.commit()
                with pytest.raises(
                    AssertionError, match="Cannot downgrade while recipe likes exist"
                ):
                    await downgrade
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM alembic_version_receipes")
                )
                == LIKE_REVISION
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM recipe_like")) == 1
            )
            await connection.execute(text("DELETE FROM recipe_like"))
        alembic("downgrade", PREVIOUS_REVISION)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT to_regclass('recipe_like')"))
                is None
            )
            assert (
                await connection.scalar(
                    text("SELECT name FROM recipe WHERE id = :id"), {"id": RECIPE_ID}
                )
                == "Блюдо 1"
            )
    finally:
        alembic("upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE recipe, nutrition CASCADE"))
        await engine.dispose()
