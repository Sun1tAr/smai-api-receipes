import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from receipes.generation.metrics import TASK_BUCKETS, GenerationMetrics
from receipes.generation.metrics_refresh import QueueMetricsRefresher

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
STATUSES = ("pending", "in_progress", "done", "failed")


def sample(registry: CollectorRegistry, name: str, **labels: str) -> float | None:
    return registry.get_sample_value(name, labels)


class FakeResult:
    def __init__(self, rows: list[tuple[str, int, float]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int, float]]:
        return self._rows


class FakeSessions:
    def __init__(self) -> None:
        self.rows: list[tuple[str, int, float]] = []
        self.error: Exception | None = None
        self.hang = False
        self.queries = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator["FakeSessions"]:
        yield self

    async def execute(self, query: Any) -> FakeResult:
        self.queries += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return FakeResult(self.rows)


def refresher(
    interval: float = 15,
) -> tuple[QueueMetricsRefresher, FakeSessions, CollectorRegistry, list[float]]:
    registry = CollectorRegistry()
    sessions = FakeSessions()
    now = [1000.0]
    instance = QueueMetricsRefresher(
        sessions,  # type: ignore[arg-type]
        GenerationMetrics(registry),
        interval,
        clock=lambda: now[0],
    )
    return instance, sessions, registry, now


def test_all_series_exist_at_zero_before_the_first_refresh() -> None:
    registry = CollectorRegistry()
    GenerationMetrics(registry)
    for status in STATUSES:
        assert sample(registry, "recipes_generation_tasks", status=status) == 0
    for status in ("pending", "in_progress"):
        age = sample(
            registry, "recipes_generation_task_oldest_age_seconds", status=status
        )
        assert age == 0
    assert sample(registry, "recipes_generation_tasks_refresh_timestamp_seconds") == 0
    for name in ("duration", "processing"):
        for status in ("done", "failed"):
            count = sample(
                registry, f"recipes_generation_task_{name}_seconds_count", status=status
            )
            assert count == 0


def test_histogram_buckets_follow_the_ticket() -> None:
    assert TASK_BUCKETS == (
        5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 360, 480, 600, 900, 1200, 1800,
        3600,
    )  # fmt: skip


async def test_refresh_sets_counts_ages_and_zeros_for_missing_statuses() -> None:
    instance, sessions, registry, _ = refresher()
    sessions.rows = [("pending", 3, 42.5), ("done", 7, 9999.0)]
    await instance.refresh_once()
    assert sessions.queries == 1
    assert sample(registry, "recipes_generation_tasks", status="pending") == 3
    assert sample(registry, "recipes_generation_tasks", status="done") == 7
    assert sample(registry, "recipes_generation_tasks", status="in_progress") == 0
    assert sample(registry, "recipes_generation_tasks", status="failed") == 0
    age = "recipes_generation_task_oldest_age_seconds"
    assert sample(registry, age, status="pending") == 42.5
    assert sample(registry, age, status="in_progress") == 0
    stamp = "recipes_generation_tasks_refresh_timestamp_seconds"
    assert sample(registry, stamp) == 1000.0


async def test_a_status_that_empties_goes_back_to_zero() -> None:
    instance, sessions, registry, _ = refresher()
    sessions.rows = [("pending", 2, 10.0)]
    await instance.refresh_once()
    sessions.rows = []
    await instance.refresh_once()
    assert sample(registry, "recipes_generation_tasks", status="pending") == 0
    age = "recipes_generation_task_oldest_age_seconds"
    assert sample(registry, age, status="pending") == 0


async def test_database_error_keeps_last_values_and_the_timestamp() -> None:
    instance, sessions, registry, now = refresher()
    sessions.rows = [("pending", 3, 5.0)]
    await instance.refresh_once()
    sessions.error = ConnectionError("password=secret SELECT 1")
    now[0] = 2000.0
    await instance.refresh_once()
    assert sample(registry, "recipes_generation_tasks", status="pending") == 3
    stamp = "recipes_generation_tasks_refresh_timestamp_seconds"
    assert sample(registry, stamp) == 1000.0


