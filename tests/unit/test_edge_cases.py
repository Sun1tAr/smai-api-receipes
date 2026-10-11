"""Редкие ветки, которые не достигаются обычными сценариями."""

import asyncio
import runpy
import uuid
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from receipes.domain import GenerationTaskNotFound, InvalidGenerationParameters
from receipes.errors import (
    invalid_parameters_handler,
    task_not_found_handler,
    unexpected_error_handler,
)
from receipes.generated_menu import GeneratedMenu
from receipes.migration_diagnostics import find_postgres_error
from receipes.observability import create_tracer_provider, exception_summary
from receipes.ollama import FailureReason
from receipes.worker import GenerationWorker, WorkerMetrics
from tests.unit.fakes import unavailable_db_settings
from tests.unit.test_generated_menu import payload


async def test_error_handlers_return_the_project_format() -> None:
    request: Any = None
    invalid = await invalid_parameters_handler(
        request, InvalidGenerationParameters("weekly_budget", "bad")
    )
    assert invalid.status_code == 400
    assert invalid.body == (
        b'{"error":{"code":"VALIDATION_ERROR","message":"Request validation failed",'
        b'"details":{"fields":[{"loc":["body","weeklyBudget"],"type":"value_error"}]}}}'
    )
    missing = await task_not_found_handler(request, GenerationTaskNotFound("x"))
    assert missing.status_code == 404
    with capture_logs() as logs:
        crashed = await unexpected_error_handler(request, RuntimeError("secret"))
    assert crashed.status_code == 500 and b"secret" not in crashed.body
    assert [e["event"] for e in logs] == ["unhandled_error"]


def test_a_domain_error_raised_by_a_route_is_a_400() -> None:
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())

    @app.get("/limit")
    async def limit() -> None:
        raise InvalidGenerationParameters("people_count", "too many")

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/limit")

    response = asyncio.run(call())
    assert response.status_code == 400
    assert response.json()["error"]["details"]["fields"] == [
        {"loc": ["body", "peopleCount"], "type": "value_error"}
    ]


def test_ingredient_name_that_grows_under_casefold_is_rejected() -> None:
    from pydantic import ValidationError

    data = payload()
    # «ß» при casefold превращается в «ss»: имя помещается в колонку, ключ — нет.
    data["recipes"][0]["ingredients"][0]["name"] = "ß" * 200
    with pytest.raises(ValidationError):
        GeneratedMenu.model_validate(data)


def test_a_cyclic_exception_chain_is_walked_once() -> None:
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert find_postgres_error(first) is None


def test_exception_summary_without_an_active_exception_changes_nothing() -> None:
    event: dict[str, Any] = {"exc_info": True, "event": "x"}
    assert exception_summary(None, "error", event) == {"event": "x"}
    assert exception_summary(None, "error", {"event": "y"}) == {"event": "y"}


def test_exception_summary_caps_causes_and_honours_suppressed_context() -> None:
    error: BaseException = RuntimeError("root")
    for index in range(10):
        wrapper = RuntimeError(f"level {index}")
        wrapper.__cause__ = error
        error = wrapper
    summary = exception_summary(None, "error", {"exc_info": error})["exception"]
    assert len(summary["causes"]) == 5

    try:
        try:
            raise ValueError("inner")
        except ValueError:
            raise KeyError("outer") from None
    except KeyError as suppressed:
        result = exception_summary(None, "error", {"exc_info": suppressed})
    assert result["exception"]["causes"] == []


def test_tracer_provider_with_a_collector_gets_a_span_processor() -> None:
    provider = create_tracer_provider(
        unavailable_db_settings(otel_exporter_otlp_endpoint="http://collector:4318")
    )
    processors = provider._active_span_processor._span_processors
    assert len(processors) == 1
    provider.shutdown()
    bare = create_tracer_provider(unavailable_db_settings())
    assert len(bare._active_span_processor._span_processors) == 0
    bare.shutdown()


def test_python_dash_m_receipes_starts_the_server(monkeypatch) -> None:
    import uvicorn

    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(options))
    for key, value in {
        "DB_HOST": "postgres",
        "DB_NAME": "receipes",
        "DB_USER": "receipes",
        "DB_PASSWORD": "local-only",
        "OLLAMA_BASE_URL": "http://ollama.test:11434",
    }.items():
        monkeypatch.setenv(key, value)
    runpy.run_module("receipes", run_name="__main__")
    assert started and started[0]["port"] == 8000


# --- воркер: запись прерывания и сбой записи failed ---------------------------------


def make_worker() -> GenerationWorker:
    return GenerationWorker(
        settings=unavailable_db_settings(),
        engine=None,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        client=None,  # type: ignore[arg-type]
        metrics=WorkerMetrics(CollectorRegistry()),
    )


class BrokenSessions:
    def __call__(self) -> "BrokenSessions":
        return self

    async def __aenter__(self) -> None:
        raise ConnectionError("db is gone")

    async def __aexit__(self, *exc: object) -> None:  # pragma: no cover
        return None


async def test_a_failure_that_cannot_be_recorded_is_logged_not_raised() -> None:
    import structlog

    worker = make_worker()
    worker.session_factory = BrokenSessions()  # type: ignore[assignment]
    log = structlog.get_logger()
    with capture_logs() as logs:
        await worker.fail(uuid.uuid4(), FailureReason.TIMEOUT, log)
    assert [e["event"] for e in logs] == ["generation_fail_not_recorded"]
    # Исход не засчитывается: записать его в БД не удалось.
    assert worker.metrics.outcomes.labels("failed", "timeout")._value.get() == 0


async def test_a_slow_interruption_write_times_out_without_raising(monkeypatch) -> None:
    import structlog

    import receipes.worker as module

    worker = make_worker()
    release = asyncio.Event()

    async def slow_fail(*args: Any, **kwargs: Any) -> None:
        await release.wait()

    monkeypatch.setattr(worker, "fail", slow_fail)
    monkeypatch.setattr(module, "INTERRUPT_WRITE_TIMEOUT_SECONDS", 0.05)
    with capture_logs() as logs:
        await worker.record_interruption(uuid.uuid4(), structlog.get_logger())
    assert [e["event"] for e in logs] == ["generation_interruption_not_recorded"]
    release.set()
    await asyncio.sleep(0)  # дать завершиться защищённой записи
