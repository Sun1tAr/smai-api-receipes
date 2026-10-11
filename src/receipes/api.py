from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from receipes.domain import GenerationParameters
from receipes.errors import error_doc
from receipes.schemas import (
    GenerateMenuRequest,
    TaskCreatedResponse,
    TaskStatusResponse,
    menu_response,
)
from receipes.service import MenuGenerationService

router = APIRouter(tags=["generate-menu"])

VALIDATION_DOC = error_doc(
    "Некорректные параметры запроса.",
    "VALIDATION_ERROR",
    "Request validation failed",
    {"fields": [{"loc": ["body", "peopleCount"], "type": "int_type"}]},
)
INTERNAL_DOC = error_doc(
    "Внутренняя ошибка сервиса.", "INTERNAL_ERROR", "Internal server error"
)

MENU_EXAMPLE = {
    "recipes": [
        {
            "name": "Овсяная каша с ягодами",
            "ingredients": [
                {"name": "Овсяные хлопья", "quantity": 200, "unit": "г"},
                {"name": "Молоко", "quantity": 500, "unit": "мл"},
            ],
            "calories": 320,
            "nutrition": {
                "calories": 320,
                "proteins": 12,
                "fats": 8,
                "carbohydrates": 50,
            },
            "cookingTime": 15,
            "instructions": ["Вскипятить молоко.", "Добавить хлопья и варить."],
            "photo": None,
        }
    ]
}
STATUS_EXAMPLES = {
    "pending": {
        "summary": "Задача в очереди",
        "value": {"status": "pending", "menu": None, "error": None},
    },
    "in_progress": {
        "summary": "Задача выполняется",
        "value": {"status": "in_progress", "menu": None, "error": None},
    },
    "done": {
        "summary": "Задача завершена",
        "value": {"status": "done", "menu": MENU_EXAMPLE, "error": None},
    },
    "failed": {
        "summary": "Задача завершилась ошибкой",
        "value": {"status": "failed", "menu": None, "error": "Queue timeout"},
    },
}


async def get_service(request: Request) -> AsyncIterator[MenuGenerationService]:
    async with request.app.state.session_factory() as session:
        yield MenuGenerationService(session)


Service = Annotated[MenuGenerationService, Depends(get_service)]


@router.post(
    "/generate-menu",
    status_code=202,
    summary="Запуск генерации меню (API-1)",
    description=(
        "Создаёт задачу генерации недельного меню и сразу возвращает её "
        "идентификатор. Результат запрашивается через `GET /generate-menu/{task_id}`."
    ),
    response_model=TaskCreatedResponse,
    responses={
        202: {
            "description": "Задача создана.",
            "content": {
                "application/json": {
                    "example": {"task_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab"}
                }
            },
        },
        400: VALIDATION_DOC,
        500: INTERNAL_DOC,
    },
)
async def generate_menu(
    body: GenerateMenuRequest, request: Request, service: Service
) -> TaskCreatedResponse:
    parameters = GenerationParameters(body.people_count, Decimal(body.weekly_budget))
    task_id = await service.create_task(parameters)
    request.app.state.tasks_created.inc()
    return TaskCreatedResponse(task_id=str(task_id))


@router.get(
    "/generate-menu/{task_id}",
    summary="Получение результата генерации (API-2)",
    description=(
        "Возвращает статус задачи. `menu` заполнено только при `done`, `error` — "
        "только при `failed`. Неизвестный и некорректный идентификатор дают 404."
    ),
    response_model=TaskStatusResponse,
    responses={
        200: {
            "description": "Статус задачи.",
            "content": {"application/json": {"examples": STATUS_EXAMPLES}},
        },
        404: error_doc("Задача не найдена.", "NOT_FOUND", "Generation task not found"),
        500: INTERNAL_DOC,
    },
)
async def get_generation_result(task_id: str, service: Service) -> TaskStatusResponse:
    view = await service.get_task(task_id)
    return TaskStatusResponse(
        status=view.status,
        menu=menu_response(view.menu) if view.menu is not None else None,
        error=view.error,
    )
