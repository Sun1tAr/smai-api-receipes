import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest
from menu_samples import UnusedDictionaries, ollama_reply, sample_menu
from opentelemetry.trace import NoOpTracer
from prometheus_client import CollectorRegistry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.database import create_lock_engine
from receipes.db.models import PromptTemplate
from receipes.db.repositories import TaskRepository
from receipes.domain.params import GenerationParams
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


def make_worker(factory: Factory, settings: Settings) -> GenerationWorker:
    metrics = GenerationMetrics(CollectorRegistry())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=ollama_reply(sample_menu()))
    )
    client = OllamaClient(
        settings,
        OllamaClient.build_http_client(settings, transport),
        metrics,
        NoOpTracer(),
    )

    async def quick_sleep(seconds: float) -> None:
        await asyncio.sleep(0.01)

    return GenerationWorker(
        factory,
        create_lock_engine(settings),
        client,
        settings,
        metrics,
        UnusedDictionaries(),
        sleep=quick_sleep,
    )


async def eventually(condition: Callable[[], Awaitable[bool] | bool]) -> None:
    for _ in range(300):
        result = condition()
        if (await result) if asyncio.iscoroutine(result) else result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met in time")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def stuck_task(factory: Factory):
    async with factory() as session, session.begin():
        task_id = await TaskRepository(session).create(
            GenerationParams(2, Decimal("100"))
        )
        await TaskRepository(session).claim_next()
    return task_id


async def status_of(factory: Factory, task_id) -> tuple[str, str | None]:
    async with factory() as session:
        task = await TaskRepository(session).get(task_id)
    assert task is not None
    return task.status, task.error


async def has_status(factory: Factory, task_id, expected: tuple) -> bool:
    return await status_of(factory, task_id) == expected


async def test_second_worker_waits_and_does_not_recover_foreign_tasks(
    session_factory: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    first = make_worker(session_factory, settings)
    second = make_worker(session_factory, settings)
    first_task = asyncio.create_task(first.run())
    await eventually(lambda: first._lock is not None)
    # Задача «живого» первого воркера: второй не имеет права её закрыть.
    live = await stuck_task(session_factory)
    second_task = asyncio.create_task(second.run())
    await asyncio.sleep(0.3)
    assert second._lock is None
    assert await status_of(session_factory, live) == (TaskStatus.IN_PROGRESS, None)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    busy = [
        item
        for item in map(json.loads, lines)
        if item["message"] == "generation_lock_busy"
    ]
    assert len(busy) == 1 and busy[0]["level"] == "warning"
    # Первый ушёл: замок освобождён, второй берёт его и закрывает зависшую задачу.
    await stop(first_task)
    await eventually(lambda: second._lock is not None)
    await eventually(
        lambda: has_status(
            session_factory, live, (TaskStatus.FAILED, "Service restarted")
        )
    )
    await stop(second_task)


async def test_second_worker_does_not_take_tasks_while_the_lock_is_busy(
    session_factory: Factory, settings: Settings
) -> None:
    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    holder = make_worker(session_factory, settings)
    assert await holder._ensure_lock() is True
    async with session_factory() as session, session.begin():
        task_id = await TaskRepository(session).create(
            GenerationParams(2, Decimal("100"))
        )
    other = make_worker(session_factory, settings)
    running = asyncio.create_task(other.run())
    await asyncio.sleep(0.3)
    assert await status_of(session_factory, task_id) == (TaskStatus.PENDING, None)
    await holder._release_lock()
    await eventually(
        lambda: has_status(session_factory, task_id, (TaskStatus.DONE, None))
    )
    await stop(running)


async def test_lock_is_released_when_the_worker_stops(
    session_factory: Factory, settings: Settings
) -> None:
    first = make_worker(session_factory, settings)
    running = asyncio.create_task(first.run())
    await eventually(lambda: first._lock is not None)
    await stop(running)
    assert first._lock is None
    second = make_worker(session_factory, settings)
    assert await second._ensure_lock() is True
    await second._release_lock()


async def test_lost_lock_connection_is_reacquired(
    session_factory: Factory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    worker = make_worker(session_factory, settings)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: worker._lock is not None)
    first_connection = worker._lock
    pid = await first_connection.scalar(text("SELECT pg_backend_pid()"))
    async with session_factory() as session:
        await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    await eventually(
        lambda: worker._lock is not None and worker._lock is not first_connection
    )
    assert "generation_lock_lost" in capsys.readouterr().out
    await stop(running)


async def test_lock_connection_has_tcp_keepalive_on_both_ends(
    session_factory: Factory, settings: Settings
) -> None:
    import socket

    worker = make_worker(session_factory, settings)
    assert await worker._ensure_lock() is True
    try:
        connection = worker._lock
        # Серверная сторона: GUC сессии (освободит замок, если клиент пропал).
        for name, expected in (
            ("tcp_keepalives_idle", "30"),
            ("tcp_keepalives_interval", "10"),
            ("tcp_keepalives_count", "3"),
        ):
            assert await connection.scalar(text(f"SHOW {name}")) == expected
        # Клиентская сторона: опции сокета asyncpg-соединения.
        raw = await connection.get_raw_connection()
        sock = raw.driver_connection._transport.get_extra_info("socket")
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        idle = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
        if idle is not None:
            assert sock.getsockopt(socket.IPPROTO_TCP, idle) == 30
        interval = getattr(socket, "TCP_KEEPINTVL", None)
        if interval is not None:
            assert sock.getsockopt(socket.IPPROTO_TCP, interval) == 10
        count = getattr(socket, "TCP_KEEPCNT", None)
        if count is not None:
            assert sock.getsockopt(socket.IPPROTO_TCP, count) == 3
    finally:
        await worker._release_lock()


async def test_application_worker_uses_the_keepalive_engine(
    settings: Settings, session_factory: Factory
) -> None:
    from receipes.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        worker = app.state.worker
        assert await worker._ensure_lock() is True
        idle = await worker._lock.scalar(text("SHOW tcp_keepalives_idle"))
        await worker._release_lock()
    assert idle == "30"
