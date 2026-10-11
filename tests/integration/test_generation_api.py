import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from receipes.config import Settings
from receipes.repositories import TaskRepository
from tests.integration.seed import make_task, menu_data, start

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


@pytest.fixture
async def client(
    settings: Settings, session_factory
) -> AsyncIterator[httpx.AsyncClient]:
    from receipes.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http:
            yield http


async def rows(session_factory, sql: str, **params: Any):
    async with session_factory() as session:
        return (await session.execute(text(sql), params)).all()


async def test_post_creates_a_pending_task_and_returns_202(
    client, session_factory
) -> None:
    response = await client.post(
        "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000.5}
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"task_id"}
    task_id = uuid.UUID(body["task_id"])
    assert isinstance(body["task_id"], str)
    stored = await rows(
        session_factory,
        "SELECT t.status, t.prompt, p.people_count, p.weekly_budget "
        "FROM generation_task t JOIN generation_parameters p "
        "ON p.id = t.parameters_id WHERE t.task_id = :id",
        id=task_id,
    )
    assert [tuple(row) for row in stored] == [("pending", None, 4, Decimal("5000.50"))]
    status = await client.get(f"/generate-menu/{task_id}")
    assert status.json() == {"status": "pending", "menu": None, "error": None}


@pytest.mark.parametrize("budget", [0.01, 1000000, 1e6, 12])
async def test_boundary_budgets_are_accepted(client, budget) -> None:
    response = await client.post(
        "/generate-menu", json={"peopleCount": 1, "weeklyBudget": budget}
    )
    assert response.status_code == 202


@pytest.mark.parametrize("people", [1, 20])
async def test_boundary_people_counts_are_accepted(client, people) -> None:
    response = await client.post(
        "/generate-menu", json={"peopleCount": people, "weeklyBudget": 100}
    )
    assert response.status_code == 202


async def test_invalid_post_creates_nothing(client, session_factory) -> None:
    response = await client.post(
        "/generate-menu", json={"peopleCount": 21, "weeklyBudget": 100}
    )
    assert response.status_code == 400
    assert await rows(session_factory, "SELECT 1 FROM generation_task") == []


async def test_get_reports_pending_and_in_progress(client, session_factory) -> None:
    async with session_factory() as session:
        pending = await make_task(session)
        running = await make_task(session)
        await start(session, running.task_id)
        await session.commit()
    expected = {
        pending.task_id: {"status": "pending", "menu": None, "error": None},
        running.task_id: {"status": "in_progress", "menu": None, "error": None},
    }
    for task_id, body in expected.items():
        response = await client.get(f"/generate-menu/{task_id}")
        assert response.status_code == 200
        assert response.json() == body


async def test_expired_task_is_reported_as_failed(client, session_factory) -> None:
    async with session_factory() as session:
        task = await make_task(session, timedelta(hours=1))
        assert await TaskRepository(session).expire_stale(
            timedelta(minutes=15), timedelta(minutes=10)
        )
        await session.commit()
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.json() == {
        "status": "failed",
        "menu": None,
        "error": "Queue timeout",
    }


async def test_done_task_returns_the_menu_with_json_numbers(
    client, session_factory
) -> None:
    generated = menu_data(
        ["Каша", "Суп", "Салат"],
        Каша=[
            ("овсяные хлопья", 200, "г"),
            ("ОВСЯНЫЕ ЯГОДЫ", 1.5, "ст. л."),
            ("Мёд", 3, "г"),
        ],
    )
    async with session_factory() as session:
        task = await make_task(session)
        await start(session, task.task_id)
        assert await TaskRepository(session).complete(task.task_id, generated)
        await session.commit()
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["error"]) == ("done", None)
    recipes = body["menu"]["recipes"]
    assert [recipe["name"] for recipe in recipes] == ["Каша", "Суп", "Салат"]
    first = recipes[0]
    assert set(first) == {
        "name",
        "ingredients",
        "calories",
        "nutrition",
        "cookingTime",
        "instructions",
        "photo",
    }
    assert first["photo"] is None
    # Имена показываются с заглавной буквы; регистр остального сохраняется.
    assert [i["name"] for i in first["ingredients"]] == [
        "Овсяные хлопья",
        "ОВСЯНЫЕ ЯГОДЫ",
        "Мёд",
    ]
    assert [i["quantity"] for i in first["ingredients"]] == [200, 1.5, 3]
    numbers = [
        first["calories"],
        first["cookingTime"],
        *first["nutrition"].values(),
        *[i["quantity"] for i in first["ingredients"]],
    ]
    assert all(type(value) in (int, float) for value in numbers)
    assert first["nutrition"] == {
        "calories": 320,
        "proteins": 12,
        "fats": 8,
        "carbohydrates": 50,
    }
    assert first["instructions"] == ["Первый шаг.", "Второй шаг.", "Третий шаг."]
    # В БД имя ингредиента не менялось.
    stored = await rows(
        session_factory, "SELECT name FROM ingredient WHERE name_key = 'овсяные хлопья'"
    )
    assert [row[0] for row in stored] == ["овсяные хлопья"]


async def test_failed_task_returns_the_error_string(client, session_factory) -> None:
    async with session_factory() as session:
        task = await make_task(session)
        await start(session, task.task_id)
        await session.execute(
            text(
                "UPDATE generation_task SET status = 'failed', error = 'Neurocore "
                "timeout', finished_at = now() WHERE task_id = :id"
            ),
            {"id": task.task_id},
        )
        await session.commit()
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.json() == {
        "status": "failed",
        "menu": None,
        "error": "Neurocore timeout",
    }


async def test_unknown_task_is_404(client) -> None:
    response = await client.get(f"/generate-menu/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Generation task not found",
        "details": {},
    }
    assert (await client.get("/generate-menu/not-a-uuid")).status_code == 404


async def test_database_failure_is_a_500_with_request_id(client, monkeypatch) -> None:
    async def broken(self, parameters):
        raise RuntimeError("SELECT secret FROM /var/lib/db")

    monkeypatch.setattr(TaskRepository, "create", broken)
    response = await client.post(
        "/generate-menu",
        json={"peopleCount": 2, "weeklyBudget": 100},
        headers={"X-Request-ID": "rid-api-500"},
    )
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "rid-api-500"
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
    }
    assert "secret" not in response.text


async def test_metrics_count_created_tasks_without_task_labels(client) -> None:
    await client.post("/generate-menu", json={"peopleCount": 2, "weeklyBudget": 100})
    task_id = (
        await client.post("/generate-menu", json={"peopleCount": 2, "weeklyBudget": 1})
    ).json()["task_id"]
    await client.get(f"/generate-menu/{task_id}")
    metrics = (await client.get("/metrics")).text
    assert "generation_tasks_created_total 2.0" in metrics
    assert 'handler="/generate-menu/{task_id}"' in metrics
    assert task_id not in metrics


async def test_openapi_and_docs_are_served(client) -> None:
    document = (await client.get("/openapi.json")).json()
    assert "/generate-menu" in document["paths"]
    assert (await client.get("/docs")).status_code == 200
