import asyncio
import time
from collections.abc import Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.domain.status import TaskKind
from receipes.generation.metrics import AGE_STATUSES, QUEUE_STATUSES, GenerationMetrics

logger = structlog.get_logger()

# Таймаут одного обновления: «висящая» БД не должна останавливать цикл.
MAX_REFRESH_TIMEOUT_SECONDS = 5.0

# Один запрос на тик, возраст считает БД (часы приложения не участвуют).
COUNTS_QUERY = text(
    "SELECT kind, status, count(*), "
    "EXTRACT(EPOCH FROM now() - min(status_changed_at)) "
    "FROM generation_task GROUP BY kind, status"
)


# Gauge очереди генерации обновляются из БД раз в интервал, а не на каждый scrape.
# При сбое остаются прошлые значения, refresh_timestamp не двигается.
class QueueMetricsRefresher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        metrics: GenerationMetrics,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sessions = session_factory
        self._metrics = metrics
        self._interval = interval_seconds
        self._timeout = min(MAX_REFRESH_TIMEOUT_SECONDS, interval_seconds)
        self._clock = clock
        self._failing = False

    async def _fetch(self) -> dict[tuple[str, str], tuple[int, float]]:
        async with self._sessions() as session:
            rows = (await session.execute(COUNTS_QUERY)).all()
        return {
            (kind, status): (count, float(age)) for kind, status, count, age in rows
        }

    async def refresh_once(self) -> None:
        try:
            counts = await asyncio.wait_for(self._fetch(), self._timeout)
        except Exception as error:
            self._report_failure(error)
            return
        for kind in TaskKind:
            for status in QUEUE_STATUSES:
                count, age = counts.get((kind, status), (0, 0.0))
                self._metrics.queue_tasks.labels(kind, status).set(count)
                if status in AGE_STATUSES:
                    self._metrics.oldest_age.labels(kind, status).set(max(age, 0.0))
        self._metrics.refresh_timestamp.set(self._clock())
        if self._failing:
            self._failing = False
            logger.info("generation_metrics_refresh_recovered")

    # WARNING один раз при переходе ok -> fail; повторные сбои идут на DEBUG.
    # Тип ошибки без текста: сообщения драйвера могут содержать SQL и адреса.
    def _report_failure(self, error: Exception) -> None:
        log = logger.debug if self._failing else logger.warning
        self._failing = True
        log("generation_metrics_refresh_failed", error_type=type(error).__name__)

    async def run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._interval)
