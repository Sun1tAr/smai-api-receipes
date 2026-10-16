import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from time import perf_counter
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from receipes.config import Settings
from receipes.db.repositories import (
    FinishedTask,
    MenuRepository,
    PromptTemplateRepository,
    TaskRepository,
)
from receipes.domain.errors import (
    GENERATION_FAILED,
    SERVICE_RESTARTED,
    GenerationError,
    InvalidNeurocoreResponse,
    NeurocoreError,
    NeurocoreModelNotFound,
    NeurocoreTimeout,
    NeurocoreUnavailable,
    PromptTemplateError,
    TaskStateConflict,
)
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.ollama import GeneratedMenu, OllamaClient
from receipes.generation.prompt import render_prompt, validate_template

logger = structlog.get_logger()

Sleep = Callable[[float], Awaitable[None]]

# Ключ pg_try_advisory_lock (на уровне БД): один активный воркер на базу.
ADVISORY_LOCK_KEY = 7_460_520_004

# Пауза между попытками взять занятый замок (режим standby), не интервал опроса.
LOCK_RETRY_SECONDS = 10.0

# Пауза после сбоя итерации (обычно БД): 1 -> 2 -> 4 -> ... -> 30 с, сброс при успехе.
ITERATION_BACKOFF_MAX_SECONDS = 30.0

# Ошибки стороны Ollama: после них очередь не разбирается, пока Ollama не вернётся.
OLLAMA_SIDE_ERRORS = (
    NeurocoreUnavailable,
    NeurocoreTimeout,
    NeurocoreModelNotFound,
    NeurocoreError,
)

# Пауза после падения Ollama растёт 5 -> 10 -> 20 -> 40 -> 60 с, пока она не вернётся.
OLLAMA_BACKOFF_START_SECONDS = 5.0
OLLAMA_BACKOFF_MAX_SECONDS = 60.0


