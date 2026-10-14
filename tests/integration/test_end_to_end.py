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
            # False при сбое стороны Ollama: следующая итерация начнётся с паузы.
            await app.state.worker.process_one()
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


async def create_tasks(client: httpx.AsyncClient, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        created = await client.post(
            "/generate-menu", json={"peopleCount": 2, "weeklyBudget": 700}
        )
        ids.append(created.json()["task_id"])
    return ids


async def statuses(client: httpx.AsyncClient, ids: list[str]) -> list[str]:
    return [
        (await client.get(f"/generate-menu/{item}")).json()["status"] for item in ids
    ]


async def test_ollama_503_does_not_burn_the_rest_of_the_queue(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    app = create_app(
        settings, ollama_transport=httpx.MockTransport(lambda r: httpx.Response(503))
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ids = await create_tasks(client, 5)
            for _ in range(5):
                await app.state.worker.process_one()
            assert await statuses(client, ids) == ["failed"] + ["pending"] * 4
            failed = (await client.get(f"/generate-menu/{ids[0]}")).json()
    assert failed["error"] == "Neurocore unavailable"


async def test_broken_template_keeps_every_task_pending_through_the_api(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="шаблон без плейсхолдеров"))
    app = create_app(
        settings,
        ollama_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=ollama_reply(sample_menu()))
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ids = await create_tasks(client, 3)
            for _ in range(3):
                assert await app.state.worker.process_one() is False
            assert await statuses(client, ids) == ["pending"] * 3
            # Шаблон исправлен новой записью: очередь разбирается без потерь.
            async with session_factory() as session, session.begin():
                session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
            for _ in range(3):
                assert await app.state.worker.process_one() is True
            assert await statuses(client, ids) == ["done"] * 3


async def test_model_not_loaded_keeps_the_queue_until_it_appears(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    loaded = {"models": ["other:1b"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": name} for name in loaded["models"]]}
            )
        if "qwen2.5:7b" in loaded["models"]:
            return httpx.Response(200, json=ollama_reply(sample_menu()))
        return httpx.Response(404, json={"error": "model not found"})

    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    app = create_app(settings, ollama_transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ids = await create_tasks(client, 4)
            for _ in range(4):
                await app.state.worker.process_one()
            first = (await client.get(f"/generate-menu/{ids[0]}")).json()
            assert first["error"] == "Neurocore model not found"
            assert await statuses(client, ids) == ["failed"] + ["pending"] * 3
            # /api/tags отвечает 200, но модели в списке всё ещё нет.
            assert await app.state.worker.process_one() is False
            loaded["models"].append("qwen2.5:7b")
            for _ in range(3):
                assert await app.state.worker.process_one() is True
            assert await statuses(client, ids) == ["failed"] + ["done"] * 3


async def test_ollama_500_loses_only_the_failing_task(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    handler_state = {"healthy": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if handler_state["healthy"]:
            return httpx.Response(200, json=ollama_reply(sample_menu()))
        return httpx.Response(500, text="boom")

    app = create_app(settings, ollama_transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ids = await create_tasks(client, 4)
            assert await app.state.worker.process_one() is False
            handler_state["healthy"] = True
            for _ in range(3):
                assert await app.state.worker.process_one() is True
            body = (await client.get(f"/generate-menu/{ids[0]}")).json()
            assert (body["status"], body["error"]) == ("failed", "Neurocore error")
            assert await statuses(client, ids) == ["failed"] + ["done"] * 3
