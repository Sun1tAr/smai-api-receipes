import asyncio
import os
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from test_worker import (
    Factory,
    Rig,
    add_task,
    add_template,
    good,
    rig,
)

from receipes.config import Settings
from receipes.db.repositories import TaskRepository
from receipes.domain.status import TaskStatus
from receipes.generation.metrics_refresh import QueueMetricsRefresher

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

SQL_PARAMETERS = """
INSERT INTO generation_parameters (id, people_count, weekly_budget)
VALUES ('00000000-0000-0000-0000-000000000001', 2, 100)
"""
SQL_TASK = """
INSERT INTO generation_task (task_id, status, parameters_id, created_at)
VALUES ('{task_id}', '{status}', '00000000-0000-0000-0000-000000000001', {created})
"""
SQL_NULLABLE = """
SELECT is_nullable FROM information_schema.columns
WHERE table_name = 'generation_task' AND column_name = 'status_changed_at'
"""
SQL_INDEX = """
SELECT indexdef FROM pg_indexes
WHERE indexname = 'ix_generation_task_status_status_changed_at'
"""
OLD_ID = "00000000-0000-0000-0000-0000000000a1"
NEW_ID = "00000000-0000-0000-0000-0000000000a2"

DURATION = "recipes_generation_task_duration_seconds"
PROCESSING = "recipes_generation_task_processing_seconds"


def refresher(harness: Rig, factory: Factory) -> QueueMetricsRefresher:
    return QueueMetricsRefresher(factory, harness.worker._metrics, 15)


def count(harness: Rig, name: str, status: str) -> float:
    return harness.sample(f"{name}_count", status=status)


async def backdate(factory: Factory, task_id: UUID, seconds: int) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE generation_task SET "
                "created_at = created_at - make_interval(secs => :shift), "
                "status_changed_at = status_changed_at "
                "- make_interval(secs => :shift) WHERE task_id = :task_id"
            ),
            {"shift": seconds, "task_id": task_id},
        )


@pytest.fixture
async def ready(session_factory: Factory) -> Factory:
    await add_template(session_factory)
    return session_factory


async def test_migration_fills_existing_rows_with_created_at(
    settings: Settings, schema: None, alembic: Any
) -> None:
    from receipes.database import create_engine

    engine = create_engine(settings)
    try:
        alembic("downgrade", "0002_seed_prompt_template")
        async with engine.connect() as connection:
            assert await connection.scalar(text(SQL_NULLABLE)) is None
            assert await connection.scalar(text(SQL_INDEX)) is None
        old = SQL_TASK.format(
            task_id=OLD_ID, status="done", created="'2026-01-02 03:04:05+00'"
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE generation_task, generation_parameters CASCADE")
            )
            await connection.execute(text(SQL_PARAMETERS))
            await connection.execute(text(old))
        alembic("upgrade", "head")
        async with engine.begin() as connection:
            equal = await connection.scalar(
                text("SELECT created_at = status_changed_at FROM generation_task")
            )
            assert equal is True
            assert await connection.scalar(text(SQL_NULLABLE)) == "NO"
            index = await connection.scalar(text(SQL_INDEX))
            assert index is not None and "(status, status_changed_at)" in index
            # DEFAULT now(): вставка в обход репозитория не падает.
            new = SQL_TASK.format(task_id=NEW_ID, status="pending", created="now()")
            await connection.execute(text(new))
            await connection.execute(
                text("TRUNCATE generation_task, generation_parameters CASCADE")
            )
    finally:
        alembic("upgrade", "head")
        await engine.dispose()


async def test_new_task_starts_with_status_changed_at_equal_to_created_at(
    ready: Factory,
) -> None:
    task_id = await add_task(ready)
    async with ready() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None and task.status_changed_at == task.created_at


async def test_gauges_follow_the_queue_and_every_task_is_counted_once(
    ready: Factory, settings: Settings
) -> None:
    for _ in range(3):
        await add_task(ready)
    harness = rig(ready, settings)
    refresh = refresher(harness, ready)
    await refresh.refresh_once()
    assert harness.sample("recipes_generation_tasks", status="pending") == 3
    assert harness.sample("recipes_generation_tasks_refresh_timestamp_seconds") > 0

    assert await harness.worker.process_one() is True
    await refresh.refresh_once()
    assert harness.sample("recipes_generation_tasks", status="pending") == 2
    assert harness.sample("recipes_generation_tasks", status="done") == 1
    assert harness.sample("recipes_generation_tasks", status="in_progress") == 0
    assert count(harness, DURATION, "done") == 1
    assert count(harness, PROCESSING, "done") == 1
    assert count(harness, DURATION, "failed") == 0


async def test_duration_includes_the_queue_wait_and_processing_does_not(
    ready: Factory, settings: Settings
) -> None:
    task_id = await add_task(ready)
    await backdate(ready, task_id, 100)
    harness = rig(ready, settings)
    await harness.worker.process_one()
    duration = harness.sample(f"{DURATION}_sum", status="done")
    processing = harness.sample(f"{PROCESSING}_sum", status="done")
    assert duration >= 100
    assert 0 <= processing < 30


