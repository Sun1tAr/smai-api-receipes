import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from time import perf_counter

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from receipes.breaker import CircuitBreaker
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
# Один активный воркер на кластер: pg_try_advisory_lock на отдельном соединении.
ADVISORY_LOCK_ID = 7_243_219_907_100_001
# Потолок экспоненциальной паузы при ошибках тика (например, БД недоступна).
TICK_BACKOFF_CAP_SECONDS = 30
# Сколько отмена воркера ждёт записи «Generation interrupted».
INTERRUPT_WRITE_TIMEOUT_SECONDS = 5
# Сбои Ollama, после которых breaker не пускает новые задачи.
BREAKER_REASONS = frozenset(
    {FailureReason.UNAVAILABLE, FailureReason.TIMEOUT, FailureReason.HTTP_ERROR}
)


# Метки метрик — только фиксированные значения, без task_id и свободного текста.
class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.outcomes = Counter(
            "generation_outcomes",
            "Finished menu generations by outcome and reason. Outcome 'discarded' "
            "means the result could not be recorded because the task had expired; "
            "its reason is the outcome that was intended.",
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
        self.breaker_open = Gauge(
            "generation_breaker_open",
            "1 while the Ollama circuit breaker is open (no new tasks are taken).",
            registry=registry,
        )
        self.breaker_opened = Counter(
            "generation_breaker_opened",
            "Times the Ollama circuit breaker opened.",
            registry=registry,
        )
        self.leader = Gauge(
            "generation_worker_active",
            "1 while this replica holds the generation advisory lock.",
            registry=registry,
        )


class GenerationWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        client: OllamaClient,
        metrics: WorkerMetrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.session_factory = session_factory
        self.client = client
        self.metrics = metrics
        self.attempts = 1 + settings.generation_bad_response_retries
        self.stale_after = timedelta(
            seconds=settings.ollama_timeout_seconds * self.attempts
            + STALE_MARGIN_SECONDS
        )
        self.breaker = CircuitBreaker(
            settings.generation_breaker_min_pause_seconds,
            settings.generation_breaker_max_pause_seconds,
            clock,
        )
        self._task: asyncio.Task[None] | None = None
        self._lock_connection: AsyncConnection | None = None

    # --- жизненный цикл -----------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="generation-worker")

    # Отмена и ожидание: после остановки не остаётся висящих задач. Прерванная на
    # вызове Ollama задача помечается failed (см. process). Блокировка и клиент
    # освобождаются независимо друг от друга.
    async def stop(self) -> None:
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            try:
                await self.release_leadership()
            finally:
                await self.client.close()

    async def run(self) -> None:
        logger.info("generation_worker_started")
        errors = 0
        try:
            while True:
                processed = False
                try:
                    processed = await self.tick()
                except Exception:
                    # Одна ошибка тика не останавливает воркер. Лог — на смену
                    # состояния и затем по нарастающей (1, 2, 4, 8 …), пауза растёт.
                    errors += 1
                    self.metrics.tick_errors.inc()
                    if errors & (errors - 1) == 0:
                        logger.error(
                            "generation_tick_failed", consecutive=errors, exc_info=True
                        )
                else:
                    if errors:
                        logger.info("generation_worker_recovered", after_errors=errors)
                        errors = 0
                if processed:
                    continue
                interval = self.settings.generation_worker_interval_seconds
                if errors:
                    delay = min(interval * 2**errors, TICK_BACKOFF_CAP_SECONDS)
                elif self.is_active:
                    delay = interval
                else:
                    # Неактивная реплика не открывает соединение каждые 2 секунды.
                    delay = self.settings.generation_standby_interval_seconds
                await asyncio.sleep(delay)
        finally:
            logger.info("generation_worker_stopped")

    # --- один активный воркер -----------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._lock_connection is not None

    # Блокировка держится на отдельном соединении в AUTOCOMMIT; она снимается при
    # закрытии или обрыве соединения. Потеря соединения замечается на следующем тике:
    # воркер перестаёт считать себя активным и пробует взять блокировку заново.
    async def ensure_leader(self) -> bool:
        if self._lock_connection is not None:
            try:
                await self._lock_connection.execute(text("SELECT 1"))
                return True
            except Exception:
                logger.warning("generation_lock_connection_lost")
                await self.release_leadership()
        connection = await self.engine.connect()
        try:
            connection = await connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await self.enable_keepalive(connection)
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}
            )
        except BaseException:
            await self.discard(connection)
            raise
        if not acquired:
            await connection.close()
            return False
        self._lock_connection = connection
        self.metrics.leader.set(1)
        logger.info("generation_worker_active")
        return True

    # Соединение блокировки простаивает, пока идёт вызов Ollama: keepalive на стороне
    # сервера быстрее замечает молчаливый обрыв и снимает блокировку.
    async def enable_keepalive(self, connection: AsyncConnection) -> None:
        idle = self.settings.generation_lock_keepalive_seconds
        for name, value in (
            ("tcp_keepalives_idle", idle),
            ("tcp_keepalives_interval", max(idle // 3, 1)),
            ("tcp_keepalives_count", 3),
        ):
            await connection.execute(
                text("SELECT set_config(:name, :value, false)"),
                {"name": name, "value": str(value)},
            )

    # Блокировка ещё принадлежит этому соединению (pg_locks по собственному pid).
    async def holds_lock(self) -> bool:
        connection = self._lock_connection
        if connection is None:
            return False
        try:
            held = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND granted AND pid = pg_backend_pid() "
                    "AND (classid::bigint << 32) | objid::bigint = :id"
                ),
                {"id": ADVISORY_LOCK_ID},
            )
        except Exception:
            return False
        return bool(held)

    async def release_leadership(self) -> None:
        connection, self._lock_connection = self._lock_connection, None
        self.metrics.leader.set(0)
        if connection is not None:
            await self.discard(connection)

    # Соединение с блокировкой не возвращается в пул, а закрывается по-настоящему.
    @staticmethod
    async def discard(connection: AsyncConnection) -> None:
        with contextlib.suppress(Exception):
            await connection.invalidate()
        with contextlib.suppress(Exception):
            await connection.close()

    # --- тик ---------------------------------------------------------------

    # Один тик: обслуживание очереди и обработка одной задачи. True — задача была,
    # и следующий тик нужен сразу, без паузы.
    async def tick(self) -> bool:
        if not await self.ensure_leader():
            return False
        await self.maintain_queue()
        build_prompt = await self.load_prompt_builder()
        if build_prompt is None:
            return False
        if not await self.breaker_allows():
            return False
        async with self.session_factory() as session:
            task = await TaskRepository(session).claim_next(build_prompt)
            # Коммит сразу: блокировка строки не должна жить во время вызова Ollama.
            await session.commit()
        if task is None or task.prompt is None:
            return False
        await self.process(task)
        return True

    # Открытый breaker не пускает новые задачи: после паузы проверяется доступность
    # Ollama (GET /api/tags), и только удачная проба закрывает его.
    async def breaker_allows(self) -> bool:
        if not self.breaker.is_open:
            return True
        if not self.breaker.probe_due():
            return False
        if await self.client.probe():
            self.breaker.record_probe_success()
            self.metrics.breaker_open.set(0)
            logger.info("ollama_breaker_closed")
            return True
        self.breaker.record_failure()
        logger.warning("ollama_probe_failed", next_pause_seconds=self.breaker.pause)
        return False

    def note_ollama_failure(self, reason: FailureReason) -> None:
        if reason in BREAKER_REASONS and self.breaker.record_failure():
            self.metrics.breaker_opened.inc()
            self.metrics.breaker_open.set(1)
            logger.warning(
                "ollama_breaker_opened",
                reason=reason.value,
                pause_seconds=self.breaker.pause,
            )

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
            await self.verify_leadership(log)
            done = await self.finish(task_id, menu)
        except GenerationFailure as failure:
            self.note_ollama_failure(failure.reason)
            await self.fail(task_id, failure.reason, log)
            return
        except asyncio.CancelledError:
            # Остановка посреди вызова (деплой): без этого задача висела бы
            # in_progress до expire_stale. Запись защищена от повторной отмены.
            await self.record_interruption(task_id, log)
            raise
        except Exception:
            log.error("generation_failed_unexpectedly", exc_info=True)
            await self.fail(task_id, FailureReason.INTERNAL, log)
            return
        if done:
            self.breaker.record_success()
            self.metrics.breaker_open.set(0)
            self.metrics.outcomes.labels("done", "none").inc()
            log.info("generation_done")
        else:
            # Задачу успели завершить (просрочена): меню не сохранено.
            self.metrics.outcomes.labels("discarded", "done").inc()
            log.warning("generation_result_discarded")

    # Во время долгого вызова соединение блокировки простаивало: если блокировка
    # потеряна, воркер перестаёт считать себя активным (готовый результат всё равно
    # сохраняется: задача уже его, а другая реплика берёт только другие задачи).
    async def verify_leadership(self, log: structlog.stdlib.BoundLogger) -> None:
        if self._lock_connection is not None and not await self.holds_lock():
            log.warning("generation_lock_lost_during_call")
            await self.release_leadership()

    async def record_interruption(
        self, task_id: uuid.UUID, log: structlog.stdlib.BoundLogger
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(self.fail(task_id, FailureReason.INTERRUPTED, log)),
                INTERRUPT_WRITE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # Повторная отмена: запись продолжает выполняться под shield, а отмена
            # не проглатывается.
            log.warning("generation_interruption_not_recorded")
            raise
        except (TimeoutError, Exception):
            # Не удалось вовремя: задачу завершит expire_stale.
            log.warning("generation_interruption_not_recorded")

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

    # Исход считается после записи в БД: проигранный переход — «discarded», а не
    # failed; если записать не удалось, исход не считается вовсе.
    async def fail(
        self,
        task_id: uuid.UUID,
        reason: FailureReason,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
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
            self.metrics.outcomes.labels("failed", reason.value).inc()
            log.warning("generation_failed", reason=reason.value)
        else:
            self.metrics.outcomes.labels("discarded", reason.value).inc()
            log.warning("generation_failure_discarded", reason=reason.value)
