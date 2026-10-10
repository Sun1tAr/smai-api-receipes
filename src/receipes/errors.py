from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger()


class ErrorBody(BaseModel):
    code: str = Field(description="Машинно-читаемый код ошибки.")
    message: str = Field(
        description="Описание ошибки на английском, безопасное для показа."
    )
    details: dict[str, Any] = Field(
        default_factory=dict, description="Дополнительные сведения; может быть пустым."
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


# Только место и тип ошибки: текст исключений и введённые значения могут содержать
# пользовательские данные, поэтому не возвращаются и не логируются.
async def validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(part) for part in item["loc"] if part != "body"),
            "type": item["type"],
        }
        for item in error.errors()
    ]
    return error_response(
        400, "VALIDATION_ERROR", "Request validation failed", {"fields": fields}
    )


async def http_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
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


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
