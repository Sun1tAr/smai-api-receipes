import asyncio
import json
from enum import StrEnum
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from receipes.config import Settings
from receipes.generated_menu import GeneratedMenu, ollama_format_schema

logger = structlog.get_logger()


class FailureReason(StrEnum):
    """Исход неудачной генерации; значения — метки метрик и не содержат текста."""

    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    TRUNCATED = "truncated"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL = "internal"
    # Воркер остановлен во время вызова (деплой, перезапуск).
    INTERRUPTED = "interrupted"


# Короткие безопасные тексты: попадают в generation_task.error и в ответ API-2, поэтому
# в них нет ни ответа модели, ни деталей транспорта.
SAFE_MESSAGES = {
    FailureReason.UNAVAILABLE: "Ollama is unavailable",
    FailureReason.TIMEOUT: "Ollama request timed out",
    FailureReason.HTTP_ERROR: "Ollama returned an error",
    FailureReason.TRUNCATED: "Model response was cut off (token limit reached)",
    FailureReason.INVALID_RESPONSE: "Model returned an invalid menu",
    FailureReason.INTERNAL: "Menu generation failed",
    FailureReason.INTERRUPTED: "Generation interrupted",
}


class GenerationFailure(Exception):
    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.message = SAFE_MESSAGES[reason]

    # Повторяется только невалидный ответ модели: недоступность и таймаут — нет.
    @property
    def retryable(self) -> bool:
        return self.reason is FailureReason.INVALID_RESPONSE


class ResponseTruncated(Exception):
    """Генерация оборвана по лимиту токенов (done_reason == "length")."""


def build_request(
    prompt: str,
    model: str,
    *,
    num_ctx: int,
    num_predict: int,
    temperature: float,
    keep_alive: str,
) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": prompt,
        "format": ollama_format_schema(),
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }


# Ответ Ollama → проверенное «воротами» меню; ошибки формата поднимаются наверх.
# Обрыв по num_predict даёт невалидный JSON, поэтому он определяется заранее.
def parse_response(body: dict[str, Any]) -> GeneratedMenu:
    if body.get("done_reason") == "length":
        raise ResponseTruncated
    return GeneratedMenu.model_validate(json.loads(body["response"]))


# Клиент POST /api/generate. Вызывается вне транзакций БД; ответ модели и промпт
# не логируются.
class OllamaClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.total_timeout = settings.ollama_timeout_seconds
        self.http = httpx.AsyncClient(
            base_url=str(settings.ollama_base_url),
            timeout=httpx.Timeout(
                settings.ollama_timeout_seconds,
                connect=settings.ollama_connect_timeout_seconds,
            ),
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    # Лёгкая проба доступности (GET /api/tags) для circuit breaker: без модели и без
    # промпта. Любой сбой — просто «недоступна».
    async def probe(self) -> bool:
        try:
            async with asyncio.timeout(self.settings.ollama_connect_timeout_seconds):
                response = await self.http.get("/api/tags")
        except (TimeoutError, httpx.HTTPError):
            return False
        return response.status_code < 400

    async def generate_menu(self, prompt: str) -> GeneratedMenu:
        request = build_request(
            prompt,
            self.settings.ollama_model,
            num_ctx=self.settings.ollama_num_ctx,
            num_predict=self.settings.ollama_num_predict,
            temperature=self.settings.ollama_temperature,
            keep_alive=self.settings.ollama_keep_alive,
        )
        body = await self._post(request)
        try:
            return parse_response(body)
        except ResponseTruncated:
            raise GenerationFailure(FailureReason.TRUNCATED) from None
        except (
            ValidationError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,  # json.loads на глубоко вложенном ответе
        ) as error:
            # Только тип ошибки: в сообщениях Pydantic есть фрагменты ответа модели.
            logger.warning("ollama_invalid_response", error_type=type(error).__name__)
            raise GenerationFailure(FailureReason.INVALID_RESPONSE) from None

    # Общий таймаут (OLLAMA_TIMEOUT_SECONDS) ограничивает весь вызов целиком, а
    # таймаут httpx — лишь отдельные операции.
    async def _post(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.total_timeout):
                response = await self.http.post("/api/generate", json=request)
        except TimeoutError:
            raise GenerationFailure(FailureReason.TIMEOUT) from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise GenerationFailure(FailureReason.UNAVAILABLE) from None
        except httpx.TimeoutException:
            raise GenerationFailure(FailureReason.TIMEOUT) from None
        except httpx.HTTPError:
            raise GenerationFailure(FailureReason.UNAVAILABLE) from None
        if response.status_code >= 400:
            logger.warning("ollama_http_error", status_code=response.status_code)
            raise GenerationFailure(FailureReason.HTTP_ERROR)
        try:
            body = response.json()
        except (ValueError, RecursionError):
            raise GenerationFailure(FailureReason.INVALID_RESPONSE) from None
        if not isinstance(body, dict):
            raise GenerationFailure(FailureReason.INVALID_RESPONSE)
        return body
