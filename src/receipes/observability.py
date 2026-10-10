import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from receipes.config import Settings

SERVICE_NAME = "smai-api-receipes"


# Связываем JSON-логи с активным трейсом без логирования содержимого запросов.
def add_context(
    logger: Any,
    method_name: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    context = trace.get_current_span().get_span_context()
    event["service"] = SERVICE_NAME
    event["trace_id"] = f"{context.trace_id:032x}" if context.is_valid else None
    event.setdefault("request_id", None)
    return event


# Одинаковый формат применяется к structlog и стандартным логгерам библиотек.
def configure_logging(level: str) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_context,
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.EventRenamer("message"),
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(handlers=[handler], level=level, force=True)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
        server_logger.setLevel(level)
    # HTTP-события пишет middleware без query; access log мог бы раскрыть query.
    logging.getLogger("uvicorn.access").disabled = True
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# Без коллектора сохраняем trace_id в логах, не делая внешних запросов.
def create_tracer_provider(settings: Settings) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if settings.otel_exporter_otlp_endpoint is not None:
        endpoint = str(settings.otel_exporter_otlp_endpoint).rstrip("/") + "/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint,
                    timeout=5,
                )
            )
        )
    return provider
