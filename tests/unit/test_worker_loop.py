import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakes import unavailable_db_settings
from prometheus_client import CollectorRegistry

from receipes.generation.metrics import GenerationMetrics
from receipes.generation.worker import GenerationWorker
from receipes.observability import configure_logging


class StopLoop(BaseException):
    pass


async def held() -> bool:
    return True


def make_worker(sleep: Callable[..., Any], factory: Any = None) -> GenerationWorker:
    return GenerationWorker(
        factory,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        unavailable_db_settings(generation_worker_interval_seconds=7),
        GenerationMetrics(CollectorRegistry()),
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


async def test_an_exception_in_an_iteration_is_logged_and_the_loop_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    calls = 0

    async def sleep(seconds: float) -> None:
        if calls >= 2:
            raise StopLoop

    worker = make_worker(sleep)

    async def process() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database is down")
        return False

    worker._ensure_lock = held  # type: ignore[method-assign]
    worker.process_one = process  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await worker.run()
    assert calls == 2
    errors = [
        item for item in log_lines(capsys.readouterr().out) if item["level"] == "error"
    ]
    assert [item["message"] for item in errors] == ["generation_iteration_failed"]


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

        async def fail_in_progress(self, error: str) -> int:
            calls.append(error)
            return 2

    monkeypatch.setattr("receipes.generation.worker.TaskRepository", Repository)
    factory = FailingThenWorkingSession(failures=2)
    worker = make_worker(sleep, factory)
    await worker.recover()
    assert factory.attempts == 3
    assert sleeps == [7, 7]
    assert calls == ["Service restarted"]
