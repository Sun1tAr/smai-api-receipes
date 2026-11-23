from datetime import datetime, timedelta

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from receipes.domain.status import TaskKind

logger = structlog.get_logger()

# Генерация идёт от секунд до десятков минут в худшем случае: свои границы.
DURATION_BUCKETS = (5, 10, 20, 30, 60, 120, 300, 600)
# Очередь Ollama последовательна, таймаут вызова 360 с (SMAI-95).
TASK_BUCKETS = (
    5,
    10,
    20,
    30,
    60,
    90,
    120,
    180,
    240,
    300,
    360,
    480,
    600,
    900,
    1200,
    1800,
    3600,
)
TASK_STATUSES = ("done", "failed")
# Все статусы LE-6; возраст самой старой задачи имеет смысл только у незавершённых.
QUEUE_STATUSES = ("pending", "in_progress", "done", "failed")
AGE_STATUSES = ("pending", "in_progress")
OLLAMA_OUTCOMES = (
    "success",
    "timeout",
    "unavailable",
    "invalid_response",
    "truncated",
    "error",
)
REJECT_REASONS = (
    "schema",
    "count",
    "duplicate_name",
    "duplicate_ingredient",
    "ingredient_name",
    "bounds",
    "text",
)


# Метрики генерации живут в реестре приложения (отдаются через /metrics).
class GenerationMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.tasks = Counter(
            "menu_generation_tasks",
            "Finished generation tasks by kind and final status.",
            ["kind", "status"],
            registry=registry,
        )
        self.duration = Histogram(
            "menu_generation_duration_seconds",
            "Time from task claim to its final status.",
            ["kind"],
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self.ollama_duration = Histogram(
            "ollama_request_duration_seconds",
            "Duration of the Ollama generate call.",
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self.ollama_requests = Counter(
            "ollama_requests",
            "Ollama generate calls by outcome.",
            ["outcome"],
            registry=registry,
        )
        self.rejected = Counter(
            "menu_generation_rejected",
            "Model responses rejected by validation, by reason.",
            ["kind", "reason"],
            registry=registry,
        )
        self.queue_tasks = Gauge(
            "recipes_generation_tasks",
            "Generation tasks by kind and status, refreshed in the background.",
            ["kind", "status"],
            registry=registry,
        )
        self.oldest_age = Gauge(
            "recipes_generation_task_oldest_age_seconds",
            "Age of the oldest task in the status, 0 if there are none.",
            ["kind", "status"],
            registry=registry,
        )
        self.task_duration = Histogram(
            "recipes_generation_task_duration_seconds",
            "Task creation to its terminal status, including the queue wait.",
            ["kind", "status"],
            buckets=TASK_BUCKETS,
            registry=registry,
        )
        self.task_processing = Histogram(
            "recipes_generation_task_processing_seconds",
            "Time the task spent in in_progress.",
            ["kind", "status"],
            buckets=TASK_BUCKETS,
            registry=registry,
        )
        self.refresh_timestamp = Gauge(
            "recipes_generation_tasks_refresh_timestamp_seconds",
            "Unix time of the last successful refresh of the queue gauges.",
            registry=registry,
        )
        for kind in TaskKind:
            for status in QUEUE_STATUSES:
                self.queue_tasks.labels(kind, status).set(0)
            for status in AGE_STATUSES:
                self.oldest_age.labels(kind, status).set(0)
            for status in TASK_STATUSES:
                self.task_duration.labels(kind, status)
                self.task_processing.labels(kind, status)
                self.tasks.labels(kind, status)
            self.duration.labels(kind)
            for reason in REJECT_REASONS:
                self.rejected.labels(kind, reason)
        self.refresh_timestamp.set(0)
        for outcome in OLLAMA_OUTCOMES:
            self.ollama_requests.labels(outcome)

    # Вызывать только после commit терминального перехода: ровно один раз на задачу.
    # processing не пишется, если начало обработки неизвестно (восстановление).
    def observe_finished(
        self,
        status: str,
        created_at: datetime,
        finished_at: datetime,
        claimed_at: datetime | None = None,
        *,
        kind: TaskKind,
    ) -> None:
        self.tasks.labels(kind, status).inc()
        self.task_duration.labels(kind, status).observe(
            self._seconds(finished_at - created_at)
        )
        if claimed_at is not None:
            processing = self._seconds(finished_at - claimed_at)
            self.task_processing.labels(kind, status).observe(processing)
            self.duration.labels(kind).observe(processing)

    # created_at пишет приложение, остальное БД: расхождение часов не должно давать
    # отрицательных наблюдений, но и теряться молча не должно.
    @staticmethod
    def _seconds(delta: timedelta) -> float:
        seconds = delta.total_seconds()
        if seconds < 0:
            logger.debug("generation_metrics_negative_duration", seconds=seconds)
            return 0.0
        return seconds
