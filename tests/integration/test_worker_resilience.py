import asyncio
import os
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from receipes.config import Settings
from receipes.repositories import TaskRepository
from tests.integration.test_generation_worker import (
    add_template,
    count,
    garbage,
    new_task,
    outcome,
    state,
    valid,
    worker_for,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeOllama:
    """Ollama, которую можно «выключать»: generate и /api/tags отвечают по флагу."""

    def __init__(self) -> None:
        self.up = True
        self.generates = 0
        self.probes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            self.probes += 1
            if not self.up:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"models": []})
        self.generates += 1
        if not self.up:
            raise httpx.ConnectError("down")
        return valid()


def with_clock(worker, clock: Clock):
    worker.breaker.clock = clock
    return worker


@pytest.fixture
async def ready(settings: Settings, session_factory):
    await add_template(session_factory)
    return settings, session_factory


async def statuses(factory) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT status FROM generation_task ORDER BY created_at, task_id")
        )
        return [row[0] for row in rows]


async def test_unavailable_ollama_does_not_drain_the_queue(ready) -> None:
    settings, factory = ready
    for age in range(5, 0, -1):
        await new_task(factory, timedelta(seconds=age))
    ollama = FakeOllama()
    ollama.up = False
    clock = Clock()
    worker = with_clock(worker_for(settings, factory, ollama), clock)
    with capture_logs() as logs:
        for _ in range(6):
            await worker.tick()
    await worker.stop()
    # Одна задача пострадала (она уже была взята), остальные ждут.
    assert sorted(await statuses(factory)) == ["failed"] + ["pending"] * 4
    assert ollama.generates == 1
    assert worker.breaker.is_open
    assert worker.metrics.breaker_open._value.get() == 1
    assert worker.metrics.breaker_opened._value.get() == 1
    assert [e["event"] for e in logs].count("ollama_breaker_opened") == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("x"), httpx.ReadTimeout("y"), httpx.Response(503)],
)
async def test_each_ollama_failure_kind_opens_the_breaker(ready, failure) -> None:
    settings, factory = ready
    await new_task(factory)
    await new_task(factory)

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    worker = worker_for(settings, factory, handler)
    await worker.tick()
    assert worker.breaker.is_open
    assert await worker.tick() is False
    await worker.stop()
    assert sorted(await statuses(factory)) == ["failed", "pending"]


async def test_invalid_responses_do_not_open_the_breaker(ready) -> None:
    settings, factory = ready
    for _ in range(3):
        await new_task(factory)
    truncated = httpx.Response(200, json={"response": "{", "done_reason": "length"})
    responses = iter([garbage(), garbage(), truncated, truncated])
    worker = worker_for(settings, factory, lambda request: next(responses))
    assert [await worker.tick() for _ in range(3)] == [True, True, True]
    await worker.stop()
    assert not worker.breaker.is_open
    assert await statuses(factory) == ["failed"] * 3


async def test_the_pause_grows_probes_fail_and_recovery_closes_the_breaker(
    ready,
) -> None:
    settings, factory = ready
    for _ in range(3):
        await new_task(factory)
    ollama = FakeOllama()
    ollama.up = False
    clock = Clock()
    worker = with_clock(worker_for(settings, factory, ollama), clock)
    await worker.tick()  # одна задача failed, breaker открыт, пауза 5 с
    assert worker.breaker.pause == 5
    clock.now += 4.9
    assert await worker.tick() is False and ollama.probes == 0  # рано для пробы
    clock.now += 0.2
    assert await worker.tick() is False and ollama.probes == 1  # проба неудачна
    assert worker.breaker.pause == 10 and worker.breaker.is_open
    clock.now += 10.1
    assert await worker.tick() is False and ollama.probes == 2
    assert worker.breaker.pause == 20
    assert ollama.generates == 1 and await statuses(factory) == [
        "failed",
        "pending",
        "pending",
    ]
    # Ollama вернулась: проба проходит, breaker закрывается, задачи доходят до done.
    ollama.up = True
    clock.now += 20.1
    with capture_logs() as logs:
        assert await worker.tick() is True
    assert not worker.breaker.is_open
    assert "ollama_breaker_closed" in [e["event"] for e in logs]
    assert await worker.tick() is True
    await worker.stop()
    assert await statuses(factory) == ["failed", "done", "done"]
    # Успешная генерация сбросила серию: следующий сбой снова начинается с 5 с.
    assert worker.breaker.streak == 0
    assert worker.metrics.breaker_open._value.get() == 0


