import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
    ClaimedTask,
    FinishedTask,
    MenuRepository,
    PromptTemplateRepository,
    RecipeLikeRepository,
    ReplaceContext,
    TaskRepository,
)
from receipes.dictionaries import DictionarySource
from receipes.domain.errors import (
    GENERATION_FAILED,
    REPLACEMENT_TIMED_OUT,
    SERVICE_RESTARTED,
    DictionariesUnavailable,
    DictionaryError,
    GenerationError,
    InvalidNeurocoreResponse,
    NeurocoreError,
    NeurocoreModelNotFound,
    NeurocoreTimeout,
    NeurocoreUnavailable,
    PromptTemplateError,
    TaskStateConflict,
    UnknownDictionaryId,
)
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskKind
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.ollama import GeneratedMenu, OllamaClient
from receipes.generation.prompt import (
    build_constraints,
    needs_dictionaries,
    render_prompt,
    validate_template,
)

logger = structlog.get_logger()

Sleep = Callable[[float], Awaitable[None]]

# Ключ pg_try_advisory_lock (на уровне БД): один активный воркер на базу.
ADVISORY_LOCK_KEY = 7_460_520_004

# Пауза между попытками взять занятый замок (режим standby), не интервал опроса.
LOCK_RETRY_SECONDS = 10.0

