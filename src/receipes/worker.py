import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from time import perf_counter

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.domain import (
    GenerationParameters,
    InvalidPromptTemplate,
    PromptTemplateNotFound,
    TaskStatus,
)
from receipes.generated_menu import GeneratedMenu
from receipes.models import GenerationTask
from receipes.ollama import (
    SAFE_MESSAGES,
    FailureReason,
    GenerationFailure,
    OllamaClient,
)
from receipes.repositories import PromptTemplateRepository, TaskRepository

logger = structlog.get_logger()

# Запас к времени всех попыток: in_progress старше него считается зависшим.
STALE_MARGIN_SECONDS = 60


# Метки метрик — только фиксированные значения, без task_id и свободного текста.
class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.outcomes = Counter(
            "generation_outcomes",
            "Finished menu generations by outcome and reason.",
            ["outcome", "reason"],
            registry=registry,
        )
        self.ollama_duration = Histogram(
            "ollama_request_duration_seconds",
            "Duration of one Ollama generate call.",
            ["result"],
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900),
            registry=registry,
        )
        self.pending = Gauge(
            "generation_pending_tasks",
            "Tasks waiting in the queue.",
            registry=registry,
        )
        self.expired = Counter(
            "generation_tasks_expired",
            "Tasks failed by the queue timeout or as interrupted.",
            registry=registry,
        )
        self.tick_errors = Counter(
            "generation_worker_tick_errors",
            "Worker ticks that ended with an unexpected error.",
            registry=registry,
        )
        self.template_errors = Counter(
            "generation_template_errors",
            "Ticks skipped because the prompt template is missing or invalid.",
            registry=registry,
        )


class GenerationWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        client: OllamaClient,
        metrics: WorkerMetrics,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.client = client
        self.metrics = metrics
        self.attempts = 1 + settings.generation_bad_response_retries
        self.stale_after = timedelta(
            seconds=settings.ollama_timeout_seconds * self.attempts
            + STALE_MARGIN_SECONDS
        )
        self._task: asyncio.Task[None] | None = None

    # --- жизненный цикл -----------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="generation-worker")

    # Отмена и ожидание: после остановки не остаётся висящих задач. Задача, которую
    # прервали на вызове Ollama, остаётся in_progress; её завершит expire_stale.
    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.client.close()

    async def run(self) -> None:
        logger.info("generation_worker_started")
        try:
            while True:
                processed = False
                try:
                    processed = await self.tick()
                except Exception:
                    # Одна ошибка тика не должна останавливать воркер.
                    self.metrics.tick_errors.inc()
                    logger.error("generation_tick_failed", exc_info=True)
                if not processed:
                    await asyncio.sleep(
                        self.settings.generation_worker_interval_seconds
                    )
        finally:
            logger.info("generation_worker_stopped")

    # --- тик ---------------------------------------------------------------

    # Один тик: обслуживание очереди и обработка одной задачи. True — задача была,
    # и следующий тик нужен сразу, без паузы.
    async def tick(self) -> bool:
        await self.maintain_queue()
        build_prompt = await self.load_prompt_builder()
        if build_prompt is None:
            return False
        async with self.session_factory() as session:
            task = await TaskRepository(session).claim_next(build_prompt)
            # Коммит сразу: блокировка строки не должна жить во время вызова Ollama.
            await session.commit()
        if task is None or task.prompt is None:
            return False
        await self.process(task)
        return True

    async def maintain_queue(self) -> None:
        async with self.session_factory() as session:
            repository = TaskRepository(session)
            expired = await repository.expire_stale(
                timedelta(seconds=self.settings.generation_pending_ttl_seconds),
                self.stale_after,
            )
            pending = await repository.count_pending()
            await session.commit()
        if expired:
            self.metrics.expired.inc(expired)
            logger.warning("generation_tasks_expired", count=expired)
        self.metrics.pending.set(pending)

    # Сломанный или отсутствующий шаблон — дефект шаблона, а не задач: ошибка в лог на
    # каждом тике, задачи остаются pending.
    async def load_prompt_builder(
        self,
    ) -> Callable[[GenerationParameters], str] | None:
        try:
            async with self.session_factory() as session:
                return await PromptTemplateRepository(session).builder()
        except (InvalidPromptTemplate, PromptTemplateNotFound) as error:
            self.metrics.template_errors.inc()
            logger.error(
                "prompt_template_unusable",
                error_type=type(error).__name__,
                reason=getattr(error, "reason", "no template"),
            )
            return None

    async def process(self, task: GenerationTask) -> None:
        task_id, prompt = task.task_id, task.prompt
        assert prompt is not None
        log = logger.bind(task_id=str(task_id))
        try:
            menu = await self.generate(prompt, log)
            if not await self.finish(task_id, menu):
                # Задачу успели завершить (просрочена): меню не сохранено.
                log.warning("generation_result_discarded")
                return
        except GenerationFailure as failure:
            await self.fail(task_id, failure.reason, log)
            return
        except Exception:
            log.error("generation_failed_unexpectedly", exc_info=True)
            await self.fail(task_id, FailureReason.INTERNAL, log)
            return
        self.metrics.outcomes.labels("done", "none").inc()
        log.info("generation_done")

    # Вызов Ollama без открытых сессий и транзакций. Повторяется только невалидный
    # ответ модели (GENERATION_BAD_RESPONSE_RETRIES); недоступность и таймаут — нет.
    async def generate(
        self, prompt: str, log: structlog.stdlib.BoundLogger
    ) -> GeneratedMenu:
        for attempt in range(1, self.attempts + 1):
            started = perf_counter()
            try:
                menu = await self.client.generate_menu(prompt)
            except GenerationFailure as failure:
                self.metrics.ollama_duration.labels(failure.reason.value).observe(
                    perf_counter() - started
                )
                log.warning(
                    "ollama_call_failed",
                    reason=failure.reason.value,
                    attempt=attempt,
                    attempts=self.attempts,
                )
                if not failure.retryable or attempt == self.attempts:
                    raise
            else:
                self.metrics.ollama_duration.labels("ok").observe(
                    perf_counter() - started
                )
                return menu
        raise AssertionError("unreachable")  # pragma: no cover

    # Меню и переход в done — одной транзакцией (complete).
    async def finish(self, task_id: uuid.UUID, menu: GeneratedMenu) -> bool:
        async with self.session_factory() as session:
            done = await TaskRepository(session).complete(task_id, menu)
            if done:
                await session.commit()
            else:
                await session.rollback()
            return done

    async def fail(
        self,
        task_id: uuid.UUID,
        reason: FailureReason,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        self.metrics.outcomes.labels("failed", reason.value).inc()
        try:
            async with self.session_factory() as session:
                changed = await TaskRepository(session).transition(
                    task_id,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.FAILED,
                    error=SAFE_MESSAGES[reason],
                )
                await session.commit()
        except Exception:
            # Задача останется in_progress; её завершит expire_stale.
            log.error("generation_fail_not_recorded", exc_info=True)
            return
        if changed:
            log.warning("generation_failed", reason=reason.value)
        else:
            log.warning("generation_failure_discarded", reason=reason.value)
