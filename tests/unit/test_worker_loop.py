import asyncio
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from receipes.worker import GenerationWorker, WorkerMetrics
from tests.unit.fakes import unavailable_db_settings


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def make_worker(interval: float = 0.01, **settings: Any):
    client = FakeClient()
    worker = GenerationWorker(
        settings=unavailable_db_settings(
            generation_worker_interval_seconds=interval, **settings
        ),
        engine=None,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        metrics=WorkerMetrics(CollectorRegistry()),
    )
    return worker, client


def other_tasks() -> set[asyncio.Task[Any]]:
    return asyncio.all_tasks() - {asyncio.current_task()}


async def test_stop_cancels_the_loop_and_leaves_no_tasks(monkeypatch) -> None:
    worker, client = make_worker()
    started = asyncio.Event()

    async def tick() -> bool:
        started.set()
        await asyncio.Event().wait()  # «зависший» вызов Ollama
        return False

    monkeypatch.setattr(worker, "tick", tick)
    worker.start()
    worker.start()  # повторный start не создаёт второй цикл
    await started.wait()
    assert len(other_tasks()) == 1
    await worker.stop()
    assert other_tasks() == set()
    assert client.closed
    await worker.stop()  # повторная остановка безопасна


async def test_stop_before_the_first_tick_is_safe() -> None:
    worker, client = make_worker()
    await worker.stop()
    assert client.closed and other_tasks() == set()


async def test_a_failing_tick_is_logged_and_does_not_stop_the_loop(monkeypatch) -> None:
    from structlog.testing import capture_logs

    worker, _ = make_worker()
    calls = 0
    recovered = asyncio.Event()

    async def tick() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("SELECT secret FROM /var/lib/db")
        recovered.set()
        return False

    monkeypatch.setattr(worker, "tick", tick)
    with capture_logs() as logs:
        worker.start()
        await recovered.wait()
        await worker.stop()
    errors = [entry for entry in logs if entry["event"] == "generation_tick_failed"]
    assert len(errors) == 2 and all(e["log_level"] == "error" for e in errors)
    assert calls >= 3
    samples = {
        sample.name: sample.value
        for metric in worker.metrics.tick_errors.collect()
        for sample in metric.samples
    }
    assert samples["generation_worker_tick_errors_total"] == 2


async def test_a_busy_queue_is_drained_without_waiting_for_the_interval(
    monkeypatch,
) -> None:
    worker, _ = make_worker(interval=3600)
    results = iter([True, True, True, False])
    drained = asyncio.Event()

    async def tick() -> bool:
        value = next(results, False)
        if not value:
            drained.set()
        return value

    monkeypatch.setattr(worker, "tick", tick)
    worker.start()
    await asyncio.wait_for(drained.wait(), timeout=5)
    await worker.stop()
    assert next(results, None) is None


async def test_cancellation_is_not_swallowed_by_the_tick_handler(monkeypatch) -> None:
    worker, _ = make_worker()
    entered = asyncio.Event()

    async def tick() -> bool:
        entered.set()
        await asyncio.sleep(3600)
        return False

    monkeypatch.setattr(worker, "tick", tick)
    worker.start()
    await entered.wait()
    await asyncio.wait_for(worker.stop(), timeout=5)
    assert other_tasks() == set()


def test_stale_threshold_covers_every_attempt_plus_a_margin() -> None:
    worker, _ = make_worker(
        ollama_timeout_seconds=600, generation_bad_response_retries=2
    )
    assert worker.attempts == 3
    assert worker.stale_after.total_seconds() == 600 * 3 + 60


@pytest.mark.parametrize("retries", [0, 1, 5])
def test_attempts_follow_the_retry_setting(retries) -> None:
    worker, _ = make_worker(generation_bad_response_retries=retries)
    assert worker.attempts == 1 + retries


async def test_tick_errors_back_off_exponentially_and_log_sparsely(monkeypatch) -> None:
    from structlog.testing import capture_logs

    import receipes.worker as module

    worker, _ = make_worker(interval=2)
    worker._lock_connection = object()
    delays: list[float] = []
    enough = asyncio.Event()
    outcomes = iter([RuntimeError] * 8 + [False])

    async def tick() -> bool:
        item = next(outcomes, False)
        if item is RuntimeError:
            raise RuntimeError("db is down")
        return bool(item)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 9:
            enough.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(worker, "tick", tick)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    with capture_logs() as logs:
        worker.start()
        await enough.wait()
        await worker.stop()
    # 2·2ⁿ секунд до потолка 30; успешный тик возвращает обычный интервал.
    assert delays == [4, 8, 16, 30, 30, 30, 30, 30, 2]
    failures = [e for e in logs if e["event"] == "generation_tick_failed"]
    assert [e["consecutive"] for e in failures] == [1, 2, 4, 8]
    recovered = [e for e in logs if e["event"] == "generation_worker_recovered"]
    assert [e["after_errors"] for e in recovered] == [8]


@pytest.mark.parametrize(("active", "expected"), [(True, 0.02), (False, 0.5)])
async def test_standby_replicas_wait_longer_than_the_active_one(
    monkeypatch, active, expected
) -> None:
    import receipes.worker as module

    worker, _ = make_worker(interval=0.02, generation_standby_interval_seconds=0.5)
    if active:
        worker._lock_connection = object()
    delays: list[float] = []
    enough = asyncio.Event()

    async def tick() -> bool:
        return False

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        enough.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "tick", tick)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    worker.start()
    await enough.wait()
    await worker.stop()
    assert delays == [expected]
    assert worker.is_active is False