async def test_pending_tasks_still_expire_while_the_breaker_is_open(ready) -> None:
    settings, factory = ready
    await new_task(factory)
    await new_task(factory, timedelta(hours=1))
    ollama = FakeOllama()
    ollama.up = False
    worker = with_clock(worker_for(settings, factory, ollama), Clock())
    await worker.tick()
    await worker.tick()
    await worker.stop()
    errors = []
    async with factory() as session:
        rows = await session.execute(text("SELECT status, error FROM generation_task"))
        errors = sorted(tuple(r) for r in rows)
    assert ("failed", "Queue timeout") in errors


async def test_cancelling_the_worker_mid_call_marks_the_task_interrupted(
    ready,
) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()  # вызов Ollama «висит» до отмены
        return valid()

    worker = worker_for(settings, factory, handler)
    worker.start()
    await entered.wait()
    assert (await state(factory, task_id))["status"] == "in_progress"
    await worker.stop()
    task = await state(factory, task_id)
    assert (task["status"], task["error"]) == ("failed", "Generation interrupted")
    assert task["finished_at"] is not None and await count(factory, "menu") == 0
    assert outcome(worker, "failed", "interrupted") == 1


async def test_cancellation_is_re_raised_after_recording_the_interruption(
    ready,
) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return valid()

    worker = worker_for(settings, factory, handler)
    runner = asyncio.create_task(worker.tick())
    await entered.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert (await state(factory, task_id))["error"] == "Generation interrupted"
    await worker.stop()


async def test_interruption_does_not_overwrite_a_finished_task(ready) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    entered = asyncio.Event()

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
        entered.set()
        await asyncio.Event().wait()
        return valid()

    worker = worker_for(settings, factory, handler)
    runner = asyncio.create_task(worker.tick())
    await entered.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    await worker.stop()
    # Задачу уже завершил expire_stale: отмена записала бы то же самое и не меняет её.
    assert (await state(factory, task_id))["error"] == "Generation interrupted"
    assert outcome(worker, "discarded", "interrupted") == 1


async def test_only_one_worker_is_active_at_a_time(ready) -> None:
    settings, factory = ready
    for _ in range(3):
        await new_task(factory)
    ollama = FakeOllama()
    first = worker_for(settings, factory, ollama)
    second = worker_for(settings, factory, ollama)
    assert await first.tick() is True
    assert first.metrics.leader._value.get() == 1
    # Вторая реплика ждёт, даже если в очереди есть задачи.
    assert await second.tick() is False and await second.tick() is False
    assert second.metrics.leader._value.get() == 0
    assert ollama.generates == 1
    await first.stop()
    assert first.metrics.leader._value.get() == 0
    # Остановка первой освободила блокировку: вторая становится активной.
    assert await second.tick() is True
    assert second.metrics.leader._value.get() == 1
    await second.stop()
    assert await statuses(factory) == ["done", "done", "pending"]


async def test_a_lost_lock_connection_hands_the_lead_to_another_worker(ready) -> None:
    settings, factory = ready
    ollama = FakeOllama()
    first = worker_for(settings, factory, ollama)
    second = worker_for(settings, factory, ollama)
    assert await first.tick() is False
    assert first._lock_connection is not None
    async with factory() as session:
        killed = await session.scalar(
            text(
                "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM "
                "pg_locks WHERE locktype = 'advisory' AND granted) AS t"
            )
        )
        await session.commit()
    assert killed == 1
    # Блокировка снята сервером; второй воркер перехватывает её, первый это замечает.
    await new_task(factory)
    assert await second.tick() is True
    with capture_logs() as logs:
        assert await first.tick() is False
    assert "generation_lock_connection_lost" in [e["event"] for e in logs]
    assert first._lock_connection is None
    assert first.metrics.leader._value.get() == 0
    await first.stop()
    await second.stop()


