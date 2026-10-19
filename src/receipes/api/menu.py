from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path

from receipes.api.dependencies import TaskStore, get_task_store
from receipes.api.schemas import (
    GenerateMenuRequest,
    GenerateMenuResponse,
    IngredientResponse,
    MenuResponse,
    NutritionResponse,
    RecipeResponse,
    TaskResultResponse,
)
from receipes.db.models import GenerationTask
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskStatus
from receipes.errors import ErrorResponse, error_response

router = APIRouter(tags=["menu"])

TASK_NOT_FOUND = "Generation task not found"
Store = Annotated[TaskStore, Depends(get_task_store)]


def _error_example(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _error_doc(
    description: str,
    example: dict[str, Any] | None = None,
    examples: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # OpenAPI: либо один `example`, либо именованные `examples`, но не оба сразу.
    assert (example is None) != (examples is None)
    content = {"examples": examples} if examples else {"example": example}
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": content},
    }


REQUEST_EXAMPLES = {
    "full": {
        "summary": "Все параметры выбраны",
        "value": {
            "peopleCount": 4,
            "weeklyBudget": 5000,
            "habits": [3, 5],
            "diet": 4,
            "allergies": [1, 2],
            "kitchenTools": [1, 2, 6],
        },
    },
    "empty": {
        "summary": "Ничего не выбрано (минимальный запрос v2)",
        "value": {
            "peopleCount": 4,
            "weeklyBudget": 5000,
            "habits": [],
            "kitchenTools": [],
        },
    },
}

INTERNAL_ERROR_DOC = _error_doc(
    "Внутренняя ошибка сервиса (например, недоступна база данных). Подробности "
    "только в логе сервиса.",
    _error_example("INTERNAL_ERROR", "Internal server error", {}),
)

DONE_EXAMPLE = {
    "status": "done",
    "menu": {
        "recipes": [
            {
                "name": "Овсяная каша с яблоком",
                "ingredients": [
                    {"name": "Овсяные хлопья", "quantity": 320, "unit": "г"},
                    {"name": "Молоко", "quantity": 800, "unit": "мл"},
                    {"name": "Яблоко", "quantity": 4, "unit": "шт."},
                ],
                "calories": 310,
                "nutrition": {
                    "calories": 310,
                    "proteins": 11.5,
                    "fats": 8,
                    "carbohydrates": 45,
                },
                "cookingTime": 15,
                "instructions": [
                    "Вскипятите молоко в кастрюле.",
                    "Всыпьте хлопья и варите 7 минут, помешивая.",
                    "Добавьте нарезанное яблоко и прогрейте 2 минуты.",
                ],
                "photo": None,
            },
            {
                "name": "Куриный суп с лапшой",
                "ingredients": [
                    {"name": "Куриное филе", "quantity": 600, "unit": "г"},
                    {"name": "Лапша", "quantity": 200, "unit": "г"},
                    {"name": "Морковь", "quantity": 2, "unit": "шт."},
                ],
                "calories": 280,
                "nutrition": {
                    "calories": 280,
                    "proteins": 24,
                    "fats": 7.5,
                    "carbohydrates": 30,
                },
                "cookingTime": 40,
                "instructions": [
                    "Сварите бульон из филе 25 минут.",
                    "Добавьте нарезанную морковь и лапшу, варите 8 минут.",
                ],
                "photo": None,
            },
            {
                "name": "Запечённый лосось с рисом",
                "ingredients": [
                    {"name": "Лосось", "quantity": 600, "unit": "г"},
                    {"name": "Рис", "quantity": 280, "unit": "г"},
                    {"name": "Лимон", "quantity": 1, "unit": "шт."},
                ],
                "calories": 520,
                "nutrition": {
                    "calories": 520,
                    "proteins": 35,
                    "fats": 22,
                    "carbohydrates": 48,
                },
                "cookingTime": 35,
                "instructions": [
                    "Разогрейте духовку до 200 градусов.",
                    "Запекайте лосось с дольками лимона 20 минут.",
                    "Отварите рис 15 минут и подайте с рыбой.",
                ],
                "photo": None,
            },
        ]
    },
    "error": None,
}

RESULT_EXAMPLES = {
    "pending": {
        "summary": "Задача ждёт очереди",
        "value": {"status": "pending", "menu": None, "error": None},
    },
    "in_progress": {
        "summary": "Идёт генерация",
        "value": {"status": "in_progress", "menu": None, "error": None},
    },
    "done": {"summary": "Меню готово", "value": DONE_EXAMPLE},
    "failed": {
        "summary": "Генерация завершилась ошибкой",
        "value": {"status": "failed", "menu": None, "error": "Neurocore timeout"},
    },
}


@router.post(
    "/generate-menu",
    status_code=202,
    summary="Запустить генерацию меню (API-1)",
    description=(
        "Создаёт задачу генерации меню и сразу возвращает её идентификатор; меню "
        "готовится в фоне. Результат запрашивается по `task_id` через "
        "`GET /generate-menu/{task_id}`.\n\n"
        "В теле запроса: `peopleCount`, `weeklyBudget` и параметры версии 2: `habits`, "
        "`kitchenTools` (обязательны в JSON, могут быть `[]`), `diet` (число или "
        "`null`, можно не передавать) и `allergies` (можно не передавать). "
        "Параметры сохраняются вместе с задачей. Запрос без `habits` или "
        "`kitchenTools` (формат версии 1) получает 400.\n\n"
        "Здесь проверяются только типы и диапазон int4; существование ID в "
        "справочниках MS-2 и ограничение на число привычек пока не проверяются, а "
        "в промпт новые параметры пока не попадают."
    ),
    response_model=GenerateMenuResponse,
    responses={
        202: {"description": "Задача создана и ждёт обработки."},
        400: _error_doc(
            "Некорректные параметры: поля не заданы, неверный тип или значение вне "
            "границ, битый JSON, пустое тело, тело не JSON-объект.",
            examples={
                "peopleCount": {
                    "summary": "Неверный тип peopleCount",
                    "value": _error_example(
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        {
                            "fields": [
                                {"loc": ["body", "peopleCount"], "type": "int_type"}
                            ]
                        },
                    ),
                },
                "habits_missing": {
                    "summary": "Нет обязательного habits",
                    "value": _error_example(
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        {"fields": [{"loc": ["body", "habits"], "type": "missing"}]},
                    ),
                },
                "habits_item": {
                    "summary": "Элемент habits не целое число",
                    "value": _error_example(
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        {
                            "fields": [
                                {"loc": ["body", "habits", "0"], "type": "int_type"}
                            ]
                        },
                    ),
                },
                "diet_type": {
                    "summary": "diet не число и не null",
                    "value": _error_example(
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        {"fields": [{"loc": ["body", "diet"], "type": "int_type"}]},
                    ),
                },
                "out_of_range": {
                    "summary": "ID вне диапазона int4",
                    "value": _error_example(
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        {
                            "fields": [
                                {
                                    "loc": ["body", "kitchenTools", "0"],
                                    "type": "less_than_equal",
                                }
                            ]
                        },
                    ),
                },
            },
        ),
        500: INTERNAL_ERROR_DOC,
    },
)
async def generate_menu(
    body: Annotated[GenerateMenuRequest, Body(openapi_examples=REQUEST_EXAMPLES)],
    store: Store,
) -> GenerateMenuResponse:
    params = GenerationParams(
        people_count=body.people_count,
        weekly_budget=Decimal(str(body.weekly_budget)),
        habits=tuple(body.habits),
        diet=body.diet,
        allergies=tuple(body.allergies),
        kitchen_tools=tuple(body.kitchen_tools),
    )
    task_id = await store.create(params)
    return GenerateMenuResponse(task_id=str(task_id))


def _menu_response(task: GenerationTask) -> MenuResponse:
    if task.menu is None or not task.menu.menu_recipes:
        # Статус done без данных меню — нарушение инварианта, а не ответ клиенту.
        raise RuntimeError("done task has no menu")
    recipes = []
    for link in task.menu.menu_recipes:
        recipe = link.recipe
        recipes.append(
            RecipeResponse(
                name=recipe.name,
                ingredients=[
                    IngredientResponse(
                        name=item.ingredient.name,
                        quantity=item.quantity,
                        unit=item.unit,
                    )
                    for item in recipe.ingredients
                ],
                calories=recipe.calories,
                nutrition=NutritionResponse(
                    calories=recipe.nutrition.calories,
                    proteins=recipe.nutrition.proteins,
                    fats=recipe.nutrition.fats,
                    carbohydrates=recipe.nutrition.carbohydrates,
                ),
                cookingTime=recipe.cooking_time,
                instructions=[step.text for step in recipe.instructions],
                photo=None,
            )
        )
    return MenuResponse(recipes=recipes)


@router.get(
    "/generate-menu/{task_id}",
    summary="Получить результат генерации (API-2)",
    description=(
        "Возвращает статус задачи; поля `status`, `menu` и `error` есть всегда. "
        "`menu` заполнено только при `status = done`, `error` — только при "
        "`status = failed`.\n\n"
        "Генерация обычно занимает от секунд до пары минут (один запрос к модели) "
        "плюс ожидание в очереди; в худшем случае сама задача идёт до 10 минут, "
        "поэтому клиент не должен рвать опрос по короткому таймауту. Очередь по "
        "длине не ограничена.\n\n"
        "Число рецептов не фиксировано: промпт просит не менее 3, на практике модель "
        "отдаёт 3, контракт гарантирует минимум 3. Рецепты идут списком без "
        "разбивки по дням, порядок рецептов и ингредиентов не гарантируется. "
        "`calories` и `nutrition.calories` — калорийность одной порции от модели, "
        "значения могут расходиться; для КБЖУ основное `nutrition.calories`."
    ),
    response_model=TaskResultResponse,
    responses={
        # Примеры с null FastAPI вырезает при сборке схемы (exclude_none):
        # полные примеры возвращает restore_example_nulls().
        200: {
            "description": "Состояние задачи.",
            "content": {"application/json": {"examples": RESULT_EXAMPLES}},
        },
        404: _error_doc(
            "Задача не найдена, в том числе если `task_id` не является UUID.",
            _error_example("NOT_FOUND", TASK_NOT_FOUND, {}),
        ),
        500: INTERNAL_ERROR_DOC,
    },
)
async def get_generation_result(
    task_id: Annotated[
        str,
        Path(
            description=(
                "Идентификатор задачи из ответа API-1 (UUID). Неизвестный или "
                "не UUID даёт 404."
            ),
            examples=["b1e2c3d4-5678-90ab-cdef-1234567890ab"],
        ),
    ],
    store: Store,
) -> Any:
    try:
        identifier = UUID(task_id)
    except ValueError:
        return error_response(404, "NOT_FOUND", TASK_NOT_FOUND)
    task = await store.get(identifier)
    if task is None:
        return error_response(404, "NOT_FOUND", TASK_NOT_FOUND)
    status = TaskStatus(task.status)
    return TaskResultResponse(
        status=status.value,
        menu=_menu_response(task) if status == TaskStatus.DONE else None,
        error=task.error if status == TaskStatus.FAILED else None,
    )


# FastAPI собирает OpenAPI с exclude_none и теряет null в примерах (`menu`, `error`,
# `photo`), хотя именно они показывают клиенту контракт. Возвращаем их на место.
def restore_example_nulls(schema: dict[str, Any]) -> None:
    operation = schema["paths"]["/generate-menu/{task_id}"]["get"]
    content = operation["responses"]["200"]["content"]["application/json"]
    content["examples"] = RESULT_EXAMPLES
