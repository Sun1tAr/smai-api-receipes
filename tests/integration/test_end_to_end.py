import asyncio
import os

import httpx
import pytest
from menu_samples import ollama_reply, sample_menu
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.db.models import PromptTemplate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]


async def post_and_process(settings, factory, handler):
    from receipes.main import create_app

    async with factory() as session, session.begin():
        session.add(
            PromptTemplate(template="{people_count} чел., {weekly_budget} руб.")
        )
    app = create_app(settings, ollama_transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
            )
            task_id = created.json()["task_id"]
            before = (await client.get(f"/generate-menu/{task_id}")).json()
            assert await app.state.worker.process_one() is True
            after = await client.get(f"/generate-menu/{task_id}")
    return before, after


async def test_post_worker_get_full_cycle(
    settings: Settings, session_factory: Factory
) -> None:
    before, after = await post_and_process(
        settings,
        session_factory,
        lambda request: httpx.Response(200, json=ollama_reply(sample_menu())),
    )
    assert before == {"status": "pending", "menu": None, "error": None}
    body = after.json()
    assert after.status_code == 200
    assert (body["status"], body["error"]) == ("done", None)
    recipes = {recipe["name"]: recipe for recipe in body["menu"]["recipes"]}
    assert set(recipes) == {"Овсяная каша", "Куриный суп", "Запечённый лосось"}
    soup = recipes["Куриный суп"]
    assert soup["calories"] == 350 and soup["cookingTime"] == 25
    assert soup["nutrition"] == {
        "calories": 350,
        "proteins": 20.5,
        "fats": 10,
        "carbohydrates": 40,
    }
    assert {item["name"] for item in soup["ingredients"]} == {
        "Курица",
        "Картофель",
        "Вода",
    }
    assert soup["instructions"][1] == "Варите 20 минут"
    assert all(recipe["photo"] is None for recipe in recipes.values())


async def test_status_is_in_progress_while_ollama_is_called(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    seen: dict = {}
    holder: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await holder["client"].get(f"/generate-menu/{holder['task_id']}")
        seen["status"] = response.json()
        async with session_factory() as session:
            seen["prompt"] = await session.scalar(
                text("SELECT prompt FROM generation_task")
            )
        return httpx.Response(200, json=ollama_reply(sample_menu()))

    async with session_factory() as session, session.begin():
        session.add(
            PromptTemplate(template="{people_count} чел., {weekly_budget} руб.")
        )
    app = create_app(settings, ollama_transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            holder["client"] = client
            created = await client.post(
                "/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
            )
            holder["task_id"] = created.json()["task_id"]
            await app.state.worker.process_one()
    assert seen["status"] == {"status": "in_progress", "menu": None, "error": None}
    assert seen["prompt"] == "4 чел., 5000 руб."


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(503), "Neurocore unavailable"),
        (
            httpx.Response(200, json=ollama_reply("не json")),
            "Invalid neurocore response",
        ),
        (
            httpx.Response(200, json=ollama_reply("{", done_reason="length")),
            "Neurocore response truncated",
        ),
    ],
)
async def test_failure_is_visible_through_the_api(
    settings: Settings, session_factory: Factory, response: httpx.Response, message: str
) -> None:
    _, after = await post_and_process(settings, session_factory, lambda r: response)
    assert after.json() == {"status": "failed", "menu": None, "error": message}


async def test_worker_started_with_the_app_serves_a_posted_task(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    config = settings.model_copy(
        update={
            "generation_worker_enabled": True,
            "generation_worker_interval_seconds": 0.05,
        }
    )
    app = create_app(
        config,
        ollama_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=ollama_reply(sample_menu()))
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/generate-menu", json={"peopleCount": 2, "weeklyBudget": 900}
            )
            task_id = created.json()["task_id"]
            for _ in range(100):
                body = (await client.get(f"/generate-menu/{task_id}")).json()
                if body["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"task did not finish: {body}")
    assert len(body["menu"]["recipes"]) == 3
