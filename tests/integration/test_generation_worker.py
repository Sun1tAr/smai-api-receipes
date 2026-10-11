import asyncio
import json
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from sqlalchemy import text
from structlog.testing import capture_logs

from receipes.config import Settings
from receipes.models import PromptTemplate
from receipes.ollama import OllamaClient
from receipes.repositories import TaskRepository
from receipes.worker import GenerationWorker, WorkerMetrics
from tests.integration.seed import make_task, start
from tests.unit.test_generated_menu import payload

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

TEMPLATE = 'Людей {people_count}, бюджет {weekly_budget}. Формат {"recipes": []}'
Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


def valid() -> httpx.Response:
    return httpx.Response(200, json={"response": json.dumps(payload())})


def garbage() -> httpx.Response:
    return httpx.Response(200, json={"response": "this is not json"})


class Calls:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = min(self.count, len(self.responses) - 1)
        self.count += 1
        outcome = self.responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def worker_for(settings: Settings, session_factory, handler: Handler, **overrides):
    effective = settings.model_copy(update=overrides)
    return GenerationWorker(
        settings=effective,
        engine=session_factory.kw["bind"],
        session_factory=session_factory,
        client=OllamaClient(effective, transport=httpx.MockTransport(handler)),
        metrics=WorkerMetrics(CollectorRegistry()),
    )


async def add_template(session_factory, template: str = TEMPLATE) -> None:
    async with session_factory() as session:
        session.add(PromptTemplate(id=uuid.uuid4(), template=template))
        await session.commit()


async def new_task(session_factory, age: timedelta | None = None) -> uuid.UUID:
    async with session_factory() as session:
        task = await make_task(session, age)
        await session.commit()
        return task.task_id


async def state(session_factory, task_id: uuid.UUID) -> dict[str, Any]:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, error, prompt, menu_id, started_at, finished_at "
                    "FROM generation_task WHERE task_id = :id"
                ),
                {"id": task_id},
            )
        ).one()
    return dict(row._mapping)


async def count(session_factory, table: str) -> int:
    async with session_factory() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


def outcome(worker: GenerationWorker, name: str, reason: str) -> float:
    value = worker.metrics.outcomes.labels(name, reason)._value.get()
    return float(value)


@pytest.fixture
async def ready(settings: Settings, session_factory):
    await add_template(session_factory)
    return settings, session_factory


