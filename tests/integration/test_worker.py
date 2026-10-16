import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from menu_samples import ollama_reply, sample_menu
from opentelemetry.trace import NoOpTracer
from prometheus_client import CollectorRegistry
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.api.dependencies import SqlTaskStore
from receipes.config import Settings
from receipes.db.models import (
    GenerationTask,
    Ingredient,
    Menu,
    MenuRecipe,
    PromptTemplate,
    Recipe,
)
from receipes.db.repositories import TaskRepository
from receipes.domain.status import TaskStatus
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.ollama import OllamaClient
from receipes.generation.worker import GenerationWorker
from receipes.observability import configure_logging

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
TEMPLATE = "Меню для {people_count} человек, бюджет {weekly_budget} рублей."


class Rig:
    def __init__(
        self,
        factory: Factory,
        settings: Settings,
        handler: Handler,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.factory = factory
        self.requests: list[dict[str, Any]] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            if request.content:
                self.requests.append(json.loads(request.content))
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        self.registry = CollectorRegistry()
        metrics = GenerationMetrics(self.registry)
        http = OllamaClient.build_http_client(settings, httpx.MockTransport(recording))
        client = OllamaClient(settings, http, metrics, NoOpTracer())
        self.worker = GenerationWorker(
            factory, factory.kw["bind"], client, settings, metrics, sleep=sleep
        )

    def sample(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    async def task(self, task_id: UUID) -> GenerationTask:
        async with self.factory() as session:
            task = await TaskRepository(session).get(task_id)
        assert task is not None
        return task

    async def count(self, model: type) -> int:
        async with self.factory() as session:
            return await session.scalar(select(func.count()).select_from(model))


def good(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=ollama_reply(sample_menu()))


async def add_template(factory: Factory, text: str = TEMPLATE) -> None:
    async with factory() as session, session.begin():
        session.add(PromptTemplate(template=text))


async def add_task(factory: Factory, people: int = 4, budget: str = "5000.50") -> UUID:
    async with factory() as session, session.begin():
        return await TaskRepository(session).create(people, Decimal(budget))


@pytest.fixture
async def ready(session_factory: Factory) -> Factory:
    await add_template(session_factory)
    return session_factory


def rig(
    factory: Factory,
    settings: Settings,
    handler: Handler = good,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **extra: Any,
):
    return Rig(factory, settings.model_copy(update=extra), handler, sleep)


async def test_no_pending_task_means_nothing_to_do(
    ready: Factory, settings: Settings
) -> None:
    assert await rig(ready, settings).worker.process_one() is False


async def test_successful_cycle_saves_menu_and_marks_done(
    ready: Factory, settings: Settings
) -> None:
    task_id = await add_task(ready, 4, "5000.50")
    harness = rig(ready, settings)
    assert await harness.worker.process_one() is True
    task = await harness.task(task_id)
    assert (task.status, task.error) == (TaskStatus.DONE, None)
    assert task.menu is not None and task.menu_id == task.menu.id
    names = {link.recipe.name for link in task.menu.menu_recipes}
    assert names == {"Овсяная каша", "Куриный суп", "Запечённый лосось"}
    assert harness.sample("menu_generation_tasks_total", status="done") == 1
    assert harness.sample("menu_generation_tasks_total", status="failed") == 0


async def test_prompt_is_rendered_from_the_active_template_and_sent_once(
    ready: Factory, settings: Settings
) -> None:
    task_id = await add_task(ready, 4, "5000.50")
    harness = rig(ready, settings)
    await harness.worker.process_one()
    task = await harness.task(task_id)
    expected = "Меню для 4 человек, бюджет 5000.5 рублей."
    assert task.prompt == expected
    assert len(harness.requests) == 1
    assert harness.requests[0]["prompt"] == expected


async def test_newest_template_is_used(
    session_factory: Factory, settings: Settings
) -> None:
    await add_template(session_factory, "старый {people_count} {weekly_budget}")
    await add_template(session_factory, "новый {people_count} {weekly_budget}")
    task_id = await add_task(session_factory, 2, "100")
    harness = rig(session_factory, settings)
    await harness.worker.process_one()
    assert (await harness.task(task_id)).prompt == "новый 2 100"


async def test_tasks_are_processed_oldest_first_one_at_a_time(
    ready: Factory, settings: Settings
) -> None:
    first = await add_task(ready)
    second = await add_task(ready)
    harness = rig(ready, settings)
    await harness.worker.process_one()
    assert (await harness.task(first)).status == TaskStatus.DONE
    assert (await harness.task(second)).status == TaskStatus.PENDING


async def test_no_transaction_is_open_during_the_ollama_call(
    ready: Factory, settings: Settings
) -> None:
    task_id = await add_task(ready)
    idle: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        async with ready() as session:
            idle.append(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE state LIKE 'idle in transaction%' "
                        "AND pid <> pg_backend_pid() "
                        "AND datname = current_database()"
                    )
                )
            )
        return good(request)

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    assert (await harness.task(task_id)).status == TaskStatus.DONE
    assert idle == [0]


