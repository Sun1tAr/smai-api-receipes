import os
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.db.models import GenerationParameters, GenerationTask, PromptTemplate
from receipes.db.repositories import MenuRepository, TaskRepository
from receipes.domain.menu import (
    IngredientPayload,
    MenuPayload,
    NutritionPayload,
    RecipePayload,
)
from receipes.domain.status import TaskStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def api(
    settings: Settings, session_factory: Factory
) -> AsyncIterator[httpx.AsyncClient]:
    from receipes.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


def stored_recipe(name: str, *ingredients: str) -> RecipePayload:
    return RecipePayload(
        name=name,
        ingredients=[
            IngredientPayload(name=item, quantity=Decimal("200"), unit="г")
            for item in ingredients
        ],
        calories=Decimal("350"),
        nutrition=NutritionPayload(
            calories=Decimal("350"),
            proteins=Decimal("20.5"),
            fats=Decimal("10"),
            carbohydrates=Decimal("40"),
        ),
        cookingTime=Decimal("25"),
        instructions=[f"Подготовьте продукты для блюда {name}", "Варите 20 минут"],
    )


def stored_menu() -> MenuPayload:
    return MenuPayload(
        recipes=[
            stored_recipe("Овсяная каша", "Овсяные хлопья", "Молоко"),
            stored_recipe("Куриный суп", "Курица", "Картофель", "Вода"),
            stored_recipe("Запечённый лосось", "Лосось", "Лимон"),
        ]
    )


async def test_post_creates_pending_task_with_parameters_in_the_database(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            PromptTemplate(template="{people_count} чел., {weekly_budget} руб.")
        )
    response = await api.post(
        "/generate-menu", json={"peopleCount": 3, "weeklyBudget": 1234.5}
    )
    assert response.status_code == 202
    task_id = UUID(response.json()["task_id"])
    async with session_factory() as session:
        task = await session.get(GenerationTask, task_id)
        assert task is not None
        assert (task.status, task.menu_id, task.prompt, task.error) == (
            TaskStatus.PENDING,
            None,
            "3 чел., 1234.5 руб.",
            None,
        )
        parameters = await session.get(GenerationParameters, task.parameters_id)
    assert parameters is not None
    assert parameters.people_count == 3
    assert parameters.weekly_budget == Decimal("1234.50")


async def test_v2_fields_are_not_stored(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu",
        json={"peopleCount": 2, "weeklyBudget": 100, "habits": [1], "diet": 2},
    )
    assert response.status_code == 202
    async with session_factory() as session:
        columns = set(GenerationParameters.__table__.columns.keys())
        rows = (await session.scalars(select(GenerationParameters))).all()
    assert columns == {"id", "people_count", "weekly_budget"} and len(rows) == 1


async def test_get_pending_after_post(api: httpx.AsyncClient) -> None:
    created = await api.post(
        "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
    )
    response = await api.get(f"/generate-menu/{created.json()['task_id']}")
    assert response.status_code == 200
    assert response.json() == {"status": "pending", "menu": None, "error": None}


async def test_get_unknown_task_is_404(api: httpx.AsyncClient) -> None:
    for task_id in (str(uuid4()), "not-a-uuid"):
        response = await api.get(f"/generate-menu/{task_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_get_done_task_returns_the_saved_menu(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    created = await api.post(
        "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
    )
    task_id = UUID(created.json()["task_id"])
    async with session_factory() as session, session.begin():
        claim = await TaskRepository(session).claim_next()
        assert claim is not None and claim.task_id == task_id
        menu_id = await MenuRepository(session).save(stored_menu())
        assert await TaskRepository(session).mark_done(task_id, menu_id) is not None
    response = await api.get(f"/generate-menu/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["error"]) == ("done", None)
    recipes = {recipe["name"]: recipe for recipe in body["menu"]["recipes"]}
    assert set(recipes) == {"Овсяная каша", "Куриный суп", "Запечённый лосось"}
    soup = recipes["Куриный суп"]
    assert {item["name"] for item in soup["ingredients"]} == {
        "Курица",
        "Картофель",
        "Вода",
    }
    assert soup["instructions"] == [
        "Подготовьте продукты для блюда Куриный суп",
        "Варите 20 минут",
    ]
    assert soup["nutrition"]["proteins"] == 20.5
    assert soup["photo"] is None
    assert '"quantity":200,' in response.text


async def test_get_failed_task_returns_error(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    created = await api.post(
        "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
    )
    task_id = UUID(created.json()["task_id"])
    async with session_factory() as session, session.begin():
        await TaskRepository(session).claim_next()
        await TaskRepository(session).mark_failed(task_id, "Neurocore timeout")
    response = await api.get(f"/generate-menu/{task_id}")
    assert response.json() == {
        "status": "failed",
        "menu": None,
        "error": "Neurocore timeout",
    }