async def test_success_saves_the_menu_and_the_prompt(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    calls = Calls(valid())
    worker = worker_for(settings, factory, calls)
    assert await worker.tick() is True
    await worker.stop()
    task = await state(factory, task_id)
    assert task["status"] == "done" and task["error"] is None
    assert task["menu_id"] is not None
    assert task["prompt"] == 'Людей 2, бюджет 1500.50. Формат {"recipes": []}'
    assert task["started_at"] is not None and task["finished_at"] is not None
    assert task["finished_at"] >= task["started_at"]
    assert calls.count == 1
    assert await count(factory, "recipe") == 3
    assert outcome(worker, "done", "none") == 1


@pytest.mark.parametrize(
    ("responses", "message", "reason", "calls_made"),
    [
        ((httpx.Response(503),), "Ollama returned an error", "http_error", 1),
        ((httpx.Response(500),), "Ollama returned an error", "http_error", 1),
        ((httpx.ReadTimeout("slow"),), "Ollama request timed out", "timeout", 1),
        ((httpx.ConnectError("no route"),), "Ollama is unavailable", "unavailable", 1),
        (
            (httpx.Response(200, json={"response": "{", "done_reason": "length"}),),
            "Model response was cut off (token limit reached)",
            "truncated",
            1,
        ),
        (
            (garbage(), garbage()),
            "Model returned an invalid menu",
            "invalid_response",
            2,
        ),
        (
            (RuntimeError("boom with secret-text"),),
            "Menu generation failed",
            "internal",
            1,
        ),
    ],
)
async def test_errors_fail_the_task_with_a_safe_message(
    ready, responses, message, reason, calls_made
) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    calls = Calls(*responses)
    worker = worker_for(settings, factory, calls)
    assert await worker.tick() is True
    await worker.stop()
    task = await state(factory, task_id)
    assert (task["status"], task["error"]) == ("failed", message)
    assert task["menu_id"] is None and task["finished_at"] is not None
    assert calls.count == calls_made
    assert await count(factory, "menu") == 0
    assert outcome(worker, "failed", reason) == 1
    assert "secret" not in str(task["error"])


async def test_invalid_first_answer_and_valid_second_is_done(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    calls = Calls(garbage(), valid())
    worker = worker_for(settings, factory, calls)
    await worker.tick()
    await worker.stop()
    assert (await state(factory, task_id))["status"] == "done"
    assert calls.count == 2


async def test_gate_rejection_is_retried_like_invalid_json(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    bad = payload()
    bad["recipes"] = bad["recipes"][:2]
    rejected = httpx.Response(200, json={"response": json.dumps(bad)})
    calls = Calls(rejected, valid())
    worker = worker_for(settings, factory, calls)
    await worker.tick()
    await worker.stop()
    assert (await state(factory, task_id))["status"] == "done"
    assert calls.count == 2


async def test_retries_follow_the_setting(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    calls = Calls(garbage(), valid())
    worker = worker_for(settings, factory, calls, generation_bad_response_retries=0)
    await worker.tick()
    await worker.stop()
    assert (await state(factory, task_id))["status"] == "failed"
    assert calls.count == 1


async def test_unavailability_and_timeout_are_never_retried(ready) -> None:
    settings, factory = ready
    for exception in (
        httpx.ConnectError("x"),
        httpx.ReadTimeout("y"),
        httpx.Response(503),
    ):
        await new_task(factory)
        calls = Calls(exception, valid())
        worker = worker_for(settings, factory, calls, generation_bad_response_retries=3)
        await worker.tick()
        await worker.stop()
        assert calls.count == 1


async def test_broken_template_leaves_tasks_pending_and_logs_every_tick(
    settings: Settings, session_factory
) -> None:
    await add_template(session_factory, "бюджет без людей {weekly_budget}")
    task_ids = [await new_task(session_factory) for _ in range(3)]
    calls = Calls(valid())
    worker = worker_for(settings, session_factory, calls)
    with capture_logs() as logs:
        assert await worker.tick() is False
        assert await worker.tick() is False
    await worker.stop()
    errors = [e for e in logs if e["event"] == "prompt_template_unusable"]
    assert len(errors) == 2 and all(e["log_level"] == "error" for e in errors)
    assert "missing" in errors[0]["reason"]
    assert calls.count == 0
    for task_id in task_ids:
        assert (await state(session_factory, task_id))["status"] == "pending"


async def test_missing_template_is_reported_and_tasks_stay_pending(
    settings: Settings, session_factory
) -> None:
    task_id = await new_task(session_factory)
    worker = worker_for(settings, session_factory, Calls(valid()))
    with capture_logs() as logs:
        assert await worker.tick() is False
    await worker.stop()
    assert [e["event"] for e in logs if e["log_level"] == "error"] == [
        "prompt_template_unusable"
    ]
    assert (await state(session_factory, task_id))["status"] == "pending"


async def test_two_workers_never_process_the_same_task(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    calls = Calls(valid())
    first = worker_for(settings, factory, calls)
    second = worker_for(settings, factory, calls)
    results = await asyncio.gather(first.tick(), second.tick())
    await first.stop()
    await second.stop()
    assert sorted(results) == [False, True]
    assert calls.count == 1
    assert (await state(factory, task_id))["status"] == "done"
    assert await count(factory, "menu") == 1


async def test_ollama_is_called_outside_any_transaction(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    observed: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        async with factory() as session:
            observed["idle_in_transaction"] = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%'"
                )
            )
            # Строка задачи не заблокирована: NOWAIT не ждёт и не падает.
            observed["row"] = await session.scalar(
                text(
                    "SELECT status FROM generation_task WHERE task_id = :id "
                    "FOR UPDATE NOWAIT"
                ),
                {"id": task_id},
            )
            await session.rollback()
        return valid()

    worker = worker_for(settings, factory, handler)
    await worker.tick()
    await worker.stop()
    assert observed == {"idle_in_transaction": 0, "row": "in_progress"}
    assert (await state(factory, task_id))["status"] == "done"


async def test_task_expired_during_the_call_does_not_get_a_menu(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE generation_task SET started_at = now() - interval "
                    "'3 hours' WHERE task_id = :id"
                ),
                {"id": task_id},
            )
            expired = await TaskRepository(session).expire_stale(
                timedelta(days=1), timedelta(minutes=10)
            )
            await session.commit()
        assert expired == 1
        return valid()

    worker = worker_for(settings, factory, handler)
    with capture_logs() as logs:
        assert await worker.tick() is True
    await worker.stop()
    task = await state(factory, task_id)
    assert (task["status"], task["error"]) == ("failed", "Generation interrupted")
    assert task["menu_id"] is None
    for table in ("menu", "recipe", "nutrition", "instruction", "recipe_ingredients"):
        assert await count(factory, table) == 0
    assert [e["event"] for e in logs if e["log_level"] == "warning"].count(
        "generation_result_discarded"
    ) == 1


async def test_failure_after_expiry_does_not_overwrite_the_task(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE generation_task SET started_at = now() - interval "
                    "'3 hours' WHERE task_id = :id"
                ),
                {"id": task_id},
            )
            await TaskRepository(session).expire_stale(
                timedelta(days=1), timedelta(minutes=10)
            )
            await session.commit()
        return httpx.Response(503)

    worker = worker_for(settings, factory, handler)
    await worker.tick()
    await worker.stop()
    assert (await state(factory, task_id))["error"] == "Generation interrupted"


async def test_tick_expires_old_pending_tasks_and_updates_the_gauge(ready) -> None:
    settings, factory = ready
    old = await new_task(factory, timedelta(hours=1))
    fresh = await new_task(factory, timedelta(seconds=1))
    # Захват без вызова Ollama: шаблон ломается после обслуживания очереди.
    calls = Calls(httpx.Response(503))
    worker = worker_for(settings, factory, calls, generation_pending_ttl_seconds=900)
    await worker.tick()
    await worker.stop()
    assert (await state(factory, old))["error"] == "Queue timeout"
    assert (await state(factory, fresh))["status"] == "failed"  # 503 от Ollama
    assert worker.metrics.expired._value.get() == 1
    assert worker.metrics.pending._value.get() == 1  # свежая считалась до захвата


async def test_stuck_in_progress_tasks_are_failed_by_the_tick(ready) -> None:
    settings, factory = ready
    async with factory() as session:
        task = await make_task(session)
        await start(session, task.task_id, timedelta(hours=5))
        await session.commit()
    worker = worker_for(settings, factory, Calls(valid()))
    assert await worker.tick() is False
    await worker.stop()
    assert (await state(factory, task.task_id))["error"] == "Generation interrupted"


async def test_an_empty_queue_is_a_quiet_tick(ready) -> None:
    settings, factory = ready
    calls = Calls(valid())
    worker = worker_for(settings, factory, calls)
    assert await worker.tick() is False
    await worker.stop()
    assert calls.count == 0
    assert worker.metrics.pending._value.get() == 0


async def test_logs_never_contain_prompts_or_model_output(ready) -> None:
    settings, factory = ready
    await new_task(factory)
    leak = httpx.Response(200, json={"response": "SECRET-MODEL-OUTPUT"})
    worker = worker_for(settings, factory, Calls(leak, leak))
    with capture_logs() as logs:
        await worker.tick()
    await worker.stop()
    dump = json.dumps(logs, default=str)
    assert "SECRET-MODEL-OUTPUT" not in dump and "Людей" not in dump
    assert "task_id" in dump


async def test_running_service_processes_a_task_end_to_end(
    settings: Settings, session_factory, monkeypatch
) -> None:
    import receipes.main as main

    await add_template(session_factory)
    transport = httpx.MockTransport(Calls(valid()))
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
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/generate-menu", json={"peopleCount": 2, "weeklyBudget": 1000}
            )
            task_id = created.json()["task_id"]
            body: dict[str, Any] = {}
            for _ in range(200):
                body = (await client.get(f"/generate-menu/{task_id}")).json()
                if body["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            metrics = (await client.get("/metrics")).text
    assert body["status"] == "done" and len(body["menu"]["recipes"]) == 3
    assert 'generation_outcomes_total{outcome="done",reason="none"} 1.0' in metrics
    assert "ollama_request_duration_seconds_count" in metrics
    assert "generation_pending_tasks" in metrics
    assert task_id not in metrics
