import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from opentelemetry.trace import Tracer

from receipes.config import Settings
from receipes.domain.errors import (
    GenerationError,
    InvalidNeurocoreResponse,
    NeurocoreError,
    NeurocoreModelNotFound,
    NeurocoreTimeout,
    NeurocoreTruncated,
    NeurocoreUnavailable,
)
from receipes.domain.menu import MenuPayload, parse_menu
from receipes.generation.metrics import GenerationMetrics

# Схема ответа дословно по API-3 (SMAI-A-131); одна и для v1, и для v2 промпта.
RESPONSE_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipes": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "number"},
                                "unit": {"type": "string"},
                            },
                            "required": ["name", "quantity", "unit"],
                        },
                    },
                    "calories": {"type": "number"},
                    "nutrition": {
                        "type": "object",
                        "properties": {
                            "calories": {"type": "number"},
                            "proteins": {"type": "number"},
                            "fats": {"type": "number"},
                            "carbohydrates": {"type": "number"},
                        },
                        "required": ["calories", "proteins", "fats", "carbohydrates"],
                    },
                    "cookingTime": {"type": "number"},
                    "instructions": {"type": "array", "items": {"type": "string"}},
                    "photo": {"type": ["string", "null"]},
                },
                "required": [
                    "name",
                    "ingredients",
                    "calories",
                    "nutrition",
                    "cookingTime",
                    "instructions",
                ],
            },
        }
    },
    "required": ["recipes"],
}


@dataclass(frozen=True)
class GeneratedMenu:
    menu: MenuPayload
    prompt_eval_count: int | None
    eval_count: int | None


def _count(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Клиент API-3: POST /api/generate. Один экземпляр httpx.AsyncClient на приложение.
class OllamaClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        metrics: GenerationMetrics,
        tracer: Tracer,
    ) -> None:
        self._settings = settings
        self._http = http
        self._metrics = metrics
        self._tracer = tracer

    # httpx считает read-таймаут на каждое чтение, а не на вызов, поэтому общий
    # потолок задаётся отдельно (asyncio.timeout).
    @staticmethod
    def build_http_client(
        settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=settings.ollama_connect_timeout_seconds,
            read=settings.ollama_timeout_seconds,
            write=settings.ollama_timeout_seconds,
            pool=settings.ollama_connect_timeout_seconds,
        )
        return httpx.AsyncClient(
            base_url=str(settings.ollama_base_url), timeout=timeout, transport=transport
        )

    def _request_body(self, prompt: str) -> dict[str, Any]:
        settings = self._settings
        return {
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": RESPONSE_FORMAT,
            "options": {
                "num_ctx": settings.ollama_num_ctx,
                "num_predict": settings.ollama_num_predict,
                "temperature": settings.ollama_temperature,
            },
            "keep_alive": settings.ollama_keep_alive,
        }

    # Запрос к Ollama; ошибки приводятся к безопасным текстам GenerationError.
    async def _post(self, prompt: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._settings.ollama_timeout_seconds):
                response = await self._http.post(
                    "/api/generate", json=self._request_body(prompt)
                )
        except (TimeoutError, httpx.ReadTimeout, httpx.WriteTimeout):
            raise NeurocoreTimeout from None
        except httpx.TransportError:
            # Нет соединения, обрыв, таймаут подключения: нейроядро недоступно.
            raise NeurocoreUnavailable from None
        status = response.status_code
        if status == 503:
            raise NeurocoreUnavailable
        if status == 404:
            raise NeurocoreModelNotFound
        if status != 200:
            raise NeurocoreError
        try:
            body = response.json()
        except ValueError:
            raise InvalidNeurocoreResponse("schema") from None
        if not isinstance(body, dict):
            raise InvalidNeurocoreResponse("schema")
        return body

    # Один вызов: сначала done_reason (обрезка без разбора), затем JSON и «ворота».
    async def generate_menu(self, prompt: str) -> GeneratedMenu:
        started = perf_counter()
        outcome = "error"
        with self._tracer.start_as_current_span("ollama.generate"):
            try:
                body = await self._post(prompt)
                try:
                    if body.get("done_reason") == "length":
                        outcome = "truncated"
                        raise NeurocoreTruncated
                    menu = self._parse(body)
                except GenerationError as error:
                    # Счётчики нужны и при отказе (при обрезке eval_count == лимит).
                    error.prompt_eval_count = _count(body, "prompt_eval_count")
                    error.eval_count = _count(body, "eval_count")
                    raise
                outcome = "success"
            except NeurocoreTimeout:
                outcome = "timeout"
                raise
            except NeurocoreUnavailable:
                outcome = "unavailable"
                raise
            except InvalidNeurocoreResponse:
                outcome = "invalid_response"
                raise
            finally:
                self._metrics.ollama_requests.labels(outcome).inc()
                self._metrics.ollama_duration.observe(perf_counter() - started)
        return GeneratedMenu(
            menu=menu,
            prompt_eval_count=_count(body, "prompt_eval_count"),
            eval_count=_count(body, "eval_count"),
        )

    @staticmethod
    def _parse(body: dict[str, Any]) -> MenuPayload:
        text = body.get("response")
        if not isinstance(text, str):
            raise InvalidNeurocoreResponse("schema")
        try:
            data = json.loads(text)
        except ValueError:
            raise InvalidNeurocoreResponse("schema") from None
        return parse_menu(data)

    async def aclose(self) -> None:
        await self._http.aclose()
