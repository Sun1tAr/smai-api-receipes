import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import async_sessionmaker

from receipes.config import Settings
from receipes.database import create_engine
from receipes.errors import register_error_handlers
from receipes.health import dependencies_ready, router
from receipes.observability import configure_logging, create_tracer_provider

logger = structlog.get_logger()

API_DESCRIPTION = (
    "Сервис рецептов SmartMeal (MS-4): ядро домена рецептов и генерация меню "
    "через Ollama.\n\n"
    "Все ошибки возвращаются в едином формате "
    '`{"error": {"code": "...", "message": "...", "details": {}}}`.'
)

# Входящий X-Request-ID принимается только в безопасном виде (в логи и заголовки).
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")


def resolve_request_id(incoming: str | None) -> str:
    if incoming is not None and REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return str(uuid4())


# Фабрика позволяет задавать отдельное окружение при интеграционном тестировании.
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    provider = create_tracer_provider(settings)
    # Единый реестр: сюда же позже добавятся доменные метрики.
    metrics_registry = CollectorRegistry()

    # Владение соединениями и экспортёром ограничено жизненным циклом приложения.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        instrumentor = SQLAlchemyInstrumentor()
        instrumentor.instrument(engine=engine.sync_engine, tracer_provider=provider)
        try:
            if not await dependencies_ready(engine, settings):
                raise RuntimeError("Service dependencies are unavailable")
            logger.info("service_started")
            yield
        finally:
            await engine.dispose()
            instrumentor.uninstrument()
            provider.shutdown()
            logger.info("service_stopped")

    app = FastAPI(
        title="SmartMeal recipes service",
        version="0.1.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.tracer_provider = provider
    app.state.metrics_registry = metrics_registry
    app.include_router(router)

    # Request ID относится к текущему HTTP-вызову; query и тела не логируются.
    @app.middleware("http")
    async def request_logging(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        structlog.contextvars.clear_contextvars()
        request_id = resolve_request_id(request.headers.get("X-Request-ID"))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            route = request.scope.get("route")
            logger.info(
                "http_request",
                method=request.method,
                path=route.path if route else "unmatched",
                status_code=response.status_code,
                duration_seconds=round(perf_counter() - started, 6),
            )
            return response
        except Exception as error:
            logger.error("http_request_failed", error_type=type(error).__name__)
            raise
        finally:
            structlog.contextvars.clear_contextvars()

    Instrumentator(
        registry=metrics_registry,
        should_group_status_codes=False,
        should_ignore_untemplated=True,
    ).instrument(app).expose(app, include_in_schema=False)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return app