async def test_warning_once_per_outage_without_sql_and_info_on_recovery(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from receipes.observability import configure_logging

    configure_logging("DEBUG")
    instance, sessions, registry, _ = refresher()
    sessions.error = ConnectionError("password=secret SELECT 1")
    await instance.refresh_once()
    await instance.refresh_once()
    sessions.error = None
    await instance.refresh_once()
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if "generation_metrics_" in line]
    levels = [("warning" in line, "debug" in line, "info" in line) for line in lines]
    assert len(lines) == 3
    assert levels[0][0] and levels[1][1] and levels[2][2]
    assert "recovered" in lines[2]
    assert "secret" not in output and "SELECT" not in output
    assert "ConnectionError" in lines[0]


async def test_hanging_database_times_out_instead_of_blocking() -> None:
    instance, sessions, registry, _ = refresher(interval=0.05)
    sessions.hang = True
    await asyncio.wait_for(instance.refresh_once(), 1)
    stamp = "recipes_generation_tasks_refresh_timestamp_seconds"
    assert sample(registry, stamp) == 0


async def test_run_loop_refreshes_every_interval_and_survives_errors() -> None:
    instance, sessions, registry, _ = refresher(interval=0.01)
    sessions.error = ConnectionError("down")
    task = asyncio.create_task(instance.run())
    await asyncio.sleep(0.05)
    sessions.error = None
    sessions.rows = [("pending", 1, 1.0)]
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sample(registry, "recipes_generation_tasks", status="pending") == 1


def test_observe_finished_records_both_histograms_once() -> None:
    registry = CollectorRegistry()
    metrics = GenerationMetrics(registry)
    metrics.observe_finished(
        "done", T0, T0 + timedelta(seconds=100), T0 + timedelta(seconds=70)
    )
    duration = "recipes_generation_task_duration_seconds"
    processing = "recipes_generation_task_processing_seconds"
    assert sample(registry, f"{duration}_count", status="done") == 1
    assert sample(registry, f"{duration}_sum", status="done") == 100
    assert sample(registry, f"{processing}_count", status="done") == 1
    assert sample(registry, f"{processing}_sum", status="done") == 30
    assert sample(registry, f"{duration}_count", status="failed") == 0


def test_observe_without_claim_time_skips_processing() -> None:
    registry = CollectorRegistry()
    metrics = GenerationMetrics(registry)
    metrics.observe_finished("failed", T0, T0 + timedelta(seconds=40))
    processing = "recipes_generation_task_processing_seconds_count"
    assert sample(registry, processing, status="failed") == 0
    duration = "recipes_generation_task_duration_seconds_count"
    assert sample(registry, duration, status="failed") == 1


def test_negative_duration_from_clock_skew_is_clamped_to_zero() -> None:
    registry = CollectorRegistry()
    metrics = GenerationMetrics(registry)
    metrics.observe_finished("done", T0, T0 - timedelta(seconds=2), T0)
    duration = "recipes_generation_task_duration_seconds"
    assert sample(registry, f"{duration}_sum", status="done") == 0
    assert sample(registry, f"{duration}_count", status="done") == 1


async def test_a_hanging_first_refresh_does_not_block_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakes import unavailable_db_settings

    from receipes.main import create_app

    async def ready(*args: Any, **kwargs: Any) -> bool:
        return True

    async def hang(self: QueueMetricsRefresher) -> dict[str, tuple[int, float]]:
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr("receipes.main.dependencies_ready", ready)
    monkeypatch.setattr(QueueMetricsRefresher, "_fetch", hang)
    app = create_app(unavailable_db_settings(generation_worker_enabled=False))
    try:
        context = app.router.lifespan_context(app)
        await asyncio.wait_for(context.__aenter__(), 1)
        registry = app.state.metrics_registry
        stamp = "recipes_generation_tasks_refresh_timestamp_seconds"
        assert sample(registry, stamp) == 0
        await asyncio.wait_for(context.__aexit__(None, None, None), 5)
    finally:
        app.state.tracer_provider.shutdown()