async def test_oldest_age_grows_for_pending_and_in_progress(
    ready: Factory, settings: Settings
) -> None:
    stuck = await add_task(ready)
    async with ready() as session, session.begin():
        await TaskRepository(session).claim_next()
    waiting = await add_task(ready)
    await backdate(ready, stuck, 300)
    await backdate(ready, waiting, 120)
    harness = rig(ready, settings)
    await refresher(harness, ready).refresh_once()
    age = "recipes_generation_task_oldest_age_seconds"
    assert 300 <= harness.sample(age, status="in_progress") < 330
    assert 120 <= harness.sample(age, status="pending") < 150


async def test_failed_task_is_counted_with_failed_status(
    ready: Factory, settings: Settings
) -> None:
    await add_task(ready)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    assert count(harness, DURATION, "failed") == 1
    assert count(harness, PROCESSING, "failed") == 1
    assert count(harness, DURATION, "done") == 0


async def test_ollama_down_fails_one_task_and_keeps_the_rest_pending(
    ready: Factory, settings: Settings
) -> None:
    for _ in range(3):
        await add_task(ready)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    harness = rig(ready, settings, handler)
    refresh = refresher(harness, ready)
    for _ in range(4):
        await harness.worker.process_one()
        await refresh.refresh_once()
        # Задача никогда не возвращается из in_progress в pending.
        assert harness.sample("recipes_generation_tasks", status="in_progress") == 0
    assert harness.sample("recipes_generation_tasks", status="failed") == 1
    assert harness.sample("recipes_generation_tasks", status="pending") == 2
    assert count(harness, DURATION, "failed") == 1


async def test_two_workers_count_each_task_exactly_once(
    ready: Factory, settings: Settings
) -> None:
    total = 6
    for _ in range(total):
        await add_task(ready)
    first, second = rig(ready, settings), rig(ready, settings)

    async def drain(harness: Rig) -> None:
        while await harness.worker.process_one():
            pass

    await asyncio.gather(drain(first), drain(second))
    for name in (DURATION, PROCESSING):
        counted = count(first, name, "done") + count(second, name, "done")
        assert counted == total


async def test_a_task_taken_over_during_generation_is_not_observed(
    ready: Factory, settings: Settings
) -> None:
    await add_task(ready)

    async def handler(request: httpx.Request) -> httpx.Response:
        # «Новый контейнер» уже закрыл задачу как failed, пока шёл вызов модели.
        async with ready() as session, session.begin():
            await TaskRepository(session).fail_in_progress("Service restarted")
        return good(request)

    harness = rig(ready, settings, handler)
    await harness.worker.process_one()
    for name in (DURATION, PROCESSING):
        assert count(harness, name, "done") == 0
        assert count(harness, name, "failed") == 0


async def test_recovered_task_counts_in_duration_only(
    ready: Factory, settings: Settings
) -> None:
    stuck = await add_task(ready)
    async with ready() as session, session.begin():
        await TaskRepository(session).claim_next()
    harness = rig(ready, settings)
    await harness.worker.recover()
    assert count(harness, DURATION, "failed") == 1
    assert count(harness, PROCESSING, "failed") == 0
    async with ready() as session:
        task = await TaskRepository(session).get(stuck)
    assert task is not None and task.status == TaskStatus.FAILED
    assert task.status_changed_at > task.created_at


async def test_metrics_endpoint_answers_when_the_database_goes_away(
    ready: Factory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from receipes.main import create_app

    interval = 0.2
    await add_task(ready)
    config = settings.model_copy(
        update={"generation_metrics_refresh_seconds": interval}
    )
    app = create_app(config, ollama_transport=httpx.MockTransport(good))
    original = QueueMetricsRefresher._fetch

    async def broken(self: QueueMetricsRefresher) -> dict[str, tuple[int, float]]:
        raise ConnectionError("database is down")

    stamp = "recipes_generation_tasks_refresh_timestamp_seconds"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(50):
                before = (await client.get("/metrics")).text
                if _value(before, stamp) > 0:
                    break
                await asyncio.sleep(0.1)
            monkeypatch.setattr(QueueMetricsRefresher, "_fetch", broken)
            # Тик, начатый до подмены, успевает закончиться: интервал + таймаут.
            await asyncio.sleep(2 * interval + 0.2)
            first = _value((await client.get("/metrics")).text, stamp)
            await asyncio.sleep(3 * interval)
            after = await client.get("/metrics")
            monkeypatch.setattr(QueueMetricsRefresher, "_fetch", original)
    assert 'recipes_generation_tasks{status="pending"} 1.0' in before
    assert after.status_code == 200
    assert first > 0 and _value(after.text, stamp) == first


def _value(text_: str, name: str) -> float:
    line = next(line for line in text_.splitlines() if line.startswith(name + " "))
    return float(line.split()[1])