# Воркер генерации: берёт pending-задачи по одной (Ollama однопоточна), вызывает
# модель вне транзакций БД и записывает результат. Рассчитан на один процесс и одну
# реплику; перекрытие старого и нового контейнера при деплое не допускается.
class GenerationWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        ollama: OllamaClient,
        settings: Settings,
        metrics: GenerationMetrics,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._sessions = session_factory
        self._engine = engine
        self._lock: AsyncConnection | None = None
        self._lock_busy_reported = False
        self._ollama = ollama
        self._settings = settings
        self._metrics = metrics
        self._sleep = sleep
        self._ollama_down = False
        # После 404 пробой служит наличие модели в /api/tags, а не просто ответ 200.
        self._model_missing = False
        self._backoff = 0.0
        self._iteration_failures = 0
        self._template_problem: str | None = None

    # Задачи, оставшиеся in_progress после гибели процесса, закрываем как failed.
    # Повторяем, пока БД не ответит: брать новые задачи до этого нельзя.
    async def recover(self) -> None:
        while True:
            try:
                async with self._sessions() as session, session.begin():
                    recovered = await TaskRepository(session).fail_in_progress(
                        SERVICE_RESTARTED
                    )
            except Exception:
                logger.error("generation_recovery_failed", exc_info=True)
                await self._sleep(self._settings.generation_worker_interval_seconds)
                continue
            # Начало обработки неизвестно: пишем только полное время задачи.
            for task in recovered:
                self._metrics.observe_finished(
                    "failed", task.created_at, task.finished_at
                )
            if recovered:
                logger.info("generation_recovered", failed_tasks=len(recovered))
            return

    # Замок живёт на отдельном соединении всё время работы воркера (сессионный
    # advisory lock). True, если замок наш; при свежем захвате сначала recover().
    async def _ensure_lock(self) -> bool:
        if self._lock is not None:
            try:
                await self._lock.execute(text("SELECT 1"))
                return True
            except Exception:
                # Соединение потеряно, а с ним и замок: берём заново.
                logger.warning("generation_lock_lost")
                await self._release_lock()
        connection = await self._engine.connect()
        try:
            connection = await connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            got = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
        except BaseException:
            await connection.close()
            raise
        if not got:
            await connection.close()
            if not self._lock_busy_reported:
                self._lock_busy_reported = True
                logger.warning("generation_lock_busy")
            return False
        self._lock = connection
        self._lock_busy_reported = False
        logger.info("generation_lock_acquired")
        await self.recover()
        return True

    async def _release_lock(self) -> None:
        connection, self._lock = self._lock, None
        if connection is None:
            return
        # Соединение вернётся в пул вместе с сессионным замком, поэтому снимаем замок
        # явно; если не вышло, соединение выбрасывается и сервер освободит замок.
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
        except Exception:
            with suppress(Exception):
                await connection.invalidate()
        with suppress(Exception):
            await connection.close()

    # Замок берётся до recover(): иначе второй экземпляр оборвал бы чужие задачи.
    async def run(self) -> None:
        try:
            while True:
                try:
                    if not await self._ensure_lock():
                        self._iteration_failures = 0
                        await self._sleep(LOCK_RETRY_SECONDS)
                        continue
                    processed = await self.process_one()
                except Exception as error:
                    self._log_iteration_failure(error)
                    await self._sleep(self._iteration_delay())
                    continue
                self._iteration_failures = 0
                # Пустая итерация ничего не пишет в лог: иначе шум каждые N секунд.
                if not processed:
                    await self._sleep(self._idle_delay())
        finally:
            await self._release_lock()

    # Сбой итерации (например, БД недоступна) не должен крутиться раз в интервал:
    # пауза растёт до 30 с, а полный traceback пишется только при первом сбое серии.
    def _log_iteration_failure(self, error: Exception) -> None:
        self._iteration_failures += 1
        if self._iteration_failures == 1:
            logger.error("generation_iteration_failed", exc_info=True)
        else:
            logger.error(
                "generation_iteration_failed",
                error_type=type(error).__name__,
                consecutive_failures=self._iteration_failures,
            )

    def _iteration_delay(self) -> float:
        delay = 2.0 ** (self._iteration_failures - 1)
        return min(delay, ITERATION_BACKOFF_MAX_SECONDS)

    # Пока Ollama недоступна, ждём по нарастающей паузе, иначе обычный интервал.
    def _idle_delay(self) -> float:
        if self._ollama_down:
            return self._backoff
        return self._settings.generation_worker_interval_seconds

    def _ollama_failed(self, *, model_missing: bool | None = None) -> None:
        if model_missing is not None:
            self._model_missing = model_missing
        self._backoff = (
            min(self._backoff * 2, OLLAMA_BACKOFF_MAX_SECONDS)
            if self._backoff > 0
            else OLLAMA_BACKOFF_START_SECONDS
        )
        self._ollama_down = True

    # Проба прошла: можно брать задачу, но серия сбоев и пауза не обнуляются. Иначе
    # при «проба 200, генерация 500» очередь горела бы по задаче в 5 секунд.
    def _ollama_probe_passed(self) -> None:
        self._ollama_down = False
        self._model_missing = False

    # Сбросить серию и паузу может только успешная генерация.
    def _ollama_recovered(self) -> None:
        self._ollama_probe_passed()
        self._backoff = 0.0

    # Задачу нельзя брать, пока нечем её обработать: без годного шаблона она бы
    # сгорела в failed. Причина пишется в лог один раз, пока она не изменится.
    async def _template_usable(self) -> bool:
        async with self._sessions() as session:
            template = await PromptTemplateRepository(session).get_active()
        problem: str | None = None
        if template is None:
            problem = "missing"
        else:
            try:
                validate_template(template.template)
            except PromptTemplateError:
                problem = "invalid"
        if problem != self._template_problem:
            self._template_problem = problem
            if problem is not None:
                logger.error("prompt_template_unusable", reason=problem)
        return problem is None

    # Одна итерация: True, если задача была взята (даже закончившаяся ошибкой).
    async def process_one(self) -> bool:
        if not await self._template_usable():
            return False
        if self._ollama_down:
            ready = await self._ollama.is_available(require_model=self._model_missing)
            if not ready:
                self._ollama_failed()
                return False
            self._ollama_probe_passed()
        async with self._sessions() as session, session.begin():
            claim = await TaskRepository(session).claim_next()
        if claim is None:
            return False
        task_id, claimed_at = claim
        started = perf_counter()
        logger.info("generation_task_claimed", task_id=str(task_id))
        generated: GeneratedMenu | None = None
        try:
            prompt = await self._prepare_prompt(task_id)
            generated = await self._generate(task_id, prompt)
            finished = await self._finish(task_id, generated)
        except TaskStateConflict:
            logger.warning("generation_task_conflict", task_id=str(task_id))
            return True
        except GenerationError as error:
            await self._fail(
                task_id, error.message, started, generated, claimed_at, error
            )
            if isinstance(error, OLLAMA_SIDE_ERRORS):
                # Упавшая задача остаётся failed (LE-6), остальные ждут в pending.
                # False: следующая итерация начнётся с паузы, а не сразу с пробы.
                self._ollama_failed(
                    model_missing=isinstance(error, NeurocoreModelNotFound)
                )
                return False
        except Exception:
            logger.error("generation_task_error", task_id=str(task_id), exc_info=True)
            await self._fail(task_id, GENERATION_FAILED, started, generated, claimed_at)
        else:
            self._ollama_recovered()
            self._metrics.observe_finished(
                "done", finished.created_at, finished.finished_at, claimed_at
            )
            self._record(task_id, "done", started, generated)
        return True

    # Транзакция 2: готовый промпт задачи или (для задач без него) параметры и активный
    # шаблон; собранный промпт сохраняется в задаче.
    async def _prepare_prompt(self, task_id: UUID) -> str:
        async with self._sessions() as session, session.begin():
            tasks = TaskRepository(session)
            # Промпт, сохранённый при создании задачи, не пересобираем.
            saved = await tasks.get_prompt(task_id)
            if saved is not None:
                return saved
            parameters = await tasks.get_parameters(task_id)
            template = await PromptTemplateRepository(session).get_active()
            if parameters is None or template is None:
                raise PromptTemplateError
            prompt = render_prompt(
                template.template, parameters.people_count, parameters.weekly_budget
            )
            await tasks.set_prompt(task_id, prompt)
        return prompt

    # Вызов Ollama вне транзакций; повтор только для невалидного ответа.
    async def _generate(self, task_id: UUID, prompt: str) -> GeneratedMenu:
        attempts = 1 + self._settings.generation_bad_response_retries
        for attempt in range(1, attempts + 1):
            try:
                generated = await self._ollama.generate_menu(prompt)
            except InvalidNeurocoreResponse as error:
                self._metrics.rejected.labels(error.reason).inc()
                logger.warning(
                    "generation_response_rejected",
                    task_id=str(task_id),
                    attempt=attempt,
                    reason=error.reason,
                )
                if attempt == attempts:
                    raise
                continue
            self._warn_if_context_is_tight(task_id, generated)
            return generated
        raise AssertionError("unreachable")  # pragma: no cover

    def _warn_if_context_is_tight(
        self, task_id: UUID, generated: GeneratedMenu
    ) -> None:
        used = generated.prompt_eval_count
        settings = self._settings
        if (
            used is not None
            and used + settings.ollama_num_predict > settings.ollama_num_ctx
        ):
            logger.warning(
                "ollama_context_tight",
                task_id=str(task_id),
                prompt_eval_count=used,
                num_predict=settings.ollama_num_predict,
                num_ctx=settings.ollama_num_ctx,
            )

    # Транзакция 3: меню и статус done вместе; иначе откат всего.
    async def _finish(self, task_id: UUID, generated: GeneratedMenu) -> FinishedTask:
        async with self._sessions() as session, session.begin():
            menu_id = await MenuRepository(session).save(generated.menu)
            finished = await TaskRepository(session).mark_done(task_id, menu_id)
            if finished is None:
                raise TaskStateConflict
        mismatched = sum(
            item.calories != item.nutrition.calories for item in generated.menu.recipes
        )
        if mismatched:
            logger.warning(
                "generation_calories_mismatch",
                task_id=str(task_id),
                recipes=len(generated.menu.recipes),
                mismatched=mismatched,
            )
        return finished

    async def _fail(
        self,
        task_id: UUID,
        message: str,
        started: float,
        generated: GeneratedMenu | None,
        claimed_at: datetime,
        error: GenerationError | None = None,
    ) -> None:
        try:
            async with self._sessions() as session, session.begin():
                finished = await TaskRepository(session).mark_failed(task_id, message)
        except Exception:
            # Задачу подберёт восстановление при следующем старте.
            logger.error(
                "generation_task_fail_not_saved", task_id=str(task_id), exc_info=True
            )
            return
        if finished is not None:
            self._metrics.observe_finished(
                "failed", finished.created_at, finished.finished_at, claimed_at
            )
            self._record(
                task_id,
                "failed",
                started,
                generated,
                error=message,
                counts=error,
            )
        else:
            logger.warning("generation_task_conflict", task_id=str(task_id))

    def _record(
        self,
        task_id: UUID,
        status: str,
        started: float,
        generated: GeneratedMenu | None,
        error: str | None = None,
        counts: GenerationError | None = None,
    ) -> None:
        duration = perf_counter() - started
        self._metrics.tasks.labels(status).inc()
        self._metrics.duration.observe(duration)
        fields: dict[str, object] = {
            "task_id": str(task_id),
            "status": status,
            "duration_seconds": round(duration, 3),
        }
        if generated is not None:
            fields.update(
                recipes=len(generated.menu.recipes),
                prompt_eval_count=generated.prompt_eval_count,
                eval_count=generated.eval_count,
            )
        if counts is not None and counts.eval_count is not None:
            fields.update(
                prompt_eval_count=counts.prompt_eval_count,
                eval_count=counts.eval_count,
            )
        if error is not None:
            fields["error"] = error
        log = logger.info if status == "done" else logger.warning
        log(f"generation_task_{status}", **fields)