async def test_the_lock_connection_is_really_closed_on_stop(ready) -> None:
    settings, factory = ready
    worker = worker_for(settings, factory, FakeOllama())
    await worker.tick()
    async with factory() as session:
        held = await session.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        )
    assert held == 1
    await worker.stop()
    async with factory() as session:
        held = await session.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        )
    assert held == 0


async def test_outcomes_are_counted_after_the_write_and_discards_separately(
    ready,
) -> None:
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
    # Задачу успел завершить expire_stale: это «discarded», а не «failed».
    assert outcome(worker, "failed", "http_error") == 0
    assert outcome(worker, "discarded", "http_error") == 1


async def test_a_successful_done_is_counted_once_after_commit(ready) -> None:
    settings, factory = ready
    await new_task(factory)
    worker = worker_for(settings, factory, FakeOllama())
    await worker.tick()
    await worker.stop()
    assert outcome(worker, "done", "none") == 1
    assert outcome(worker, "discarded", "done") == 0


async def test_a_failing_worker_stop_does_not_leave_the_engine_open(
    settings: Settings, session_factory, monkeypatch
) -> None:
    import receipes.main as main
    from receipes.worker import GenerationWorker

    async def broken_stop(self: GenerationWorker) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(GenerationWorker, "stop", broken_stop)
    app = main.create_app(
        settings.model_copy(update={"generation_worker_enabled": True})
    )
    disposed: list[bool] = []
    real_dispose = AsyncEngine.dispose

    async def spy(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(True)
        await real_dispose(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)
    with pytest.raises(RuntimeError, match="stop failed"):
        async with app.router.lifespan_context(app):
            pass
    assert disposed == [True]


async def test_the_lock_connection_has_tcp_keepalive_enabled(ready) -> None:
    settings, factory = ready
    worker = worker_for(
        settings, factory, FakeOllama(), generation_lock_keepalive_seconds=45
    )
    await worker.tick()
    connection = worker._lock_connection
    assert connection is not None
    values = {
        name: await connection.scalar(text(f"SHOW {name}"))
        for name in (
            "tcp_keepalives_idle",
            "tcp_keepalives_interval",
            "tcp_keepalives_count",
        )
    }
    assert values == {
        "tcp_keepalives_idle": "45",
        "tcp_keepalives_interval": "15",
        "tcp_keepalives_count": "3",
    }
    assert await worker.holds_lock() is True
    await worker.stop()
    assert await worker.holds_lock() is False


async def test_a_lock_lost_during_the_call_is_noticed_before_the_result_is_saved(
    ready,
) -> None:
    settings, factory = ready
    task_id = await new_task(factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        # Молчаливый обрыв соединения блокировки, пока идёт вызов Ollama.
        async with factory() as session:
            await session.scalar(
                text(
                    "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM "
                    "pg_locks WHERE locktype = 'advisory' AND granted) AS t"
                )
            )
            await session.commit()
        return valid()

    worker = worker_for(settings, factory, handler)
    with capture_logs() as logs:
        assert await worker.tick() is True
    assert "generation_lock_lost_during_call" in [e["event"] for e in logs]
    assert worker.is_active is False
    assert worker.metrics.leader._value.get() == 0
    # Готовый результат не пропадает: задача завершена, меню сохранено.
    assert (await state(factory, task_id))["status"] == "done"
    # Следующий тик заново берёт блокировку.
    assert await worker.tick() is False
    assert worker.is_active is True
    await worker.stop()


async def test_a_repeated_cancellation_during_the_interruption_write_propagates(
    ready,
) -> None:
    settings, factory = ready
    task_id = await new_task(factory)
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return valid()

    worker = worker_for(settings, factory, handler)
    runner = asyncio.create_task(worker.tick())
    await entered.wait()
    runner.cancel()
    await asyncio.sleep(0)  # воркер начал запись «Generation interrupted»
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    # Запись идёт под shield и завершается, несмотря на вторую отмену.
    for _ in range(100):
        if (await state(factory, task_id))["status"] == "failed":
            break
        await asyncio.sleep(0.02)
    assert (await state(factory, task_id))["error"] == "Generation interrupted"
    await worker.stop()
