import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes import unavailable_db_settings
from menu_samples import UnusedDictionaries
from prometheus_client import CollectorRegistry

from receipes.db.repositories import FinishedTask
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.worker import GenerationWorker
from receipes.observability import configure_logging

CREATED = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class StopLoop(BaseException):
    pass


async def held() -> bool:
    return True


def make_worker(
    sleep: Callable[..., Any],
    factory: Any = None,
    registry: CollectorRegistry | None = None,
) -> GenerationWorker:
    return GenerationWorker(
        factory,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        unavailable_db_settings(generation_worker_interval_seconds=7),
        GenerationMetrics(registry or CollectorRegistry()),
        UnusedDictionaries(),
        sleep=sleep,
    )


def log_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


async def test_empty_iterations_sleep_and_write_nothing_to_the_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop

    worker = make_worker(sleep)

    async def nothing() -> bool:
        return False

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = nothing  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    assert sleeps == [7, 7, 7]
    assert log_lines(capsys.readouterr().out) == []


async def test_a_processed_task_is_followed_by_the_next_without_pause() -> None:
    answers = iter([True, True, False])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise StopLoop

    worker = make_worker(sleep)

    async def process() -> bool:
        return next(answers)

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = process  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    assert sleeps == [7] and next(answers, None) is None


async def test_iteration_failures_back_off_and_log_one_traceback_per_series(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    sleeps: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 9:
            raise StopLoop

    worker = make_worker(sleep)

    async def process() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 7:
            raise ConnectionError("database is down")
        return False

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = process  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    # 1, 2, 4, 8, 16, 30 (потолок), 30; затем успех: сброс и обычный интервал 7.
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 7, 7]
    entries = log_lines(capsys.readouterr().out)
    errors = [item for item in entries if item["level"] == "error"]
    assert len(errors) == 7
    assert all(item["message"] == "generation_iteration_failed" for item in errors)
    assert "exception" in errors[0] or "exc_info" in errors[0]
    assert all("exception" not in item for item in errors[1:])
    assert errors[-1]["consecutive_failures"] == 7
    assert errors[-1]["error_type"] == "ConnectionError"


async def test_the_failure_series_restarts_after_a_good_iteration() -> None:
    sleeps: list[float] = []
    outcomes = iter([ConnectionError, False, ConnectionError, False])

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise StopLoop

    worker = make_worker(sleep)

    async def process() -> bool:
        outcome = next(outcomes)
        if outcome is ConnectionError:
            raise ConnectionError("down")
        return outcome

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = process  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    assert sleeps == [1, 7, 1, 7]


async def test_waiting_for_ollama_uses_the_backoff_not_the_interval() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise StopLoop

    worker = make_worker(sleep)
    worker._ollama_down = True
    worker._backoff = 20

    async def nothing() -> bool:
        return False

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = nothing  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    assert sleeps == [20]


async def test_cancellation_ends_the_loop() -> None:
    started = asyncio.Event()

    async def sleep(seconds: float) -> None:
        started.set()
        await asyncio.Event().wait()

    worker = make_worker(sleep)

    async def nothing() -> bool:
        return False

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = nothing  # type: ignore[method-assign]
    task = asyncio.create_task(worker.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class FailingThenWorkingSession:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator["FailingThenWorkingSession"]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionError("database is down")
        yield self

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


async def test_recovery_retries_until_the_database_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls: list[str] = []

    class Repository:
        def __init__(self, session: object) -> None:
            pass

        async def fail_in_progress(self, error: str) -> list[FinishedTask]:
            calls.append(error)
            return [FinishedTask(CREATED, CREATED + timedelta(seconds=40))] * 2

    monkeypatch.setattr("receipes.generation.worker.TaskRepository", Repository)
    factory = FailingThenWorkingSession(failures=2)
    registry = CollectorRegistry()
    worker = make_worker(sleep, factory, registry)
    await worker.recover()
    assert factory.attempts == 3
    assert sleeps == [7, 7]
    assert calls == ["Service restarted"]
    # Восстановленные задачи попадают только в полное время задачи.
    name = "recipes_generation_task_duration_seconds"
    assert registry.get_sample_value(f"{name}_count", {"status": "failed"}) == 2
    assert registry.get_sample_value(f"{name}_sum", {"status": "failed"}) == 80
    processing = "recipes_generation_task_processing_seconds_count"
    assert registry.get_sample_value(processing, {"status": "failed"}) == 0


async def test_standby_waits_ten_seconds_between_lock_attempts_not_the_interval() -> (
    None
):
    sleeps: list[float] = []
    attempts = 0

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop

    worker = make_worker(sleep)

    async def busy() -> bool:
        nonlocal attempts
        attempts += 1
        return False

    async def never() -> bool:
        raise AssertionError("tasks must not be taken without the lock")

    worker._ensure_lock = busy  # type: ignore[method-assign]
    worker.process_one = never  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    # Интервал опроса в этом тесте 7 с: пауза standby от него не зависит.
    assert sleeps == [10, 10, 10] and attempts == 3
