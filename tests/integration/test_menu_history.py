import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_likes_api import OTHER, OWNER

from receipes.config import Settings
from receipes.db.models import (
    GenerationParameters,
    GenerationTask,
    Menu,
    MenuRecipe,
    Nutrition,
    Recipe,
)
from receipes.domain.status import TaskKind, TaskStatus
from receipes.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
PATH = "/profile/menus"
EARLY = datetime(2026, 10, 28, 6, tzinfo=UTC)
LATE = datetime(2026, 11, 1, 12, tzinfo=UTC)


@pytest.fixture
async def client(
    settings: Settings, session_factory: Factory
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-User-Id", "invalid")],
        [("X-User-Id", "0")],
        [("X-User-Id", "2147483648")],
        [("X-User-Id", "42"), ("X-User-Id", "42")],
    ],
)
async def test_history_requires_one_valid_owner(
    client: httpx.AsyncClient, headers: list[tuple[str, str]]
) -> None:
    response = await client.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "User is not identified",
            "details": {},
        }
    }


async def test_history_is_empty_without_completed_menus(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(PATH, headers=OWNER)
    assert (response.status_code, response.json()) == (200, {"menus": []})


async def test_history_contains_only_owned_completed_menus_in_creation_order(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    async with session_factory() as session, session.begin():
        nutrition = Nutrition(calories=100, proteins=10, fats=5, carbohydrates=20)
        recipes = [
            Recipe(
                id=UUID(int=100),
                name="Салат",
                calories=Decimal("200"),
                cooking_time=Decimal("10"),
                nutrition=nutrition,
            ),
            Recipe(
                id=UUID(int=101),
                name="Суп",
                calories=Decimal("321.25"),
                cooking_time=Decimal("25.5"),
                nutrition=nutrition,
            ),
        ]
        session.add_all(recipes)
        await session.flush()
        menus = {
            menu_id: Menu(
                id=UUID(int=menu_id),
                menu_recipes=[MenuRecipe(recipe_id=recipe.id) for recipe in recipes]
                if menu_id != 3
                else [],
            )
            for menu_id in range(1, 9)
        }
        for task, menu, owner, created_at, status, kind in (
            (30, 1, 42, EARLY, TaskStatus.DONE, TaskKind.MENU),
            (20, 2, 42, LATE, TaskStatus.DONE, TaskKind.MENU),
            (10, 3, 42, LATE, TaskStatus.DONE, TaskKind.MENU),
            (40, 4, 7, LATE, TaskStatus.DONE, TaskKind.MENU),
            (41, 5, None, LATE, TaskStatus.DONE, TaskKind.MENU),
            (42, 6, 42, LATE, TaskStatus.PENDING, TaskKind.MENU),
            (43, 7, 42, LATE, TaskStatus.IN_PROGRESS, TaskKind.MENU),
            (44, 8, 42, LATE, TaskStatus.FAILED, TaskKind.MENU),
            (45, 2, 42, LATE, TaskStatus.DONE, TaskKind.REPLACE),
            (46, None, 42, LATE, TaskStatus.DONE, TaskKind.MENU),
        ):
            session.add(
                GenerationTask(
                    task_id=UUID(int=task),
                    kind=kind,
                    status=status,
                    parameters=GenerationParameters(
                        people_count=2, weekly_budget=1000, user_id=owner
                    ),
                    menu=menus.get(menu),
                    created_at=created_at,
                    status_changed_at=created_at
                    + timedelta(days=7 if task == 30 else 1),
                )
            )
    response = await client.get(PATH, headers=OWNER)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"menus"}
    assert [item["menuId"] for item in body["menus"]] == [
        str(UUID(int=2)),
        str(UUID(int=3)),
        str(UUID(int=1)),
    ]
    cards = [
        {
            "recipeId": str(UUID(int=100)),
            "name": "Салат",
            "calories": 200,
            "cookingTime": 10,
        },
        {
            "recipeId": str(UUID(int=101)),
            "name": "Суп",
            "calories": 321.25,
            "cookingTime": 25.5,
        },
    ]
    for item, expected_time, expected_cards in zip(
        body["menus"], (LATE, LATE, EARLY), (cards, [], cards), strict=True
    ):
        assert set(item) == {"menuId", "createdAt", "recipes"}
        created_at = datetime.fromisoformat(item["createdAt"])
        assert created_at.utcoffset() == timedelta(0)
        assert created_at == expected_time
        actual_cards = sorted(item["recipes"], key=lambda card: card["recipeId"])
        assert actual_cards == expected_cards
        if actual_cards:
            integer, fractional = actual_cards
            for field in ("calories", "cookingTime"):
                assert type(integer[field]) is int
                assert type(fractional[field]) is float

    response = await client.get(PATH, headers=OTHER)
    assert response.status_code == 200
    assert [item["menuId"] for item in response.json()["menus"]] == [str(UUID(int=4))]
