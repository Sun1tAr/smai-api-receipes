from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from receipes.domain import GenerationTaskNotFound, InvalidGenerationParameters

logger = structlog.get_logger()


class ErrorBody(BaseModel):
    code: str = Field(description="Машинно-читаемый код ошибки.")
    message: str = Field(
        description="Описание ошибки на английском, безопасное для показа."
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Дополнительные сведения; может быть пустым. Для `VALIDATION_ERROR` — "
            '`{"fields": [{"loc": ["body", "name"], "type": "missing"}]}`: '
            "`loc` — список строк с местом ошибки, `type` — тип ошибки Pydantic; "
            "значения и тексты исключений не возвращаются."
        ),
    )


# Единый формат ошибок всех эндпоинтов сервиса.
class ErrorResponse(BaseModel):
    error: ErrorBody

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                    "details": {},
                }
            }
        }
    }


# Описание ошибок для OpenAPI: единый формат и пример на каждый код.
def error_doc(description: str, code: str, message: str, details: dict | None = None):
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message": message,
                        "details": details or {},
                    }
                }
            }
        },
    }


HTTP_ERRORS = {
    404: ("NOT_FOUND", "Resource not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(body, status_code=status_code, headers=headers)


# Контракт gateway: loc — список строк. Для битого JSON loc несёт позицию символа,
# а не поле, поэтому остаётся только ["body"].
# Имя лишнего поля приходит от клиента, поэтому каждый элемент усекается: ключ в
# несколько мегабайт не должен раздувать ответ.
LOC_PART_MAX_LENGTH = 64
TRUNCATION_MARK = "…"


def truncate_loc_part(part: str) -> str:
    if len(part) <= LOC_PART_MAX_LENGTH:
        return part
    return part[:LOC_PART_MAX_LENGTH] + TRUNCATION_MARK


def error_location(item: Any) -> list[str]:
    location = item["loc"]
    if item["type"] == "json_invalid":
        location = [part for part in location if not isinstance(part, int)]
    return [truncate_loc_part(str(part)) for part in location]


# Только место и тип ошибки: текст исключений и введённые значения могут содержать
# пользовательские данные, поэтому не возвращаются и не логируются.
async def validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    fields = [
        {"loc": error_location(item), "type": item["type"]} for item in error.errors()
    ]
    return error_response(
        400, "VALIDATION_ERROR", "Request validation failed", {"fields": fields}
    )


async def http_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    if error.status_code == 400:
        # Тело не удалось разобрать (в том числе слишком глубокая вложенность JSON,
        # на которой падает парсер): та же ошибка валидации, что и для битого JSON.
        fields = [{"loc": ["body"], "type": "json_invalid"}]
        return error_response(
            400, "VALIDATION_ERROR", "Request validation failed", {"fields": fields}
        )
    code, message = HTTP_ERRORS.get(
        error.status_code,
        ("HTTP_ERROR", "Request failed")
        if error.status_code < 500
        else ("INTERNAL_ERROR", "Internal server error"),
    )
    return error_response(
        error.status_code, code, message, headers=dict(error.headers or {})
    )


# Запасной путь для ошибок вне middleware логирования; стек пишется без текста.
async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
    logger.error("unhandled_error", exc_info=error)
    return error_response(500, "INTERNAL_ERROR", "Internal server error")


# Параметры, не прошедшие доменную проверку, — тот же 400, что и ошибка схемы.
async def invalid_parameters_handler(
    request: Request, error: Exception
) -> JSONResponse:
    assert isinstance(error, InvalidGenerationParameters)
    fields = [{"loc": ["body", to_camel(error.field)], "type": "value_error"}]
    return error_response(
        400, "VALIDATION_ERROR", "Request validation failed", {"fields": fields}
    )


async def task_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    return error_response(404, "NOT_FOUND", "Generation task not found")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(InvalidGenerationParameters, invalid_parameters_handler)
    app.add_exception_handler(GenerationTaskNotFound, task_not_found_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
