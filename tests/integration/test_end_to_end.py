"""Сквозные сценарии через HTTP: приложение с воркером и замоканной Ollama.

Моменты синхронизации — события и очередь, а не паузы: заглушка Ollama сама сообщает,
что вызов начался, и ждёт разрешения; воркер сообщает, что обработал задачу.
"""

import asyncio
import os
from typing import Any

import httpx
import pytest
from sqlalchemy import text

import receipes.main as main
from receipes.config import Settings
from receipes.ollama import OllamaClient
from receipes.repositories import MenuRepository
from receipes.worker import GenerationWorker
from tests.integration.test_generation_worker import (
    add_template,
    garbage,
    valid,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

WAIT = 15  # верхняя граница ожидания события, секунд (не пауза в сценарии)


class ScriptedOllama:
    """Заглушка Ollama: первый вызов можно придержать, ответы идут по сценарию."""

    def __init__(self, *script: httpx.Response | Exception, hold_first: bool = False):
        self.script = list(script)
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_first = hold_first
        self.prompts: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.prompts.append(json.loads(request.content)["prompt"])
        self.calls += 1
        if self.hold_first and self.calls == 1:
            self.entered.set()
            await self.release.wait()
        outcome = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Service:
    def __init__(self, client: httpx.AsyncClient, processed: asyncio.Queue[None]):
        self.client = client
        self.processed = processed

    async def create(self, people: int = 4, budget: float = 5000) -> str:
        response = await self.client.post(
            "/generate-menu", json={"peopleCount": people, "weeklyBudget": budget}
        )
        assert response.status_code == 202
        return str(response.json()["task_id"])

    async def status(self, task_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/generate-menu/{task_id}")
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    async def next_processed(self) -> None:
        await asyncio.wait_for(self.processed.get(), WAIT)


@pytest.fixture
async def run_service(settings: Settings, session_factory, monkeypatch):
    processed: asyncio.Queue[None] = asyncio.Queue()
    original = GenerationWorker.process

    async def process(self: GenerationWorker, task: Any) -> None:
        try:
            await original(self, task)
        finally:
            processed.put_nowait(None)

    monkeypatch.setattr(GenerationWorker, "process", process)
    running: list[tuple[Any, httpx.AsyncClient]] = []

    async def start(ollama: ScriptedOllama) -> Service:
        await add_template(session_factory)
        transport = httpx.MockTransport(ollama)
        monkeypatch.setattr(
            main, "OllamaClient", lambda s: OllamaClient(s, transport=transport)
        )
        app = main.create_app(
            settings.model_copy(
                update={
                    "generation_worker_enabled": True,
                    "generation_worker_interval_seconds": 0.05,
                }
            )
        )
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        running.append((context, http))
        return Service(http, processed)

    yield start
    for context, http in reversed(running):
        await http.aclose()
        await context.__aexit__(None, None, None)


async def rows(session_factory, sql: str, **params: Any):
    async with session_factory() as session:
        return (await session.execute(text(sql), params)).all()


async def test_a_task_goes_pending_in_progress_done_through_http(
    run_service, session_factory
) -> None:
    ollama = ScriptedOllama(valid(), hold_first=True)
    service = await run_service(ollama)
    first = await service.create(people=4, budget=5000)
    await asyncio.wait_for(ollama.entered.wait(), WAIT)
    # Воркер взял первую задачу и ждёт Ollama: она in_progress, вторая — в очереди.
    assert await service.status(first) == {
        "status": "in_progress",
        "menu": None,
        "error": None,
    }
    second = await service.create(people=2, budget=1500.5)
    assert await service.status(second) == {
        "status": "pending",
        "menu": None,
        "error": None,
    }
    ollama.release.set()
    await service.next_processed()
    done = await service.status(first)
    assert done["status"] == "done" and done["error"] is None
    assert len(done["menu"]["recipes"]) == 3
    await service.next_processed()
    assert (await service.status(second))["status"] == "done"
    # Промпт сохранён в задаче и содержит параметры именно этой задачи.
    stored = {
        row[0]: row[1]
        for row in await rows(
            session_factory, "SELECT task_id::text, prompt FROM generation_task"
        )
    }
    assert "Людей 4, бюджет 5000.00." in stored[first]
    assert "Людей 2, бюджет 1500.50." in stored[second]
    assert ollama.prompts == [stored[first], stored[second]]


FAILURES = [
    ("service unavailable", (httpx.Response(503),), "Ollama returned an error"),
    ("invalid json twice", (garbage(), garbage()), "Model returned an invalid menu"),
    ("timeout", (httpx.ReadTimeout("slow"),), "Ollama request timed out"),
    ("connection refused", (httpx.ConnectError("no"),), "Ollama is unavailable"),
    ("exception", (RuntimeError("boom"),), "Menu generation failed"),
    (
        "truncated",
        (httpx.Response(200, json={"response": "{", "done_reason": "length"}),),
        "Model response was cut off (token limit reached)",
    ),
]


@pytest.mark.parametrize(
    ("script", "message"), [pytest.param(s, m, id=i) for i, s, m in FAILURES]
)
async def test_ollama_failures_end_as_failed_through_http(
    run_service, session_factory, script, message
) -> None:
    ollama = ScriptedOllama(*script)
    service = await run_service(ollama)
    task_id = await service.create()
    await service.next_processed()
    body = await service.status(task_id)
    assert body == {"status": "failed", "menu": None, "error": message}
    assert await rows(session_factory, "SELECT 1 FROM menu") == []


async def test_an_invalid_first_answer_is_retried_and_ends_done(
    run_service,
) -> None:
    ollama = ScriptedOllama(garbage(), valid())
    service = await run_service(ollama)
    task_id = await service.create()
    await service.next_processed()
    assert (await service.status(task_id))["status"] == "done"
    assert ollama.calls == 2


async def test_a_failure_while_saving_the_menu_leaves_no_half_menu(
    run_service, session_factory, monkeypatch
) -> None:
    original = MenuRepository.save

    async def save_then_fail(self: MenuRepository, generated: Any) -> Any:
        await original(self, generated)  # меню уже записано в транзакции
        raise RuntimeError("storage failed after the menu was written")

    monkeypatch.setattr(MenuRepository, "save", save_then_fail)
    ollama = ScriptedOllama(valid())
    service = await run_service(ollama)
    task_id = await service.create()
    await service.next_processed()
    body = await service.status(task_id)
    assert body == {
        "status": "failed",
        "menu": None,
        "error": "Menu generation failed",
    }
    for table in (
        "menu",
        "menu_recipes",
        "recipe",
        "nutrition",
        "instruction",
        "recipe_ingredients",
        "ingredient",
    ):
        assert await rows(session_factory, f"SELECT 1 FROM {table}") == [], table