@pytest.mark.parametrize(
    ("handler_result", "message"),
    [
        (httpx.Response(503, text="down"), "Neurocore unavailable"),
        (httpx.Response(404, text="no model"), "Neurocore model not found"),
        (httpx.Response(500, text="boom"), "Neurocore error"),
        (httpx.Response(400, text="bad"), "Neurocore error"),
    ],
)
async def test_ollama_http_errors_fail_the_task_without_retry(
    ready: Factory,
    settings: Settings,
    handler_result: httpx.Response,
    message: str,
) -> None:
    task_id = await add_task(ready)
    harness = rig(ready, settings, lambda request: handler_result)
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert (task.status, task.error, task.menu_id) == (TaskStatus.FAILED, message, None)
    assert len(harness.requests) == 1
    assert harness.sample("menu_generation_tasks_total", status="failed") == 1
    assert await harness.count(Menu) == 0


async def test_connection_error_fails_with_unavailable(
    ready: Factory, settings: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    task_id = await add_task(ready)
    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    assert (await harness.task(task_id)).error == "Neurocore unavailable"


async def test_timeout_fails_with_timeout_and_is_not_retried(
    ready: Factory, settings: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    task_id = await add_task(ready)
    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert (task.status, task.error) == (TaskStatus.FAILED, "Neurocore timeout")
    assert len(harness.requests) == 1


async def test_truncated_answer_fails_without_retry_and_logs_token_counts(
    ready: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    reply = ollama_reply(
        '{"recipes": [{"na',
        done_reason="length",
        prompt_eval_count=810,
        eval_count=4096,
    )
    task_id = await add_task(ready)
    harness = rig(ready, settings, lambda r: httpx.Response(200, json=reply))
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert task.error == "Neurocore response truncated"
    assert len(harness.requests) == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    failed = next(
        item
        for item in map(json.loads, lines)
        if item["message"] == "generation_task_failed"
    )
    assert (failed["prompt_eval_count"], failed["eval_count"]) == (810, 4096)
    assert "recipes" not in failed and failed["error"] == task.error


async def test_invalid_answer_is_retried_once_then_fails(
    ready: Factory, settings: Settings
) -> None:
    reply = ollama_reply("не json")
    task_id = await add_task(ready)
    harness = rig(ready, settings, lambda r: httpx.Response(200, json=reply))
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert (task.status, task.error) == (
        TaskStatus.FAILED,
        "Invalid neurocore response",
    )
    assert len(harness.requests) == 2
    assert harness.requests[0]["prompt"] == harness.requests[1]["prompt"]
    assert harness.sample("menu_generation_rejected_total", reason="schema") == 2


async def test_bad_answer_then_good_answer_finishes_done(
    ready: Factory, settings: Settings
) -> None:
    replies = iter([ollama_reply({"recipes": []}), ollama_reply(sample_menu())])
    task_id = await add_task(ready)
    harness = rig(ready, settings, lambda r: httpx.Response(200, json=next(replies)))
    await harness.worker.process_one()
    assert (await harness.task(task_id)).status == TaskStatus.DONE
    assert len(harness.requests) == 2
    assert harness.sample("menu_generation_rejected_total", reason="count") == 1


async def test_zero_retries_means_one_attempt(
    ready: Factory, settings: Settings
) -> None:
    reply = ollama_reply("не json")
    task_id = await add_task(ready)
    harness = rig(
        ready,
        settings,
        lambda r: httpx.Response(200, json=reply),
        generation_bad_response_retries=0,
    )
    await harness.worker.process_one()
    assert (await harness.task(task_id)).status == TaskStatus.FAILED
    assert len(harness.requests) == 1


@pytest.mark.parametrize(
    ("count", "valid"), [(0, False), (2, False), (3, True), (14, True)]
)
async def test_recipe_count_rules(
    ready: Factory, settings: Settings, count: int, valid: bool
) -> None:
    menu = sample_menu()
    template = menu["recipes"][0]
    menu["recipes"] = [
        {**template, "name": f"Блюдо номер {index}"} for index in range(count)
    ]
    task_id = await add_task(ready)
    harness = rig(
        ready, settings, lambda r: httpx.Response(200, json=ollama_reply(menu))
    )
    await harness.worker.process_one()
    task = await harness.task(task_id)
    if valid:
        assert task.status == TaskStatus.DONE
        assert await harness.count(Recipe) == count
    else:
        assert (task.status, task.error) == (
            TaskStatus.FAILED,
            "Invalid neurocore response",
        )
        assert harness.sample("menu_generation_rejected_total", reason="count") == 2


def tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": name} for name in names]})


async def test_model_not_found_waits_until_the_model_is_listed(
    ready: Factory, settings: Settings
) -> None:
    task_ids = [await add_task(ready) for _ in range(3)]
    loaded: dict[str, tuple[str, ...]] = {"models": ("other:model",)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return tags(*loaded["models"])
        if loaded["models"] == ("qwen2.5:7b",):
            return good(request)
        return httpx.Response(404, json={"error": "model not found"})

    harness = rig(ready, settings, handler)
    worker = harness.worker
    assert await worker.process_one() is False
    first = await harness.task(task_ids[0])
    assert (first.status, first.error) == (
        TaskStatus.FAILED,
        "Neurocore model not found",
    )
    assert worker._idle_delay() == 5
    # Ollama отвечает 200, но нужной модели в списке нет: задачи не берутся.
    for expected in (10, 20, 40):
        assert await worker.process_one() is False
        assert worker._idle_delay() == expected
    for task_id in task_ids[1:]:
        assert (await harness.task(task_id)).status == TaskStatus.PENDING
    # Только когда модель появилась в списке, очередь разбирается дальше.
    loaded["models"] = ("qwen2.5:7b",)
    assert await worker.process_one() is True
    assert (await harness.task(task_ids[1])).status == TaskStatus.DONE
    assert worker._idle_delay() == settings.generation_worker_interval_seconds


async def test_model_with_latest_suffix_counts_as_listed(
    ready: Factory, settings: Settings
) -> None:
    await add_task(ready)
    await add_task(ready)
    answers = {"generate": httpx.Response(404, json={"error": "x"})}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return tags("qwen2.5:7b:latest")
        return answers["generate"]

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    answers["generate"] = good(None)
    assert await harness.worker.process_one() is True


async def test_server_error_500_backs_off_and_keeps_the_queue(
    ready: Factory, settings: Settings
) -> None:
    task_ids = [await add_task(ready) for _ in range(3)]
    state = {"tags": 503, "generate_ok": False}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(state["tags"], json={"models": []})
        if state["generate_ok"]:
            return good(request)
        return httpx.Response(500, text="boom")

    harness = rig(ready, settings, handler)
    worker = harness.worker
    assert await worker.process_one() is False
    first = await harness.task(task_ids[0])
    assert (first.status, first.error) == (TaskStatus.FAILED, "Neurocore error")
    assert worker._idle_delay() == 5
    assert await worker.process_one() is False
    assert worker._idle_delay() == 10
    # Для 500 достаточно, что Ollama отвечает (список моделей не проверяется).
    state["tags"] = 200
    state["generate_ok"] = True
    assert await worker.process_one() is True
    assert paths == ["/api/generate", "/api/tags", "/api/tags", "/api/generate"]
    assert (await harness.task(task_ids[2])).status == TaskStatus.PENDING


async def test_the_first_pause_after_a_failure_is_five_seconds(
    ready: Factory, settings: Settings
) -> None:
    for _ in range(3):
        await add_task(ready)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 6:
            raise asyncio.CancelledError

    harness = rig(ready, settings, lambda request: httpx.Response(503), sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await harness.worker.run()
    assert sleeps == [5, 10, 20, 40, 60, 60]


@pytest.mark.parametrize("status", [500, 404])
async def test_failing_generation_with_a_healthy_probe_keeps_growing_the_pause(
    ready: Factory, settings: Settings, status: int
) -> None:
    task_ids = [await add_task(ready) for _ in range(8)]
    generate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_calls
        if request.url.path == "/api/tags":
            # Проба всегда успешна (и модель в списке есть), а генерация падает.
            return tags("qwen2.5:7b")
        generate_calls += 1
        return httpx.Response(status, text="boom")

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 6:
            raise asyncio.CancelledError

    harness = rig(ready, settings, handler, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await harness.worker.run()
    # Успешная проба не обнуляет серию: паузы растут, на сбой ровно одна failed.
    assert sleeps == [5, 10, 20, 40, 60, 60]
    assert generate_calls == 6
    states = [(await harness.task(task_id)).status for task_id in task_ids]
    assert states == [TaskStatus.FAILED] * 6 + [TaskStatus.PENDING] * 2


async def test_only_a_successful_generation_resets_the_series(
    ready: Factory, settings: Settings
) -> None:
    for _ in range(4):
        await add_task(ready)
    healthy = {"generate": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return tags("qwen2.5:7b")
        return good(request) if healthy["generate"] else httpx.Response(500)

    harness = rig(ready, settings, handler)
    worker = harness.worker
    await worker.process_one()
    assert worker._idle_delay() == 5
    await worker.process_one()
    assert worker._idle_delay() == 10
    healthy["generate"] = True
    assert await worker.process_one() is True
    assert worker._backoff == 0 and not worker._ollama_down
    # После успеха серия начинается заново с 5 с.
    healthy["generate"] = False
    await worker.process_one()
    assert worker._idle_delay() == 5


async def test_missing_template_leaves_tasks_pending_and_logs_once(
    session_factory: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    task_ids = [await add_task(session_factory) for _ in range(3)]
    harness = rig(session_factory, settings)
    for _ in range(3):
        assert await harness.worker.process_one() is False
    for task_id in task_ids:
        task = await harness.task(task_id)
        assert (task.status, task.error) == (TaskStatus.PENDING, None)
    assert harness.requests == []
    assert harness.sample("menu_generation_tasks_total", status="failed") == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    entries = [item for item in map(json.loads, lines) if item["level"] == "error"]
    assert [(item["message"], item["reason"]) for item in entries] == [
        ("prompt_template_unusable", "missing")
    ]


@pytest.mark.parametrize("broken", ["нет плейсхолдеров", "{people_count} {other}"])
async def test_broken_template_leaves_tasks_pending_and_resumes_when_fixed(
    session_factory: Factory, settings: Settings, broken: str
) -> None:
    await add_template(session_factory, broken)
    task_ids = [await add_task(session_factory) for _ in range(2)]
    harness = rig(session_factory, settings)
    assert await harness.worker.process_one() is False
    for task_id in task_ids:
        assert (await harness.task(task_id)).status == TaskStatus.PENDING
    assert harness.requests == []
    await add_template(session_factory)
    assert await harness.worker.process_one() is True
    assert (await harness.task(task_ids[0])).status == TaskStatus.DONE


async def test_ollama_down_does_not_burn_the_queue(
    ready: Factory, settings: Settings
) -> None:
    task_ids = [await add_task(ready) for _ in range(4)]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503)

    harness = rig(ready, settings, handler)
    # Первая задача падает с unavailable, остальные не трогаются. False: следующая
    # итерация начинается с паузы, а не сразу с пробы.
    assert await harness.worker.process_one() is False
    assert (await harness.task(task_ids[0])).error == "Neurocore unavailable"
    for _ in range(3):
        assert await harness.worker.process_one() is False
    for task_id in task_ids[1:]:
        assert (await harness.task(task_id)).status == TaskStatus.PENDING
    assert calls == ["/api/generate", "/api/tags", "/api/tags", "/api/tags"]
    assert harness.sample("menu_generation_tasks_total", status="failed") == 1


async def test_backoff_grows_to_the_cap_and_resets_after_recovery(
    ready: Factory, settings: Settings
) -> None:
    for _ in range(3):
        await add_task(ready)
    online = {"up": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not online["up"]:
            raise httpx.ConnectError("refused")
        return good(request)

    harness = rig(ready, settings, handler)
    worker = harness.worker
    delays = []
    assert await worker.process_one() is False
    delays.append(worker._idle_delay())
    for _ in range(6):
        assert await worker.process_one() is False
        delays.append(worker._idle_delay())
    assert delays == [5, 10, 20, 40, 60, 60, 60]
    online["up"] = True
    assert await worker.process_one() is True
    assert not worker._ollama_down
    assert worker._idle_delay() == settings.generation_worker_interval_seconds
    assert harness.sample("menu_generation_tasks_total", status="done") == 1


async def test_timeout_also_starts_the_backoff(
    ready: Factory, settings: Settings
) -> None:
    await add_task(ready)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    assert harness.worker._idle_delay() == 5


async def test_other_failures_do_not_start_the_backoff(
    ready: Factory, settings: Settings
) -> None:
    await add_task(ready)
    reply = ollama_reply("не json")
    harness = rig(ready, settings, lambda r: httpx.Response(200, json=reply))
    await harness.worker.process_one()
    assert harness.worker._idle_delay() == settings.generation_worker_interval_seconds


async def test_unexpected_exception_in_progress_fails_the_task_and_rolls_back_menu(
    ready: Factory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: TaskRepository, task_id: UUID, menu_id: UUID) -> int:
        raise RuntimeError("write failed: secret SQL")

    monkeypatch.setattr(TaskRepository, "mark_done", broken)
    task_id = await add_task(ready)
    harness = rig(ready, settings)
    await harness.worker.process_one()
    task = await harness.task(task_id)
    # Текст ошибки безопасный: детали сбоя только в логе.
    assert (task.status, task.error) == (TaskStatus.FAILED, "Menu generation failed")
    for model in (Menu, Recipe, MenuRecipe, Ingredient):
        assert await harness.count(model) == 0


async def test_photo_from_the_model_is_never_stored(
    ready: Factory, settings: Settings
) -> None:
    menu = sample_menu()
    for recipe in menu["recipes"]:
        recipe["photo"] = "https://example.com/photo.jpg"
    task_id = await add_task(ready)
    harness = rig(
        ready, settings, lambda r: httpx.Response(200, json=ollama_reply(menu))
    )
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert task.status == TaskStatus.DONE
    assert all(link.recipe.photo is None for link in task.menu.menu_recipes)


async def test_cancellation_during_the_ollama_call_leaves_task_in_progress(
    ready: Factory, settings: Settings
) -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task_id = await add_task(ready)
    harness = rig(ready, settings, handler)
    running = asyncio.create_task(harness.worker.process_one())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    task = await harness.task(task_id)
    assert (task.status, task.error, task.menu_id) == (
        TaskStatus.IN_PROGRESS,
        None,
        None,
    )
    assert await harness.count(Menu) == 0
    assert harness.sample("menu_generation_tasks_total", status="failed") == 0


async def test_task_failed_by_restart_during_generation_is_not_overwritten(
    ready: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    task_id = await add_task(ready)

    async def handler(request: httpx.Request) -> httpx.Response:
        # «Новый контейнер» успел пометить задачу failed, пока шёл вызов модели.
        async with ready() as session, session.begin():
            await TaskRepository(session).fail_in_progress("Service restarted")
        return good(request)

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    task = await harness.task(task_id)
    assert (task.status, task.error, task.menu_id) == (
        TaskStatus.FAILED,
        "Service restarted",
        None,
    )
    for model in (Menu, Recipe, MenuRecipe):
        assert await harness.count(model) == 0
    assert harness.sample("menu_generation_tasks_total", status="done") == 0
    assert harness.sample("menu_generation_tasks_total", status="failed") == 0
    messages = [
        json.loads(line)["message"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert "generation_task_conflict" in messages
    assert "generation_task_failed" not in messages
    assert "generation_task_done" not in messages


async def test_recovery_fails_stuck_tasks_and_keeps_pending(
    ready: Factory, settings: Settings
) -> None:
    stuck = await add_task(ready)
    async with ready() as session, session.begin():
        await TaskRepository(session).claim_next()
    waiting = await add_task(ready)
    harness = rig(ready, settings)
    await harness.worker.recover()
    first, second = await harness.task(stuck), await harness.task(waiting)
    assert (first.status, first.error) == (TaskStatus.FAILED, "Service restarted")
    assert second.status == TaskStatus.PENDING


async def test_logs_carry_counters_but_no_prompt_or_answer(
    ready: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    menu = sample_menu()
    menu["recipes"][0]["calories"] = 999
    reply = ollama_reply(menu, prompt_eval_count=5000, eval_count=1234)
    await add_task(ready)
    harness = rig(ready, settings, lambda r: httpx.Response(200, json=reply))
    await harness.worker.process_one()
    output = capsys.readouterr().out
    entries = {
        item["message"]: item
        for item in map(
            json.loads, (ln for ln in output.splitlines() if ln.startswith("{"))
        )
    }
    done = entries["generation_task_done"]
    assert done["recipes"] == 3
    assert (done["prompt_eval_count"], done["eval_count"]) == (5000, 1234)
    assert "duration_seconds" in done and "generation_task_claimed" in entries
    assert entries["generation_calories_mismatch"]["mismatched"] == 1
    # 5000 + num_predict 4096 > num_ctx 8192: промпт вырос, начало могут обрезать.
    assert "ollama_context_tight" in entries
    assert "Овсяная" not in output and "Меню для" not in output


async def test_metrics_endpoint_exposes_generation_series(
    ready: Factory, settings: Settings
) -> None:
    from receipes.main import create_app

    task_id = await add_task(ready)
    app = create_app(settings, ollama_transport=httpx.MockTransport(good))
    async with app.router.lifespan_context(app):
        await app.state.worker.process_one()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            metrics = await client.get("/metrics")
    text = metrics.text
    assert 'menu_generation_tasks_total{status="done"} 1.0' in text
    assert 'ollama_requests_total{outcome="success"} 1.0' in text
    assert "menu_generation_duration_seconds_bucket" in text
    assert 'menu_generation_rejected_total{reason="duplicate_name"} 0.0' in text
    assert task_id


async def test_started_application_runs_the_worker_and_stops_it_cleanly(
    ready: Factory, settings: Settings
) -> None:
    from receipes.main import create_app

    task_id = await add_task(ready)
    config = settings.model_copy(
        update={
            "generation_worker_enabled": True,
            "generation_worker_interval_seconds": 0.05,
        }
    )
    app = create_app(config, ollama_transport=httpx.MockTransport(good))
    async with app.router.lifespan_context(app):
        for _ in range(100):
            async with ready() as session:
                task = await TaskRepository(session).get(task_id)
            if task is not None and task.status == TaskStatus.DONE:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("worker did not finish the task")


# SMAI-94: промпт сохраняется при создании задачи, а не только при захвате воркером.
async def test_prompt_is_saved_when_the_task_is_created(ready: Factory) -> None:
    task_id = await SqlTaskStore(ready).create(4, Decimal("5000.50"))
    async with ready() as session:
        assert await TaskRepository(session).get_prompt(task_id) == (
            "Меню для 4 человек, бюджет 5000.5 рублей."
        )


async def test_worker_uses_the_prompt_saved_at_creation(
    ready: Factory, settings: Settings
) -> None:
    task_id = await SqlTaskStore(ready).create(4, Decimal("5000.50"))
    await add_template(ready, "новый {people_count} {weekly_budget}")
    harness = rig(ready, settings)
    await harness.worker.process_one()
    expected = "Меню для 4 человек, бюджет 5000.5 рублей."
    assert (await harness.task(task_id)).prompt == expected
    assert harness.requests[0]["prompt"] == expected


@pytest.mark.parametrize(
    ("template", "reason"),
    [(None, "missing"), ("нет плейсхолдеров", "invalid")],
)
async def test_task_is_created_without_prompt_when_template_is_unusable(
    session_factory: Factory,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    template: str | None,
    reason: str,
) -> None:
    configure_logging("INFO")
    if template is not None:
        await add_template(session_factory, template)
    task_id = await SqlTaskStore(session_factory).create(4, Decimal("5000.50"))
    async with session_factory() as session:
        assert await TaskRepository(session).get_prompt(task_id) is None
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    entries = [item for item in map(json.loads, lines) if item["level"] == "warning"]
    assert [(item["message"], item["reason"]) for item in entries] == [
        ("task_prompt_not_saved", reason)
    ]
    assert "5000" not in "".join(lines)
    # Шаблон появился позже: воркер соберёт промпт сам.
    await add_template(session_factory)
    harness = rig(session_factory, settings)
    await harness.worker.process_one()
    done = await harness.task(task_id)
    assert done.status == TaskStatus.DONE
    assert done.prompt == "Меню для 4 человек, бюджет 5000.5 рублей."