# Пауза после сбоя итерации или записи failed: 1 -> 2 -> 4 -> ... -> 30 с.
DB_BACKOFF_MAX_SECONDS = 30.0

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
        dictionaries: DictionarySource,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._sessions = session_factory
        self._engine = engine
        self._lock: AsyncConnection | None = None
        self._lock_busy_reported = False
        self._ollama = ollama
        self._settings = settings
        self._metrics = metrics
        self._dictionaries = dictionaries
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
                    "failed", task.created_at, task.finished_at, kind=task.kind
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
        exponent = min(self._iteration_failures - 1, 5)
        return min(2.0**exponent, DB_BACKOFF_MAX_SECONDS)

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
        menu_enabled = await self._template_usable()
        if self._ollama_down:
            ready = await self._ollama.is_available(require_model=self._model_missing)
            if not ready:
                self._ollama_failed()
                return False
            self._ollama_probe_passed()
        claim = None
        try:
            async with self._sessions() as session, session.begin():
                claim = await TaskRepository(session).claim_next(
                    menu_enabled=menu_enabled
                )
        except Exception:
            if claim is None:
                raise
            # Захват мог сохраниться, даже если БД не подтвердила commit.
            logger.error(
                "generation_task_claim_not_confirmed",
                task_id=str(claim.task_id),
                exc_info=True,
            )
            await self._fail(claim, GENERATION_FAILED, perf_counter(), None)
            raise
        if claim is None:
            return False
        task_id, claimed_at, kind = claim
        started = perf_counter()
        logger.info("generation_task_claimed", task_id=str(task_id), kind=kind)
        generated: GeneratedMenu | None = None
        try:
            if kind == TaskKind.REPLACE:
                generated = await self._generate_replace(task_id)
            else:
                prompt = await self._prepare_prompt(task_id)
                generated = await self._generate(task_id, prompt, kind)
            finished = await self._finish(task_id, generated, kind)
        except TaskStateConflict:
            if kind == TaskKind.REPLACE:
                await self._fail(claim, GENERATION_FAILED, started, generated)
            else:
                logger.warning("generation_task_conflict", task_id=str(task_id))
        except GenerationError as error:
            await self._fail(claim, error.message, started, generated, error)
            if isinstance(error, OLLAMA_SIDE_ERRORS):
                # Упавшая задача остаётся failed (LE-6), остальные ждут в pending.
                # False: следующая итерация начнётся с паузы, а не сразу с пробы.
                self._ollama_failed(
                    model_missing=isinstance(error, NeurocoreModelNotFound)
                )
                return False
        except Exception:
            logger.error("generation_task_error", task_id=str(task_id), exc_info=True)
            await self._fail(claim, GENERATION_FAILED, started, generated)
        else:
            self._ollama_recovered()
            self._metrics.observe_finished(
                "done", finished.created_at, finished.finished_at, claimed_at, kind=kind
            )
            self._record(claim, "done", started, generated)
        return True

    # Транзакция 2: готовый промпт или параметры задачи, активный шаблон и лайки.
    # Запрос к MS-2 идёт вне транзакции БД. Собранный промпт сохраняется отдельно.
    async def _prepare_prompt(
        self, task_id: UUID, context: ReplaceContext | None = None
    ) -> str:
        kind = TaskKind.MENU if context is None else TaskKind.REPLACE
        async with self._sessions() as session, session.begin():
            tasks = TaskRepository(session)
            # Промпт, сохранённый при создании задачи, не пересобираем.
            saved = await tasks.get_prompt(task_id)
            if saved is not None:
                return saved
            parameters = await tasks.get_parameters(task_id)
            template = await PromptTemplateRepository(session).get_active(kind)
            if parameters is None or template is None:
                raise PromptTemplateError
            params = parameters.to_params()
            template_text = template.template
            validate_template(template_text, kind)
            liked_names = await RecipeLikeRepository(session).recent_names(
                parameters.user_id, self._settings.likes_in_prompt_limit
            )
        constraints = await self._constraints(task_id, params)
        prompt = render_prompt(
            template_text,
            params.people_count,
            params.weekly_budget,
            constraints,
            replaced_recipe=context.replaced_name if context else None,
            existing_recipes=context.recipe_names if context else (),
            liked_recipe_names=liked_names,
        )
        async with self._sessions() as session, session.begin():
            await TaskRepository(session).set_prompt(task_id, prompt)
        return prompt

    # Задача без ID MS-2 не вызывает. Недоступность справочников или ID, которого в
    # них больше нет, фатальны для задачи: повтора нет (LE-6), в логе только причина.
    async def _constraints(self, task_id: UUID, params: GenerationParams) -> str:
        if not needs_dictionaries(params):
            return ""
        try:
            return build_constraints(params, await self._dictionaries.fetch())
        except DictionaryError:
            reason = "unavailable"
        except UnknownDictionaryId:
            reason = "unknown_id"
        logger.warning(
            "generation_dictionaries_failed", task_id=str(task_id), reason=reason
        )
        raise DictionariesUnavailable

    async def _replace_context(self, task_id: UUID) -> ReplaceContext:
        async with self._sessions() as session:
            context = await TaskRepository(session).get_replace_context(
                task_id, self._settings.replace_wait_seconds
            )
        if context is None:
            raise TaskStateConflict
        return context

    async def _generate_replace(self, task_id: UUID) -> GeneratedMenu:
        context = await self._replace_context(task_id)
        budget = asyncio.timeout(context.remaining_seconds)
        try:
            async with budget:
                prompt = await self._prepare_prompt(task_id, context)
                return await self._generate(task_id, prompt, TaskKind.REPLACE)
        except TimeoutError:
            if budget.expired():
                raise TaskStateConflict from None
            raise

    # Вызов Ollama вне транзакций; повтор только для невалидного ответа.
    async def _generate(
        self, task_id: UUID, prompt: str, kind: TaskKind
    ) -> GeneratedMenu:
        attempts = 1 + self._settings.generation_bad_response_retries
        for attempt in range(1, attempts + 1):
            try:
                if kind == TaskKind.REPLACE:
                    context = await self._replace_context(task_id)
                    generated = await self._ollama.generate_menu(
                        prompt,
                        kind=kind,
                        existing_names=(*context.recipe_names, context.replaced_name),
                    )
                else:
                    generated = await self._ollama.generate_menu(prompt)
            except InvalidNeurocoreResponse as error:
                self._metrics.rejected.labels(kind, error.reason).inc()
                logger.warning(
                    "generation_response_rejected",
                    task_id=str(task_id),
                    kind=kind,
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
    async def _finish(
        self, task_id: UUID, generated: GeneratedMenu, kind: TaskKind
    ) -> FinishedTask:
        async with self._sessions() as session, session.begin():
            tasks = TaskRepository(session)
            if kind == TaskKind.REPLACE:
                finished = await tasks.finish_replace(
                    task_id,
                    generated.menu.recipes[0],
                    self._settings.replace_wait_seconds,
                )
            else:
                menu_id = await MenuRepository(session).save(generated.menu)
                finished = await tasks.mark_done(task_id, menu_id)
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
        claim: ClaimedTask,
        message: str,
        started: float,
        generated: GeneratedMenu | None,
        error: GenerationError | None = None,
    ) -> None:
        task_id, claimed_at, kind = claim
        delay = 1.0
        while True:
            try:
                async with self._sessions() as session, session.begin():
                    tasks = TaskRepository(session)
                    finished = None
                    if kind == TaskKind.REPLACE:
                        finished = await tasks.expire_replace(
                            task_id,
                            self._settings.replace_wait_seconds,
                            REPLACEMENT_TIMED_OUT,
                        )
                    expired = finished is not None
                    if not expired:
                        finished = await tasks.mark_failed(task_id, message)
            except Exception as write_error:
                logger.error(
                    "generation_task_fail_not_saved",
                    task_id=str(task_id),
                    retry_seconds=delay,
                    error_type=type(write_error).__name__,
                    exc_info=delay == 1.0,
                )
                await self._sleep(delay)
                delay = min(delay * 2, DB_BACKOFF_MAX_SECONDS)
            else:
                break
        if finished is not None:
            self._metrics.observe_finished(
                "failed",
                finished.created_at,
                finished.finished_at,
                finished.claimed_at if expired else claimed_at,
                kind=kind,
            )
            self._record(
                claim,
                "failed",
                started,
                generated,
                error=REPLACEMENT_TIMED_OUT if expired else message,
                counts=error,
            )
        else:
            logger.warning("generation_task_conflict", task_id=str(task_id))

    def _record(
        self,
        claim: ClaimedTask,
        status: str,
        started: float,
        generated: GeneratedMenu | None,
        error: str | None = None,
        counts: GenerationError | None = None,
    ) -> None:
        duration = perf_counter() - started
        fields: dict[str, object] = {
            "task_id": str(claim.task_id),
            "kind": claim.kind,
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
